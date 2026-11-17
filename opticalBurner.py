#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""opticalBurner — macOS 光盘多区段续刻工具（tkinter GUI）

技术路径参考 https://www.simaek.com/archives/485/
  CD:  mkisofs 制作镜像 + cdrecord -multi 刻录，续刻用 -msinfo/-C 合并
  DVD: growisofs -Z 首次 / -M 续刻
"""

import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# ---------------------------------------------------------------------------
# 常量与工具路径
# ---------------------------------------------------------------------------

BREW_INSTALL_CMD = "brew install cdrtools dvd+rw-tools"

TOOLS = {
    "mkisofs": "/usr/local/bin/mkisofs",
    "cdrecord": "/usr/local/bin/cdrecord",
    "growisofs": "/usr/local/bin/growisofs",
    "drutil": "/usr/bin/drutil",
    "hdiutil": "/usr/bin/hdiutil",
}


def find_tool(name):
    """在常见路径与 PATH 中查找工具，返回完整路径或 None"""
    p = TOOLS.get(name)
    if p and os.path.isfile(p) and os.access(p, os.X_OK):
        return p
    for d in ("/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def safe_after(app, delay, fn):
    """在后台线程中安全调度主线程回调（窗口销毁后不再调度）"""
    try:
        app.after(delay, fn)
    except RuntimeError:
        pass


def run_cmd(cmd, timeout=60):
    """运行命令，返回 (returncode, stdout, stderr)"""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", "command not found: %s" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def run_streaming(cmd, on_line=None, total_timeout=3600, env=None):
    """执行命令并逐行回调 stderr（读线程 + 队列，避免 readline 永久阻塞）。
    stdout 丢弃；总时长超过 total_timeout 秒则终止进程。
    返回 (returncode, 最后若干行 stderr 文本)。"""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        env=env)
    q = queue.Queue()

    def reader():
        try:
            for line in proc.stderr:
                q.put(line)
        except Exception:
            pass
        finally:
            q.put(None)

    threading.Thread(target=reader, daemon=True).start()
    lines = []
    start = time.time()
    while True:
        try:
            line = q.get(timeout=1.0)
        except queue.Empty:
            if proc.poll() is not None:
                # 进程已退出，给读线程一点时间刷完 EOF
                try:
                    line = q.get(timeout=1.0)
                except queue.Empty:
                    break
            elif time.time() - start > total_timeout:
                proc.kill()
                lines.append("[opticalBurner] 超时，已终止进程\n")
                break
            else:
                continue
        if line is None:
            break
        lines.append(line)
        if on_line:
            try:
                on_line(line)
            except Exception:
                pass
    rc = proc.wait()
    return rc, "".join(lines[-60:])


# ---------------------------------------------------------------------------
# 光驱 / 光盘状态探测
# ---------------------------------------------------------------------------

class DiscInfo:
    def __init__(self):
        self.device = None          # BSD 设备名，如 disk6
        self.raw_device = None      # 字符设备，如 rdisk6
        self.profile = None         # 介质类型描述，如 DVD-R
        self.profile_id = None      # drutil 的 profile 编号
        self.writable = False       # 是否可写介质
        self.erasable = False       # 是否可擦除（RW/RAM）
        self.blank = False          # 是否空白盘
        self.closed = False         # 是否已封盘（不可追加）
        self.capacity = 0           # 总容量（字节）
        self.used = 0               # 已用（字节）
        self.free = 0               # 剩余可用（字节）
        self.sessions = 0           # 区段数
        self.speeds = []            # 光驱对该介质的刻录倍速，如 ['10', '16', '24']
        self.is_cd = False          # CD 介质
        self.error = None


def drutil_status():
    """drutil status 输出（无光驱/无介质时也有可解析信息）"""
    rc, out, err = run_cmd([TOOLS["drutil"], "status"], timeout=30)
    return out + "\n" + err


def parse_drutil_status(text):
    """解析 drutil status 输出中的介质信息（行导向，兼容 DVD+RW 等含 '+' 类型名）"""
    info = DiscInfo()

    # 逐行解析 Type 行：'Type: DVD+RW               Name: /dev/disk4'
    # 或 'Type: No Media Inserted'
    for line in text.splitlines():
        m = re.match(r"\s*Type:\s*(.+)$", line)
        if not m:
            continue
        rest = m.group(1).strip()
        # 用 2 个以上空白切分字段，第一段是介质类型
        fields = re.split(r"\s{2,}", rest)
        info.profile = fields[0].strip()
        nm = re.search(r"Name:\s*/dev/(r?disk[0-9]+)", rest)
        if nm:
            d = nm.group(1)
            info.device = d.replace("rdisk", "disk")
            info.raw_device = "r" + info.device  # 刻录统一走裸设备 /dev/rdiskN
        break

    # 兜底：任意位置找设备名
    if not info.device:
        m = re.search(r"/dev/(rdisk[0-9]+|disk[0-9]+)", text)
        if m:
            d = m.group(1)
            info.device = d.replace("rdisk", "disk")
            info.raw_device = "r" + info.device

    def parse_space(label):
        """解析 'Space Free: 510:01:29  blocks: 2295104 / 4.70GB / 4.38GiB'"""
        m = re.search(
            label + r":\s*[0-9]+:[0-9]+:[0-9]+\s+blocks:\s*([0-9]+)", text)
        return int(m.group(1)) * 2048 if m else None

    free_b = parse_space(r"Space Free")
    used_b = parse_space(r"Space Used")

    # 兼容旧措辞
    if free_b is None:
        m = re.search(r"Free Space:\s*([0-9.]+)\s*([KMGTP]?)B", text)
        if m:
            mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3,
                    "T": 1024 ** 4, "P": 1024 ** 5}.get(m.group(2).upper(), 1)
            free_b = int(float(m.group(1)) * mult)
    if used_b is None:
        m = re.search(r"Used Space:\s*([0-9.]+)\s*([KMGTP]?)B", text)
        if m:
            mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3,
                    "T": 1024 ** 4, "P": 1024 ** 5}.get(m.group(2).upper(), 1)
            used_b = int(float(m.group(1)) * mult)

    # 区段数：'Sessions: 3                  Tracks: 3'
    sm = re.search(r"Sessions:\s*(\d+)", text)
    if sm:
        info.sessions = int(sm.group(1))

    # 刻录倍速：'Write Speeds: 10x, 16x, 24x'
    spm = re.search(r"Write Speeds:\s*(.+)$", text, re.M)
    if spm:
        info.speeds = [s.strip().rstrip("xX")
                       for s in spm.group(1).split(",") if s.strip()]

    # 可写性/可擦除标记：'Writability: appendable, blank, erasable, overwritable'
    wm = re.search(r"Writability:\s*(.+)$", text, re.M)
    writability = wm.group(1).strip().lower() if wm else ""
    info.writable = bool(writability)
    info.erasable = "erasable" in writability or "overwritable" in writability

    if info.profile:
        p = info.profile.upper()
        if "NO MEDIA" in p:
            info.error = "光驱中没有放入光盘"
            return info
        if not info.writable:
            info.writable = True
        info.is_cd = p.startswith("CD")
        # 空白盘：'blank' 出现在 Writability 中
        info.blank = "blank" in writability
        if free_b is not None:
            info.free = free_b
        if used_b is not None:
            info.used = used_b
        # 已封盘：仅一次性介质（非可擦写）且不可追加时才算封盘；
        # DVD±RW/CD-RW 永远可以通过擦除/覆写继续使用
        if (writability and "appendable" not in writability
                and not info.blank and not info.erasable):
            info.closed = True
        info.capacity = (info.used or 0) + (info.free or 0)
        if info.capacity == 0:
            info.capacity = {"CD": 700 * 1024 * 1024,
                             "DVD": 4.7 * 1024 ** 3}.get(
                                 "CD" if info.is_cd else "DVD", 0)
    return info


def dvd_fs_bytes(device):
    """DVD 上已有文件系统实际占用的字节数。
    优先 df（已挂载时），否则直接读 ISO9660 PVD 的卷空间大小（未挂载也可）。
    未找到返回 None。"""
    if not device:
        return None
    rc, out, err = run_cmd(["/bin/df", "-k", "/dev/%s" % device], timeout=15)
    if rc == 0:
        lines = [l for l in out.splitlines() if l.strip()]
        if len(lines) >= 2:
            parts = lines[-1].split()
            try:
                return int(parts[1]) * 1024  # 1024-blocks 总数 = 该文件系统大小
            except (IndexError, ValueError):
                pass
    # 未挂载：读 PVD（扇区 16，偏移 80 的小端 32 位卷空间大小，单位 2048 字节）
    try:
        proc = subprocess.run(
            ["/bin/dd", "if=/dev/r%s" % device, "bs=2048", "skip=16",
             "count=1"],
            capture_output=True, timeout=20)
        data = proc.stdout
        if len(data) >= 84 and data[1:6] == b"CD001":
            blocks = int.from_bytes(data[80:84], "little")
            if blocks:
                return blocks * 2048
    except Exception:
        pass
    return None


def get_disc_info():
    """探测光驱与光盘状态"""
    global _LAST_INFO
    if DISKARB_PAUSED or diskarb_is_paused():
        # diskarbitrationd 暂停期间 drutil 会挂起，直接返回提示态
        info = DiscInfo()
        info.error = ("光驱被系统锁占用（diskarbitrationd 处于暂停）。"
                      "请等待其恢复，或手动执行：sudo kill -CONT $(pgrep diskarbitrationd)")
        return info
    # 与刻录互斥：探测期间（尤其 dd 读裸设备）不能与 growisofs 抢设备。
    # 拿不到锁说明正在刻录/擦除，返回上一次的探测结果（不阻塞界面）。
    if not DEVICE_LOCK.acquire(blocking=False):
        return _LAST_INFO if _LAST_INFO is not None else DiscInfo()
    try:
        text = drutil_status()
        info = parse_drutil_status(text)
        # DVD±RW：drutil 把整盘轨道都算作已用（Space Used 恒为满盘），
        # 实际可用空间应按已挂载文件系统大小计算
        if not info.is_cd and info.profile and not info.error and info.capacity:
            fs = dvd_fs_bytes(info.device)
            if fs is not None:
                info.used = fs
                info.free = max(0, info.capacity - fs)
        _LAST_INFO = info
        return info
    finally:
        DEVICE_LOCK.release()


def get_msinfo():
    """CD 多区段盘的 -msinfo 输出，返回 (next_session_start, last_track_size) 或 None。
    注意：需在 diskarbitrationd 暂停期间调用。"""
    cdrecord = find_tool("cdrecord")
    if not cdrecord:
        return None
    dev = guess_cdrecord_dev()
    if not dev:
        return None
    rc, out, err = run_cmd([cdrecord, "dev=%s" % dev, "-msinfo"], timeout=60)
    if rc != 0:
        return None
    m = re.search(r"(\d+),(\d+)", out)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def guess_cdrecord_dev():
    """猜测 cdrecord 设备号（SCSI bus,id,lun），从 cdrecord -scanbus 中找 USB 光驱"""
    cdrecord = find_tool("cdrecord")
    if not cdrecord:
        return None
    rc, out, err = run_cmd([cdrecord, "-scanbus"], timeout=60)
    text = out + "\n" + err
    # 找含 DVD/CD/USB 的行：'     1,0,0     1)  Lenovo   Ultraslim DVD ...'
    for line in text.splitlines():
        m = re.match(r"\s*([0-9]+,[0-9]+,[0-9]+)\s+[0-9]+\)\s+(.*)", line)
        if m and re.search(r"DVD|CD|Optiarc|Lenovo|Slim|USB", m.group(2), re.I):
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# diskarbitrationd 锁处理（CD 刻录需要）
# ---------------------------------------------------------------------------

# 全局标志：True 表示 diskarbitrationd 当前被暂停（此时 drutil 等会挂起，不能探测）
DISKARB_PAUSED = False

# 设备互斥锁：探测（drutil / dd 读裸设备）与刻录（growisofs/cdrecord）
# 绝不能同时碰同一个光驱设备，否则 growisofs 会 ObtainExclusiveAccess 失败。
DEVICE_LOCK = threading.RLock()

# 最近一次成功探测的结果（刻录期间探测被拒时返回它）
_LAST_INFO = None


def diskarb_is_paused():
    """检查 diskarbitrationd 是否处于暂停（T）状态"""
    rc, out, err = run_cmd(["/bin/ps", "-axo", "stat=,comm="], timeout=15)
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].strip().endswith("diskarbitrationd"):
            return parts[0].startswith("T")
    return False


def unmount_disc(info):
    """卸载光盘卷（cdrecord/mkisofs 需要独占设备）。失败可忽略。"""
    if not info.device:
        return False
    rc, out, err = run_cmd(["/usr/sbin/diskutil", "unmountDisk",
                            "/dev/%s" % info.device], timeout=60)
    return rc == 0


def diskarb_stop():
    """STOP diskarbitrationd（通过系统授权弹窗获得 root），返回 (成功?, 输出)"""
    global DISKARB_PAUSED
    script = ('do shell script "kill -STOP $(pgrep diskarbitrationd)" '
              'with administrator privileges')
    rc, out, err = run_cmd(["osascript", "-e", script], timeout=180)
    ok = rc == 0
    if ok:
        DISKARB_PAUSED = True
    return ok, (out + err).strip()


def diskarb_cont():
    """CONT diskarbitrationd（同样走系统授权弹窗）"""
    global DISKARB_PAUSED
    script = ('do shell script "kill -CONT $(pgrep diskarbitrationd)" '
              'with administrator privileges')
    rc, out, err = run_cmd(["osascript", "-e", script], timeout=180)
    ok = rc == 0
    if ok:
        DISKARB_PAUSED = False
    return ok, (out + err).strip()


_DISKARB_RESUME_FILE = "/tmp/.opticalBurner_resume"
SUDO_RULE_PATH = "/etc/sudoers.d/opticalburner"
PKILL = "/usr/bin/pkill"


def _current_user():
    return os.environ.get("USER") or os.environ.get("LOGNAME") or ""


def elevation_rule_installed():
    return os.path.exists(SUDO_RULE_PATH)


def install_elevation_rule():
    """一次性安装 sudoers 免密规则（仅允许 pkill -STOP/-CONT diskarbitrationd）。
    弹出一次系统授权框，之后刻录不再需要输密码。"""
    import base64
    user = _current_user()
    if not user:
        return False, "无法确定当前用户名"
    content = ("%s ALL=(root) NOPASSWD: %s -STOP -x diskarbitrationd, "
               "%s -CONT -x diskarbitrationd\n" % (user, PKILL, PKILL))
    b64 = base64.b64encode(content.encode()).decode()
    inner = ("tmp=/tmp/opticalburner_sudoers_$$; "
             "echo %s | /usr/bin/base64 -d > $tmp; "
             "/usr/sbin/visudo -cf $tmp >/dev/null && "
             "/usr/bin/install -m 440 -o root -g wheel $tmp %s; "
             "rc=$?; rm -f $tmp; exit $rc" % (b64, SUDO_RULE_PATH))
    script = 'do shell script "%s" with administrator privileges' % inner
    rc, out, err = run_cmd(["/usr/bin/osascript", "-e", script], timeout=180)
    return rc == 0, (out + err).strip()


def _sudo_diskarb(sig):
    """用免密规则对 diskarbitrationd 发信号，返回是否成功"""
    if not elevation_rule_installed():
        return False
    rc, out, err = run_cmd(["sudo", "-n", PKILL, "-%s" % sig,
                            "-x", "diskarbitrationd"], timeout=30)
    return rc == 0


def diskarb_pause_for_burn():
    """暂停 diskarbitrationd。
    首次会自动安装 sudoers 免密规则（弹一次系统授权框），
    之后用 sudo -n 静默暂停；失败则回退到每次授权的 osascript 流程。
    返回 (成功?, 进程句柄或错误信息)；句柄为 None 表示 sudo 模式。"""
    global DISKARB_PAUSED
    # 1) 已有免密规则：静默暂停
    if _sudo_diskarb("STOP"):
        for _ in range(10):
            if diskarb_is_paused():
                DISKARB_PAUSED = True
                return True, None
            time.sleep(0.3)
    # 2) 首次使用：安装规则（一次授权），成功后免密
    if not elevation_rule_installed():
        ok, msg = install_elevation_rule()
        if ok and _sudo_diskarb("STOP"):
            for _ in range(10):
                if diskarb_is_paused():
                    DISKARB_PAUSED = True
                    return True, None
                time.sleep(0.3)
        if not ok:
            return False, ("需要管理员授权才能暂停 diskarbitrationd：%s" % msg)
    # 3) 回退：osascript 单次授权 + root 等待进程
    return _pause_osascript()


def _pause_osascript():
    """回退方案：一次授权，root 进程保持暂停直到恢复信号出现"""
    global DISKARB_PAUSED
    rf = _DISKARB_RESUME_FILE
    try:
        os.remove(rf)
    except OSError:
        pass
    inner = ("kill -STOP $(pgrep diskarbitrationd); "
             "i=0; while [ ! -f %s ] && [ $i -lt 2400 ]; do sleep 1; "
             "i=$((i+1)); done; rm -f %s; kill -CONT $(pgrep diskarbitrationd)"
             % (rf, rf))
    script = 'do shell script "%s" with administrator privileges' % inner
    try:
        proc = subprocess.Popen(["osascript", "-e", script],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
    except Exception as e:
        return False, "启动授权进程失败：%s" % e

    start = time.time()
    while time.time() - start < 90:
        if diskarb_is_paused():
            DISKARB_PAUSED = True
            return True, proc
        if proc.poll() is not None:
            out, err = proc.communicate()
            return False, ("无法暂停 diskarbitrationd（授权被取消或失败）：%s"
                           % (err or out or "").strip())
        time.sleep(0.5)
    proc.kill()
    return False, "等待 diskarbitrationd 暂停超时（请确认授权弹窗已允许）"


def diskarb_resume(proc):
    """恢复 diskarbitrationd。proc 为 None 表示 sudo 免密模式。"""
    global DISKARB_PAUSED
    if proc is None:
        _sudo_diskarb("CONT")
        for _ in range(10):
            DISKARB_PAUSED = diskarb_is_paused()
            if not DISKARB_PAUSED:
                break
            time.sleep(0.3)
        return not DISKARB_PAUSED
    try:
        with open(_DISKARB_RESUME_FILE, "w") as f:
            f.write("resume\n")
    except OSError:
        pass
    try:
        proc.wait(timeout=180)
    except subprocess.TimeoutExpired:
        proc.kill()
    DISKARB_PAUSED = diskarb_is_paused()
    return not DISKARB_PAUSED


# ---------------------------------------------------------------------------
# 刻录引擎（后台线程执行）
# ---------------------------------------------------------------------------

class BurnJob:
    """一次刻录任务的参数"""

    def __init__(self, files, disc_info, multi, volume_label, burn_speed):
        self.files = files                  # 本次要刻录的文件/目录列表
        self.disc = disc_info
        self.multi = multi                  # 是否保留续刻空间（-multi）
        self.volume_label = volume_label or "OPTICALBURNER"
        self.burn_speed = burn_speed        # None=默认速度
        self.iso_path = None                # 生成的临时 ISO
        self.cancelled = False


def build_iso(job, progress_cb):
    """用 mkisofs 把所选文件打包成 ISO，返回 (成功?, 错误信息)。
    续刻时优先用 -M 合并旧区段；若旧区段不是 ISO9660（如 HFS），自动降级为仅新文件。"""
    mkisofs = find_tool("mkisofs")
    if not mkisofs:
        return False, "mkisofs 未安装。请先执行：%s" % BREW_INSTALL_CMD

    info = job.disc
    iso_path = os.path.join(
        os.path.expanduser("~"), ".opticalBurner", "%s.iso" % time.strftime("%Y%m%d%H%M%S"))
    os.makedirs(os.path.dirname(iso_path), exist_ok=True)
    job.iso_path = iso_path

    msinfo = None
    if info.is_cd and not info.blank:
        msinfo = get_msinfo()

    def run(merge):
        cmd = [mkisofs, "-o", iso_path, "-Jrv", "-V", job.volume_label]
        if msinfo:
            start, size = msinfo
            cmd += ["-C", "%d,%d" % (start, size)]
            if merge:
                cmd += ["-M", guess_cdrecord_dev() or ""]
        cmd += list(job.files)

        progress_cb("正在制作 ISO 镜像…%s" % ("（合并历史区段）" if (msinfo and merge) else ""))
        last_pct = [-1]

        def on_line(line):
            m = re.search(r"([0-9.]+)% done", line)
            if m:
                pct = float(m.group(1))
                if pct > last_pct[0]:
                    last_pct[0] = pct
                    progress_cb("制作镜像 %.0f%%" % pct, pct * 0.3)

        rc, err = run_streaming(cmd, on_line, total_timeout=1800)
        return rc, err

    merge = bool(msinfo)
    rc, err = run(merge)

    def run_ok(rc, err):
        # cdrtools 在“无法打开旧区段”时也返回 0，必须用文件实际生成来判断
        return (rc == 0 and os.path.isfile(iso_path)
                and os.path.getsize(iso_path) > 0
                and "Unable to open previous session" not in err)

    if not run_ok(rc, err) and merge:
        # 上一区段可能是 HFS 等非 ISO9660 格式，无法合并 → 降级：新区段仅含本次文件
        progress_cb("⚠ 无法合并旧区段（可能不是 ISO9660），改为仅新文件区段", None)
        rc, err = run(False)
    if not run_ok(rc, err):
        return False, "mkisofs 失败：%s" % err[-2000:]
    return True, ""


def burn_cd(job, progress_cb):
    """CD 刻录路径（cdrecord），返回 (成功?, 错误信息)"""
    cdrecord = find_tool("cdrecord")
    if not cdrecord:
        return False, "cdrecord 未安装。请先执行：%s" % BREW_INSTALL_CMD

    dev = guess_cdrecord_dev()
    if not dev:
        return False, "未找到 cdrecord 设备号（-scanbus 无匹配光驱）"

    info = job.disc
    cmd = [cdrecord, "dev=%s" % dev]
    if job.burn_speed:
        cmd.append("speed=%s" % job.burn_speed)
    cmd += ["fs=8m", "driveropts=burnfree", "-v", "-data"]
    # -multi：保留续刻空间；不加则本次刻录后封盘
    if job.multi:
        cmd.append("-multi")
    cmd.append(job.iso_path)

    progress_cb("正在刻录 CD（dev=%s）…" % dev, 30)

    def on_line(line):
        m = re.search(r"([0-9]+)\s+of\s+([0-9]+)", line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            if total > 0:
                pct = 30 + done / total * 65
                progress_cb("刻录中 %d/%d MB" % (done, total), pct)

    rc, err = run_streaming(cmd, on_line, total_timeout=3600)

    if rc != 0:
        return False, "cdrecord 失败：%s" % (err[-2000:] or "未知错误")
    return True, ""


def burn_dvd(job, progress_cb):
    """DVD 刻录路径（growisofs），返回 (成功?, 错误信息)

    空白盘用 -Z；已有内容用 -M 续刻。
    macOS 版 mkisofs 缺少 dvd+rw-tools 补丁（不支持 '-' 从 stdin 读旧镜像，
    会让 growisofs -M 失败），因此通过 MKISOFS 环境变量改用 mkisofs_wrap/mkisofs
    包装器补齐该行为。"""
    growisofs = find_tool("growisofs")
    if not growisofs:
        return False, "growisofs 未安装。请先执行：%s" % BREW_INSTALL_CMD

    info = job.disc
    if not info.raw_device:
        return False, "未获取到 DVD 设备名（drutil status 解析失败）"

    dev = "/dev/%s" % info.raw_device

    # 注意：不能在这里再调 diskutil。
    # 进入本函数时 do_burn 已经卸载过卷并暂停了 diskarbitrationd；
    # diskarbitrationd 暂停期间任何 diskutil 调用都会挂起（实测 60s 超时），
    # 破坏设备仲裁状态，导致 growisofs 中途 ObtainExclusiveAccess 失败。

    cmd = [growisofs]
    env = None
    if info.blank:
        cmd += ["-Z", dev]
    else:
        cmd += ["-M", dev]
        wrap = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "mkisofs_wrap", "mkisofs")
        if os.access(wrap, os.X_OK):
            env = dict(os.environ)
            env["MKISOFS"] = wrap
    cmd += ["-Jrv", "-V", job.volume_label]
    if job.burn_speed:
        cmd += ["-speed=%s" % job.burn_speed]
    if not job.multi:
        cmd += ["-dvd-compat"]  # 封盘
    cmd += list(job.files)

    progress_cb("正在刻录 DVD（%s）…" % dev, 10)

    def on_line(line):
        m = re.search(r"([0-9.]+)/([0-9.]+)\s*\(([0-9.]+)%\)", line)
        if m:
            pct = float(m.group(3))
            progress_cb("刻录中 %s/%s MB" % (m.group(1), m.group(2)), 10 + pct * 0.85)

    rc, err = run_streaming(cmd, on_line, total_timeout=3600, env=env)
    if rc != 0 and "ObtainExclusiveAccess" in err:
        # 光驱被系统短暂占用（diskarb/Finder 探测），DVD±RW 可安全重写，重试一次。
        # 注意：此时 diskarbitrationd 处于暂停状态，绝对不能再调 diskutil（会挂起）。
        progress_cb("光驱被系统短暂占用，正在重试…", 10)
        time.sleep(3)
        rc, err = run_streaming(cmd, on_line, total_timeout=3600, env=env)
    if rc != 0:
        if "Unable to open previous session image" in err:
            return False, ("续刻失败：mkisofs 无法读取旧会话镜像。"
                           "请确认 mkisofs_wrap/mkisofs 存在且可执行。\n%s"
                           % err[-1000:])
        return False, "growisofs 失败：%s" % (err[-2000:] or "未知错误")
    return True, ""


def do_burn(job, progress_cb):
    """刻录主流程：卸载卷 + 暂停 diskarbitrationd（一次授权管全程）→ 刻录。
    CD 与 DVD 都同样处理：刻录中任何时刻光驱被系统抢占都会
    导致 ObtainExclusiveAccess 失败。
    返回 (成功?, 提示/错误信息)：成功但含警告时 ok=True 且 info 非空。"""
    info = job.disc
    pause_proc = None
    paused = False
    ok, err = False, ""
    # 排他占用设备：防止 GUI 的定时探测（drutil / dd 读裸设备）与刻录并发。
    # 若正有探测在途，等它结束（探测很短），而不是直接失败。
    if not DEVICE_LOCK.acquire(timeout=10):
        return False, "无法获得光驱独占权（状态探测未结束），请重试"
    try:
        # 顺序很重要：先用 diskutil 卸载卷（此时 diskarb 仍在运行，diskutil 正常），
        # 再暂停 diskarbitrationd。暂停后任何 diskutil 调用都会挂起（实测 60s 超时），
        # 破坏设备仲裁状态 → growisofs 中途 ObtainExclusiveAccess 失败。
        progress_cb("正在卸载光盘卷…", 1)
        unmount_disc(info)
        progress_cb("正在暂停 diskarbitrationd（首次会弹出系统授权，请点允许）…", 3)
        pok, pres = diskarb_pause_for_burn()
        if not pok:
            return False, pres
        pause_proc = pres
        paused = True

        if info.is_cd:
            # CD：mkisofs 镜像 + cdrecord，全程在锁释放期间
            ok, err = build_iso(job, progress_cb)
            if not ok:
                return False, err
            ok, err = burn_cd(job, progress_cb)
        else:
            # DVD 用 growisofs 直接从文件生成
            ok, err = burn_dvd(job, progress_cb)
    finally:
        if paused:
            resumed = diskarb_resume(pause_proc)
            if not resumed:
                note = ("⚠ diskarbitrationd 未能恢复（光驱仍被系统锁占用）。"
                        "请手动执行：sudo kill -CONT $(pgrep diskarbitrationd)")
                progress_cb(note, None)
                err = ((err + "\n\n") if err else "") + note
        # 清理临时 ISO
        if job.iso_path and os.path.isfile(job.iso_path):
            try:
                os.remove(job.iso_path)
            except OSError:
                pass
        DEVICE_LOCK.release()
    return ok, err


# ---------------------------------------------------------------------------
# 快速擦除（RW 盘）
# ---------------------------------------------------------------------------

def disc_has_isofs(info):
    """检查盘上是否仍存在 ISO9660 文件系统（擦除校验用）"""
    try:
        proc = subprocess.run(
            ["/bin/dd", "if=/dev/r%s" % info.device, "bs=2048", "skip=16",
             "count=1"], capture_output=True, timeout=20)
        data = proc.stdout
        return len(data) >= 6 and data[1:6] == b"CD001"
    except Exception:
        return False


def quick_erase(disc_info, progress_cb, full=False):
    """擦除 RW 盘。
    CD-RW：cdrecord blank=fast/all（需暂停 diskarbitrationd）。
    DVD±RW：drutil erase quick/full（系统工具，dvd+rw-format 在本机验证不可靠）。
    返回 (成功?, 错误信息)"""
    info = disc_info
    if info.error:
        return False, info.error
    if not info.raw_device:
        return False, "未获取到设备名（drutil status 解析失败）"
    # 与探测互斥（擦除同样独占光驱），等待在途探测结束
    if not DEVICE_LOCK.acquire(timeout=10):
        return False, "无法获得光驱独占权（状态探测未结束），请重试"
    try:
        return _quick_erase_locked(info, progress_cb, full)
    finally:
        DEVICE_LOCK.release()


def _quick_erase_locked(info, progress_cb, full):
    # ---- DVD±RW：系统 drutil erase，全程不需要暂停 diskarbitrationd ----
    if not info.is_cd:
        progress_cb("正在卸载光盘卷…", 2)
        unmount_disc(info)
        progress_cb("正在%s DVD±RW（系统擦除）…" % ("完全擦除" if full else "快速擦除"), 10)
        try:
            p = subprocess.run(
                [TOOLS["drutil"], "erase", "full" if full else "quick"],
                capture_output=True, timeout=1800)
            rc = p.returncode
            out = (p.stdout + b"\n" + p.stderr).decode("utf-8", "replace")
            err = out
        except subprocess.TimeoutExpired:
            rc, err = 124, "超时"
        if rc != 0:
            return False, "擦除失败：%s" % ((err or "未知错误").strip()[-2000:])
        # 校验是否真的清空（dvd+rw-format 曾出现过“返回成功但未清盘”）
        time.sleep(1)
        if disc_has_isofs(info):
            return False, ("擦除命令已执行，但盘上仍残留旧文件系统。"
                           "请重新插入光盘后重试，或换用“完全擦除”。")
        progress_cb("擦除完成（如托盘弹出，请将光盘推回后刷新状态）", 100)
        return True, ""

    # ---- CD-RW：cdrecord blank（先卸载卷，再暂停 diskarbitrationd） ----
    pause_proc = None
    paused = False
    try:
        # 必须先 diskutil 卸载（此时 diskarb 运行中），再暂停；
        # 暂停后 diskutil 会挂起
        progress_cb("正在卸载光盘卷…", 2)
        unmount_disc(info)
        progress_cb("正在暂停 diskarbitrationd（首次会弹出系统授权，请点允许）…", 4)
        pok, pres = diskarb_pause_for_burn()
        if not pok:
            return False, pres
        pause_proc = pres
        paused = True

        cdrecord = find_tool("cdrecord")
        if not cdrecord:
            return False, "cdrecord 未安装。请先执行：%s" % BREW_INSTALL_CMD
        dev = guess_cdrecord_dev()
        if not dev:
            return False, "未找到 cdrecord 设备号（-scanbus 无匹配光驱）"
        # cdrecord：fast=快速，all=全盘
        cmd = [cdrecord, "dev=%s" % dev,
               "blank=%s" % ("all" if full else "fast"), "-v", "-force"]
        progress_cb("正在%s CD-RW（dev=%s）…" % ("完全擦除" if full else "快速擦除", dev), 10)

        def on_line(line):
            m = re.search(r"\(([0-9.]+)%\)", line)
            if m:
                pct = float(m.group(1))
                progress_cb("擦除中 %.0f%%" % pct, 10 + pct * 0.85)

        rc, err = run_streaming(cmd, on_line, total_timeout=3600)
        if rc != 0:
            return False, "擦除失败：%s" % (err[-2000:] or "未知错误")
        return True, ""
    finally:
        if paused:
            if not diskarb_resume(pause_proc):
                progress_cb("⚠ diskarbitrationd 未能恢复，请手动执行："
                            "sudo kill -CONT $(pgrep diskarbitrationd)", None)


# ---------------------------------------------------------------------------
# 弹出光盘
# ---------------------------------------------------------------------------

def eject_disc():
    """弹出托盘/光盘（光驱空闲时调用）。刻录刚结束设备可能还没就绪，带重试。
    返回 (成功?, 错误信息)"""
    drutil = TOOLS["drutil"]
    last_err = ""
    for attempt in range(3):
        if attempt:
            time.sleep(1.5)  # 等设备从刻录状态恢复
        rc, out, err = run_cmd([drutil, "tray", "eject"], timeout=30)
        if rc == 0:
            return True, ""
        last_err = (err or out or "").strip()
        # 无托盘的光驱兜底用 eject
        rc2, out2, err2 = run_cmd([drutil, "eject"], timeout=30)
        if rc2 == 0:
            return True, ""
        last_err = last_err or (err2 or out2 or "").strip()
    if DISKARB_PAUSED:
        last_err += "（diskarbitrationd 仍处于暂停状态，光驱被占用）"
    return False, last_err or "未知错误"


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

GB = 1024 ** 3
MB = 1024 ** 2


def default_label():
    """默认卷标带当天日期，如 OPTICALBURNER-20261116"""
    return "OPTICALBURNER-%s" % time.strftime("%Y%m%d")


def fmt_size(n):
    if n >= GB:
        return "%.2f GB" % (n / GB)
    if n >= MB:
        return "%.1f MB" % (n / MB)
    return "%d B" % n


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("opticalBurner — 光盘多区段续刻")
        self.geometry("680x560")
        self.minsize(620, 520)

        self.selected_files = []    # 本次要刻录的文件
        self.disc_info = None
        self.burning = False
        self._refreshing = False    # 探测线程进行中标记（防重入）
        self._ui_q = queue.Queue()  # 工作线程 → 主线程 UI 回调队列（tkinter 非线程安全）
        self._iso_dir = os.path.join(os.path.expanduser("~"), ".opticalBurner")

        self._build_ui()
        self.after(50, self._ui_pump)
        self.after(200, self.refresh_disc)
        self.after(2500, self._auto_refresh)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----- 线程安全的 UI 更新 -----

    def _post(self, fn):
        """工作线程调用：把回调放入队列，由主线程执行"""
        self._ui_q.put(fn)

    def _ui_pump(self):
        try:
            while True:
                fn = self._ui_q.get_nowait()
                try:
                    fn()
                except Exception:
                    pass
        except queue.Empty:
            pass
        try:
            self.after(50, self._ui_pump)
        except RuntimeError:
            pass

    def _on_close(self):
        """关闭前确保 diskarbitrationd 已恢复"""
        if DISKARB_PAUSED:
            diskarb_cont()
        self.destroy()

    # ----- UI 构建 -----

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self)
        frm.pack(fill=tk.BOTH, expand=True, **pad)

        # --- 光盘状态区 ---
        grp_disc = ttk.LabelFrame(frm, text="光盘状态")
        grp_disc.pack(fill=tk.X, **pad)

        self.lbl_disc = ttk.Label(grp_disc, text="正在探测光驱…")
        self.lbl_disc.pack(anchor=tk.W, padx=8, pady=2)
        self.lbl_capacity = ttk.Label(grp_disc, text="容量：—")
        self.lbl_capacity.pack(anchor=tk.W, padx=8, pady=2)

        row = ttk.Frame(grp_disc)
        row.pack(fill=tk.X, padx=8, pady=4)
        ttk.Button(row, text="刷新光盘状态", command=self.refresh_disc).pack(side=tk.LEFT)
        self.btn_erase = ttk.Button(row, text="快速擦除（RW 盘）",
                                    command=lambda: self.quick_erase(full=False))
        self.btn_erase.pack(side=tk.LEFT, padx=8)
        self.btn_erase_full = ttk.Button(row, text="完全擦除…",
                                         command=lambda: self.quick_erase(full=True))
        self.btn_erase_full.pack(side=tk.LEFT)
        self.btn_eject = ttk.Button(row, text="弹出光盘", command=self.eject_disc)
        self.btn_eject.pack(side=tk.LEFT, padx=8)

        # --- 文件选择区 ---
        grp_files = ttk.LabelFrame(frm, text="本次刻录的文件")
        grp_files.pack(fill=tk.BOTH, expand=True, **pad)

        row = ttk.Frame(grp_files)
        row.pack(fill=tk.X, padx=8, pady=4)
        ttk.Button(row, text="添加文件…", command=self.add_files).pack(side=tk.LEFT)
        ttk.Button(row, text="添加文件夹…", command=self.add_dir).pack(side=tk.LEFT, padx=4)
        ttk.Button(row, text="移除选中", command=self.remove_selected).pack(side=tk.LEFT, padx=4)

        self.tree = ttk.Treeview(grp_files, columns=("path", "size"), show="headings", height=7)
        self.tree.heading("path", text="路径")
        self.tree.heading("size", text="大小")
        self.tree.column("path", width=440)
        self.tree.column("size", width=110, anchor=tk.E)
        vsb = ttk.Scrollbar(grp_files, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0), pady=(0, 4))
        vsb.pack(side=tk.LEFT, fill=tk.Y, pady=(0, 4))

        self.lbl_files_total = ttk.Label(grp_files, text="共 0 个文件，0 B")
        self.lbl_files_total.pack(anchor=tk.E, padx=8, pady=(0, 4))

        # --- 刻录选项区 ---
        grp_opt = ttk.LabelFrame(frm, text="刻录选项")
        grp_opt.pack(fill=tk.X, **pad)

        row = ttk.Frame(grp_opt)
        row.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(row, text="卷标：").pack(side=tk.LEFT)
        self.var_label = tk.StringVar(value=default_label())
        ttk.Entry(row, textvariable=self.var_label, width=26).pack(side=tk.LEFT, padx=4)
        self.var_multi = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="保留续刻空间（-multi，取消则封盘）",
                        variable=self.var_multi).pack(side=tk.LEFT, padx=(12, 0))

        ttk.Label(
            grp_opt,
            text="Linux 提示：可保持勾选『保留续刻空间』持续追加——每次刻录都会关闭当前区段，"
                 "Linux 默认读取最后一个区段（本工具已把历史文件合并进新区段），无需封盘即可读；"
                 "封盘仅在不打算再加刻时更稳妥。格式为 ISO9660 + Rock Ridge + Joliet。",
            foreground="#666666", wraplength=620).pack(anchor=tk.W, padx=8, pady=(0, 4))

        # --- 刻录按钮与进度区 ---
        grp_burn = ttk.LabelFrame(frm, text="刻录")
        grp_burn.pack(fill=tk.X, **pad)

        row = ttk.Frame(grp_burn)
        row.pack(fill=tk.X, padx=8, pady=4)
        self.btn_burn = ttk.Button(row, text="开始刻录", command=self.start_burn)
        self.btn_burn.pack(side=tk.LEFT)
        ttk.Label(row, text="刻录速度：").pack(side=tk.LEFT, padx=(12, 0))
        self.var_speed = tk.StringVar(value="默认")
        self.cmb_speed = ttk.Combobox(row, textvariable=self.var_speed, width=10,
                                      state="readonly", values=["默认"])
        self.cmb_speed.pack(side=tk.LEFT, padx=4)
        self.var_eject_after = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="刻录成功后弹出光盘",
                        variable=self.var_eject_after).pack(side=tk.LEFT, padx=(12, 0))

        self.var_status = tk.StringVar(value="就绪")
        ttk.Label(grp_burn, textvariable=self.var_status).pack(anchor=tk.W, padx=8)

        self.progress = ttk.Progressbar(grp_burn, maximum=100, mode="determinate")
        self.progress.pack(fill=tk.X, padx=8, pady=(2, 8))

        # --- 依赖状态 ---
        self.lbl_deps = ttk.Label(frm, text="", foreground="#666666")
        self.lbl_deps.pack(anchor=tk.W, **pad)
        self.after(300, self.check_deps)

    # ----- 光盘状态 -----

    def _auto_refresh(self):
        """定时自动探测（插入/取出光盘后界面自动更新）"""
        if not self.burning:
            self.refresh_disc()
        try:
            self.after(2500, self._auto_refresh)
        except RuntimeError:
            pass

    def refresh_disc(self):
        if self.burning or self._refreshing or DISKARB_PAUSED:
            # 刻录中/diskarbitrationd 暂停期间不能探测（drutil 会挂起或抢占设备）
            return
        self._refreshing = True

        def work():
            try:
                info = get_disc_info()
            except Exception as e:
                info = DiscInfo()
                info.error = "探测失败：%s" % e

            def apply():
                self._refreshing = False
                self._update_disc(info)
            self._post(apply)
        threading.Thread(target=work, daemon=True).start()

    def _update_disc(self, info):
        self.disc_info = info
        # 弹出按钮：光驱空闲（未在刻录/擦除）即可用，与是否有盘无关
        self.btn_eject.config(state=tk.DISABLED if self.burning else tk.NORMAL)
        if info.error:
            self.lbl_disc.config(text="⚠ %s" % info.error)
            self.lbl_capacity.config(text="容量：—")
            self.btn_burn.config(state=tk.DISABLED)
            self._set_erase_state(tk.DISABLED)
            return
        state = []
        if info.blank:
            state.append("空白盘")
        elif info.closed:
            state.append("已封盘")
        elif info.sessions > 0:
            state.append("%d 个区段，可续刻" % info.sessions)
        else:
            state.append("可续刻")
        text = "光盘：%s（%s）" % (info.profile, "，".join(state))
        self.lbl_disc.config(text=text)
        cap = "容量：已用 %s，剩余可用 %s" % (fmt_size(info.used or 0), fmt_size(info.free or 0))
        self.lbl_capacity.config(text=cap)
        # 按当前光盘/光驱支持的倍速更新下拉框
        speeds = ["%sx" % s for s in (info.speeds or [])]
        self.cmb_speed.config(values=["默认"] + speeds)
        if self.var_speed.get() not in (["默认"] + speeds):
            self.var_speed.set("默认")
        self.btn_burn.config(
            state=tk.DISABLED if (info.closed or self.burning) else tk.NORMAL)
        # 可擦除判断：drutil Writability 含 erasable/overwritable，或介质名含 RW/RAM/RE
        profile = (info.profile or "").upper()
        is_rw = info.erasable or bool(
            re.search(r"(?:^|[^A-Z])(?:RW|RAM|RE)(?:[^A-Z]|$)", profile))
        self._set_erase_state(
            tk.DISABLED if (not is_rw or self.burning) else tk.NORMAL)

    def _set_erase_state(self, state):
        self.btn_erase.config(state=state)
        self.btn_erase_full.config(state=state)

    # ----- 擦除（快速 / 完全） -----

    def quick_erase(self, full=False):
        if self.burning:
            return
        if self.disc_info is None or self.disc_info.error:
            messagebox.showwarning("opticalBurner", "请先刷新并确认光盘状态")
            return
        kind = "完全擦除（整盘写零，耗时长）" if full else "快速擦除（清空 TOC/PMA，耗时短）"
        if not messagebox.askyesno(
                "opticalBurner",
                "将对 %s 进行%s，盘上数据将被清除，确定继续？"
                % (self.disc_info.profile or "光盘", kind)):
            return

        info = self.disc_info
        self.burning = True
        self._set_erase_state(tk.DISABLED)
        self.btn_burn.config(state=tk.DISABLED)
        self.btn_eject.config(state=tk.DISABLED)
        self.progress.config(value=0)
        self.var_status.set("准备擦除…")

        def progress_cb(text, pct=None):
            def apply():
                self.var_status.set(text)
                if pct is not None:
                    self.progress.config(value=max(0, min(100, pct)))
            self._post(apply)

        def work():
            try:
                ok, err = quick_erase(info, progress_cb, full=full)
            except Exception as e:
                ok, err = False, "异常：%s" % e

            def finish():
                self.burning = False
                self._set_erase_state(tk.NORMAL)
                self.btn_burn.config(state=tk.NORMAL)
                self.btn_eject.config(state=tk.NORMAL)
                if ok:
                    self.progress.config(value=100)
                    self.var_status.set("擦除完成")
                    messagebox.showinfo("opticalBurner", "擦除完成")
                    self.refresh_disc()
                else:
                    self.progress.config(value=0)
                    self.var_status.set("擦除失败")
                    messagebox.showerror("opticalBurner", "擦除失败：\n%s" % err)
            self._post(finish)

        threading.Thread(target=work, daemon=True).start()

    # ----- 弹出光盘 -----

    def eject_disc(self):
        """弹出光盘（光驱空闲时可用）"""
        if self.burning:
            return
        self.btn_eject.config(state=tk.DISABLED)
        self.var_status.set("正在弹出光盘…")

        def work():
            ok, err = eject_disc()

            def finish():
                self.btn_eject.config(state=tk.NORMAL)
                if ok:
                    self.var_status.set("光盘已弹出")
                    self.refresh_disc()
                else:
                    self.var_status.set("弹出失败")
                    messagebox.showerror("opticalBurner", "弹出失败：\n%s" % err)
            self._post(finish)

        threading.Thread(target=work, daemon=True).start()

    def _eject_after_burn(self):
        """刻录成功后自动弹出（勾选"刻录成功后弹出光盘"时调用）"""
        self.var_status.set("刻录完成，正在弹出光盘…")
        self.btn_eject.config(state=tk.DISABLED)

        def work():
            ok, err = eject_disc()

            def finish():
                self.btn_eject.config(state=tk.NORMAL)
                if ok:
                    self.var_status.set("刻录完成，光盘已弹出")
                    self.refresh_disc()
                else:
                    self.var_status.set("刻录完成，但弹出失败")
                    messagebox.showerror(
                        "opticalBurner", "刻录成功，但弹出光盘失败：\n%s" % err)
            self._post(finish)

        threading.Thread(target=work, daemon=True).start()

    # ----- 文件选择 -----

    def add_files(self):
        paths = filedialog.askopenfilenames(title="选择要刻录的文件")
        for p in paths:
            self._add_path(p)

    def add_dir(self):
        p = filedialog.askdirectory(title="选择要刻录的文件夹")
        if p:
            self._add_path(p)

    def _add_path(self, path):
        path = os.path.abspath(path)
        # 去重
        for item in self.tree.get_children():
            if self.tree.item(item, "values")[0] == path:
                return
        size = 0
        if os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        size += os.path.getsize(fp)
                    except OSError:
                        pass
        else:
            size = os.path.getsize(path)
        self.tree.insert("", tk.END, values=(path, fmt_size(size)))
        self.selected_files.append(path)
        self._update_files_total()

    def remove_selected(self):
        for item in self.tree.selection():
            path = self.tree.item(item, "values")[0]
            if path in self.selected_files:
                self.selected_files.remove(path)
            self.tree.delete(item)
        self._update_files_total()

    def _clear_file_list(self):
        """清空文件选择列表（刻录成功后调用）"""
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.selected_files = []
        self._update_files_total()

    def _update_files_total(self):
        total = 0
        for item in self.tree.get_children():
            v = self.tree.item(item, "values")
            s = v[1]
            # 从显示字符串还原太麻烦，直接重算
        for p in self.selected_files:
            if os.path.isdir(p):
                for root, dirs, files in os.walk(p):
                    for f in files:
                        try:
                            total += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            pass
            else:
                try:
                    total += os.path.getsize(p)
                except OSError:
                    pass
        n = len(self.selected_files)
        self.lbl_files_total.config(text="共 %d 个文件/文件夹，%s" % (n, fmt_size(total)))

    # ----- 依赖检查 -----

    def check_deps(self):
        missing = [n for n in ("mkisofs", "cdrecord", "growisofs") if not find_tool(n)]
        if missing:
            self.lbl_deps.config(
                text="⚠ 缺少依赖：%s。请执行：%s" % ("、".join(missing), BREW_INSTALL_CMD),
                foreground="#c00000")
            self.btn_burn.config(state=tk.DISABLED)
        else:
            self.lbl_deps.config(text="✓ 依赖完整（cdrtools / dvd+rw-tools）", foreground="#007000")

    # ----- 刻录 -----

    def start_burn_after_erase(self):
        """RW 盘已封盘：先快速擦除，再按空盘流程重新走刻录"""
        info = self.disc_info

        def progress_cb(text, pct=None):
            def apply():
                self.var_status.set(text)
                if pct is not None:
                    self.progress.config(value=max(0, min(100, pct)))
            self._post(apply)

        def work():
            ok, err = quick_erase(info, progress_cb)
            if not ok:
                def fail():
                    self.burning = False
                    self._set_erase_state(tk.NORMAL)
                    self.btn_burn.config(state=tk.NORMAL)
                    self.btn_eject.config(state=tk.NORMAL)
                    self.progress.config(value=0)
                    self.var_status.set("擦除失败")
                    messagebox.showerror("opticalBurner", "擦除失败：\n%s" % err)
                self._post(fail)
                return
            # 擦除成功 → 重新探测光盘，再走正常空盘刻录流程
            self._post(self._burn_after_erase_ready)

        threading.Thread(target=work, daemon=True).start()

    def _burn_after_erase_ready(self):
        """擦除完成后的回调：重新探测光盘，然后按空盘流程重新走刻录"""
        info = get_disc_info()
        info.blank = True  # 擦除成功，按空盘流程
        self.disc_info = info
        self.burning = False
        self._update_disc(info)
        self.start_burn()

    def start_burn(self):
        if self.burning:
            return
        if not self.selected_files:
            messagebox.showwarning("opticalBurner", "请先选择要刻录的文件")
            return
        if self.disc_info is None or self.disc_info.error:
            messagebox.showwarning("opticalBurner", "请先刷新并确认光盘状态")
            return
        if self.disc_info.closed:
            # RW 盘已封盘：自动走"快速擦除 → 空盘刻录"流程
            profile = (self.disc_info.profile or "").upper()
            is_rw = self.disc_info.erasable or bool(
                re.search(r"(?:^|[^A-Z])(?:RW|RAM|RE)(?:[^A-Z]|$)", profile))
            if is_rw:
                if not messagebox.askyesno(
                        "opticalBurner",
                        "光盘已封盘。将先快速擦除 RW 盘再按空盘流程刻录，确定继续？"):
                    return
                self.start_burn_after_erase()
                return
            messagebox.showwarning("opticalBurner", "光盘已封盘，无法再刻录")
            return
        if not self.var_multi.get() and not self.disc_info.blank:
            if not messagebox.askyesno(
                    "opticalBurner",
                    "对已有多区段的光盘取消 -multi 将会在本次刻录后封盘，确定继续？"):
                return

        files = list(self.selected_files)
        speed_sel = self.var_speed.get().strip()
        burn_speed = None if speed_sel in ("", "默认") else speed_sel.rstrip("xX")
        job = BurnJob(
            files=files,
            disc_info=self.disc_info,
            multi=self.var_multi.get(),
            volume_label=self.var_label.get().strip() or default_label(),
            burn_speed=burn_speed,
        )

        self.burning = True
        self.btn_burn.config(state=tk.DISABLED)
        self._set_erase_state(tk.DISABLED)
        self.btn_eject.config(state=tk.DISABLED)
        self.progress.config(value=0)
        self.var_status.set("准备刻录…")

        def progress_cb(text, pct=None):
            def apply():
                self.var_status.set(text)
                if pct is not None:
                    self.progress.config(value=max(0, min(100, pct)))
            self._post(apply)

        def work():
            try:
                ok, err = do_burn(job, progress_cb)
            except Exception as e:
                ok, err = False, "异常：%s" % e

            def finish():
                self.burning = False
                self.btn_burn.config(state=tk.NORMAL)
                self.btn_eject.config(state=tk.NORMAL)
                if ok:
                    self.progress.config(value=100)
                    self.var_status.set("刻录完成")
                    if err:
                        messagebox.showinfo("opticalBurner",
                                            "刻录完成，但请注意：\n\n%s" % err)
                    else:
                        messagebox.showinfo("opticalBurner", "刻录完成")
                    self._clear_file_list()  # 刻录成功，清空已选文件
                    self.refresh_disc()
                    if self.var_eject_after.get():
                        self._eject_after_burn()
                else:
                    self.progress.config(value=0)
                    self.var_status.set("刻录失败")
                    messagebox.showerror("opticalBurner", "刻录失败：\n%s" % err)
            self._post(finish)

        threading.Thread(target=work, daemon=True).start()


def main():
    # 上次异常退出可能遗留暂停的 diskarbitrationd（光驱会被锁死），启动时恢复
    try:
        if diskarb_is_paused():
            diskarb_cont()
    except Exception:
        pass
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
