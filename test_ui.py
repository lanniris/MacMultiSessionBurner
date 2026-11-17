#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""opticalBurner UI 自检（无头运行，monkeypatch 掉模态对话框）"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opticalBurner as ob

# monkeypatch 模态对话框，记录调用而不挂起
dialog_calls = []
ob.messagebox.showwarning = lambda *a, **k: dialog_calls.append(("warning", a))
ob.messagebox.showerror = lambda *a, **k: dialog_calls.append(("error", a))
ob.messagebox.showinfo = lambda *a, **k: dialog_calls.append(("info", a))
ob.messagebox.askyesno = lambda *a, **k: dialog_calls.append(("askyesno", a)) or False

app = ob.App()
app.update()
for _ in range(20):
    app.update()
    time.sleep(0.05)

checks = []


def check(name, cond, detail=""):
    checks.append((name, cond, detail))


check("刻录按钮存在", app.btn_burn.winfo_exists())
check("进度条存在", app.progress.winfo_exists())
check("文件树存在", app.tree.winfo_exists())
check("卷标默认值", app.var_label.get() == "OPTICALBURNER", app.var_label.get())
check("-multi 默认勾选", app.var_multi.get() is True)
check("探测完成（无盘提示）",
      "光驱中没有放入光盘" in app.lbl_disc.cget("text")
      or "光盘" in app.lbl_disc.cget("text"), app.lbl_disc.cget("text"))
check("依赖提示（缺cdrtools）", "缺少依赖" in app.lbl_deps.cget("text"), app.lbl_deps.cget("text"))
check("刻录按钮在缺依赖时禁用", str(app.btn_burn.cget("state")) == "disabled",
      app.btn_burn.cget("state"))

# 添加文件
tf = tempfile.NamedTemporaryFile(suffix=".txt", delete=False)
tf.write(b"x" * 1024)
tf.close()
app._add_path(tf.name)
vals = app.tree.item(app.tree.get_children()[0], "values")
check("添加文件到树", vals[0] == tf.name, str(vals))
check("文件数标签", "1 个" in app.lbl_files_total.cget("text"), app.lbl_files_total.cget("text"))

# 空白盘显示
info = ob.DiscInfo()
info.profile = "DVD-Recordable"
info.blank = True
info.free = 4380 * 1024 ** 2
info.capacity = info.free
app._update_disc(info)
check("空白盘状态显示", "空白盘" in app.lbl_disc.cget("text"), app.lbl_disc.cget("text"))
check("容量显示", "剩余可用" in app.lbl_capacity.cget("text"), app.lbl_capacity.cget("text"))
check("空白盘恢复刻录按钮", str(app.btn_burn.cget("state")) == "normal",
      app.btn_burn.cget("state"))

# 封盘禁用
info2 = ob.DiscInfo()
info2.profile = "DVD-Recordable"
info2.closed = True
info2.used = 2 * 1024 ** 3
info2.free = 0
info2.capacity = info2.used
app._update_disc(info2)
check("已封盘禁用刻录按钮", str(app.btn_burn.cget("state")) == "disabled",
      app.btn_burn.cget("state"))

# 空文件拒绝刻录（start_burn 内部应走 warning 分支而非挂起）
app.selected_files = []
app.start_burn()
check("空文件不启动刻录", app.burning is False)
check("空文件弹出警告", any(c[0] == "warning" for c in dialog_calls),
      str(dialog_calls))

os.unlink(tf.name)
print("--- UI 自检 ---")
fails = [c for c in checks if not c[1]]
for name, cond, detail in checks:
    print(("PASS" if cond else "FAIL"), name, ("| " + detail if detail and not cond else ""))
print()
print("共 %d 项，失败 %d 项" % (len(checks), len(fails)))
app.destroy()
sys.exit(1 if fails else 0)
