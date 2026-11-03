#!/bin/sh
# 把 rmq worker（或本机中继 relay）装成本机登录后常驻的 LaunchAgent（本机和 Mac mini 通用）。用法：rmq-service [on [worker|relay] | off | status]
set -eu
SELF=$(python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$0")
RMQ="$(dirname "$SELF")/rmq.py"
PL="$HOME/Library/LaunchAgents/local.rmq1.rmq.plist"
MODE=${2:-worker}
case "${1:-on}" in
on)
  mkdir -p "$HOME/.rmq" "$HOME/Library/LaunchAgents"
  cat > "$PL" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>local.rmq1.rmq</string>
<key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>$RMQ</string><string>$MODE</string></array>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>$HOME/.rmq/worker.log</string><key>StandardErrorPath</key><string>$HOME/.rmq/worker.log</string>
</dict></plist>
PLIST
  launchctl bootout "gui/$(id -u)/local.rmq1.rmq" 2>/dev/null || true
  for i in 1 2 3 4 5 6 7 8 9 10; do launchctl print "gui/$(id -u)/local.rmq1.rmq" >/dev/null 2>&1 || break; sleep 1; done
  for i in 1 2 3; do launchctl bootstrap "gui/$(id -u)" "$PL" 2>/dev/null && break; sleep 2; done
  launchctl print "gui/$(id -u)/local.rmq1.rmq" >/dev/null
  echo "rmq 常驻服务已开：${RMQ} ${MODE}，日志 ~/.rmq/worker.log" ;;
off) launchctl bootout "gui/$(id -u)/local.rmq1.rmq" 2>/dev/null || true; rm -f "$PL"; echo "rmq 常驻服务已关" ;;
status) launchctl list | grep local.rmq1.rmq || echo "没在跑"; tail -3 "$HOME/.rmq/worker.log" 2>/dev/null || true ;;
esac
