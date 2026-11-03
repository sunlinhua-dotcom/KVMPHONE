#!/bin/sh
# subgate-mini：在执行机（Mac mini）上把 subgate（CLIProxyAPI :8317）装成登录后常驻的 LaunchAgent，
# 飞书机器人（OpenClaw rmq1）就走执行机那条线连谷歌。在入口机上跑。
# 前提：执行机上已经放好 CLIProxyAPI 程序、~/.config/subgate/cliproxy.yaml 和 auth/ 登录凭据；
#       入口机上有同一个 client key：~/.config/subgate/client_key（600）。
# 用法：deploy/subgate-mini.sh        开启常驻并自测
#       deploy/subgate-mini.sh off    停掉并卸载
# 可用环境变量覆盖：MINI（ssh 别名）、MINI_LAN_HOST（局域网主机名）、CLIPROXY_BIN（执行机上的程序路径）
set -eu
MINI=${MINI:-macmini}
MINI_LAN_HOST=${MINI_LAN_HOST:-your-mini.local}
LABEL=local.rmq1.subgate
PL=Library/LaunchAgents/$LABEL.plist
if [ "${1:-}" = "off" ]; then
  ssh "$MINI" "launchctl bootout gui/\$(id -u)/$LABEL 2>/dev/null; rm -f ~/$PL"
  echo "执行机上的 subgate 已停掉并卸载；飞书机器人会自动回落到本机 subgate。"
  exit 0
fi
MH=$(ssh "$MINI" 'echo $HOME')
BIN=${CLIPROXY_BIN:-$MH/.local/opt/cliproxyapi/cli-proxy-api}
echo "1/3 在执行机上写常驻配置…"
ssh "$MINI" "mkdir -p ~/Library/LaunchAgents ~/Library/Logs/subgate; cat > ~/$PL" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$LABEL</string>
<key>ProgramArguments</key><array><string>$BIN</string><string>-config</string><string>$MH/.config/subgate/cliproxy.yaml</string></array>
<key>WorkingDirectory</key><string>$MH/.config/subgate</string>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>$MH/Library/Logs/subgate/out.log</string>
<key>StandardErrorPath</key><string>$MH/Library/Logs/subgate/err.log</string>
</dict></plist>
EOF
echo "2/3 启动…"
ssh "$MINI" "launchctl bootout gui/\$(id -u)/$LABEL 2>/dev/null; launchctl bootstrap gui/\$(id -u) ~/$PL"
sleep 5
echo "3/3 自测（经执行机调一次 Gemini 3.8 Flash）…"
K=$(cat "$HOME/.config/subgate/client_key")
if curl -s --noproxy '*' -m 40 -H "Authorization: Bearer $K" -H "Content-Type: application/json" \
     -d '{"model":"gemini-3.8-flash-high","messages":[{"role":"user","content":"只回一个字：好"}],"max_tokens":20,"reasoning_effort":"low"}' \
     "http://$MINI_LAN_HOST:8317/v1/chat/completions" | grep -q '"choices"'; then
  echo "好了：执行机上的 subgate 常驻在跑，飞书机器人现在优先走执行机。"
else
  echo "自测没通过：执行机上看日志 ~/Library/Logs/subgate/err.log；飞书机器人会自动回落到本机 subgate，不影响使用。"
  exit 1
fi
