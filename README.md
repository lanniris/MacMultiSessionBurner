# MacMultiSessionBurner — macOS 光盘多区段续刻工具

（内部脚本名：opticalBurner）

参考 [macOS以及Linux系统中光盘续刻问题](https://www.simaek.com/archives/485/) 实现的带简单图形界面的多区段刻录工具。

## 功能

- 选择本次要刻录的文件（支持多选），自动打包成 ISO
- 显示当前光盘状态（空白 / 多区段 / 已封盘）、已用容量、剩余可用容量
- 刻录确认按钮与进度条
- **刻录速度选择**："开始刻录"按钮旁下拉框，列出当前光盘/光驱支持的倍速（来自 `drutil status` 的 Write Speeds）
- 自动识别 CD / DVD，选择对应刻录路径：
  - CD：`mkisofs` 制作镜像 + `cdrecord -multi` 刻录（续刻用 `-msinfo`/`-C` 合并）
  - DVD：`growisofs -Z`（首次）/ `-M`（续刻，见下方 mkisofs 包装器说明）
- 依赖检测：工具启动时检查 cdrtools / dvd+rw-tools 是否安装，未安装时显示安装命令

## mkisofs 包装器（DVD 续刻必需）

Homebrew 的 mkisofs 缺少 dvd+rw-tools 官方补丁，不支持 `growisofs -M` 传入的
"从 stdin 读旧镜像"（`-M -`），导致 DVD 续刻报
`Unable to open previous session image '-'`。

本工具附带 `mkisofs_wrap/mkisofs` 包装器：把 stdin 落盘为临时文件后再调用真正的
mkisofs，通过 `MKISOFS` 环境变量注入给 growisofs，从而让 DVD±RW 续刻正常工作
（每次续刻把历史文件合并进新镜像写回盘上）。

## 依赖

```bash
brew install cdrtools dvd+rw-tools
```

## 运行

双击 `启动光盘刻录.command` 即可（自动选择 Tk ≥ 8.6 的 Python）。

**注意**：macOS 26/27 上系统自带 `/usr/bin/python3` 的 Tk 8.5 会渲染出空白窗口，
需要 Tk ≥ 8.6 的解释器：

```bash
brew install python-tk@3.13
```

启动脚本会优先使用 `/opt/homebrew/bin/python3.13`。

## 擦除 RW 盘（复用已封盘的 DVD±RW / CD-RW）

封盘的 RW 盘要当新盘用，**快速擦除即可**：

- **快速擦除**：清空 TOC/PMA/pregap，盘变为空白可重新刻录；CD-RW 约 1–2 分钟，
  DVD±RW 类似。日常复用用这个
- **完全擦除**：整盘写零，耗时 15–30 分钟以上。仅用于：① 彻底销毁数据防恢复；
  ② 快速擦除后盘状态异常、驱动器不认时

擦除命令：CD-RW 用 `cdrecord blank=fast|all`，DVD±RW 用 `dvd+rw-format -blank|-blank=full`。

## 给 Linux 使用（重要）

本工具用 `mkisofs -Jrv` 刻录，产出 **ISO 9660 + Rock Ridge + Joliet**，Linux 原生可读。
而 macOS 的 Finder / "Burn" 等软件默认刻 **HFS** 文件系统，Linux 基本读不了
（`hfsplus` 模块默认未启用），这就是盘在 Linux 上"不识别"的常见原因。

**不需要为了 Linux 封盘。** Linux 读的是"最后一个完整关闭的区段"：

- `-multi` 模式每次刻录都会写完 lead-out、正确关闭当前区段（只是整盘保持可追加），
  因此 Linux 可以随时读取
- 如果旧区段是本工具刻的（ISO 9660），续刻时会把历史文件合并进新区段
  （`mkisofs -M -C`），**最后一个区段就是全量视图**，Linux 默认挂载即可看到所有文件
- 如果旧区段不是 ISO 9660（例如之前用 Burn 软件刻的 HFS），无法合并，
  工具会自动降级为"仅新文件"区段——Linux 读最后区段时只能看到本次新增文件
- 因此可以像 U 盘一样小批量持续追加，直到盘用完，不浪费光盘
- 只有"刻录被中断、区段未关闭"的盘才可能读不出；封盘（取消勾选 -multi）
  只是不想再加刻时的稳妥选择，不是 Linux 的硬要求

Linux 挂载命令：

```bash
mount -t iso9660 -o ro /dev/sr0 /mnt              # 默认最后一个区段（全量视图）
mount -t iso9660 -o ro,session=1 /dev/sr0 /mnt    # 如需读取更早的区段
```

（Linux 不会像 Windows 那样显示合并视图，但最后一区段已包含全部历史文件。）

## 注意

- macOS 的 `diskarbitrationd` 会锁住光驱，`cdrecord`/`growisofs` 直接操作设备时可能报
  "diskarbitrationd 不允许向驱动器发送 SCSI 命令" 或
  "unable to ObtainExclusiveAccess"。GUI 在刻录/擦除 CD 时自动 STOP/CONT
  `diskarbitrationd`。
- **免密授权（推荐）**：首次刻录时会弹一次系统授权框，工具会安装
  `/etc/sudoers.d/opticalburner`（仅允许 `pkill -STOP/-CONT -x diskarbitrationd`
  两条命令），之后刻录不再需要输密码。删除该文件即可撤销。
- **CD-R 上的 HFS 区段无法转换为 ISO 9660**：已用其他软件刻了 HFS 的盘，
  只能把文件拷出来，换一张空白盘用本工具重刻。
