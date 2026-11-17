#!/bin/zsh
# opticalBurner 双击启动脚本
# 自动选择 Tk 版本 >= 8.6 的 Python（系统自带 /usr/bin/python3 的 Tk 8.5 在 macOS 26+ 上渲染空白）
cd "$(dirname "$0")"

PY=""
for cand in \
  /opt/homebrew/bin/python3.13 \
  /opt/homebrew/bin/python3.12 \
  /opt/homebrew/bin/python3 \
  /usr/local/bin/python3.13 \
  /usr/local/bin/python3.12 \
  /usr/local/bin/python3 \
  /usr/bin/python3
do
  [ -x "$cand" ] || continue
  if "$cand" -I -c 'import tkinter, sys; sys.exit(0 if tkinter.TkVersion >= 8.6 else 1)' 2>/dev/null; then
    PY="$cand"
    break
  fi
done

if [ -z "$PY" ]; then
  osascript -e 'display alert "opticalBurner" message "未找到带 Tk 8.6+ 的 Python。请先运行：brew install python-tk@3.13" as critical'
  exit 1
fi

# 程序退出后自动关闭本终端窗口
# （Terminal 默认只在 shell 退出后显示"进程已完成"并保留窗口，这里主动关掉自己）
"$PY" opticalBurner.py
osascript <<'ASC' >/dev/null 2>&1 &
tell application "Terminal"
	repeat with w in windows
		if name of w contains "启动光盘刻录" then close w
	end repeat
end tell
ASC
exit $?
