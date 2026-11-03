#!/bin/sh
# rmq-mini：把 RMQ1 的手机执行搬到 Mac mini（mini 跑 JEV + rmq worker，本机只做入口与交付中继）。
# 用法：rmq-mini        检查前置条件，全部满足就切过去
#       rmq-mini back   切回本机执行
set -eu
M=macmini
HERE=$(dirname "$(python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$0")")
say() { printf '%s\n' "$*"; }
if [ "${1:-}" = back ]; then
  ssh $M 'launchctl bootout gui/$(id -u)/local.rmq1.rmq 2>/dev/null; rm -f ~/Library/LaunchAgents/local.rmq1.rmq.plist' || true
  python3 - <<'P'
import json,os;p=os.path.expanduser("~/.rmq/config.json");c=json.load(open(p));c.pop("remote",None);json.dump(c,open(p,"w"),ensure_ascii=False)
P
  sh "$HERE/install-service.sh" on worker; say "已切回本机执行。"; exit 0
fi
say "1/4 检查 mini 上的 EGO LITE 首次引导……"
if ssh $M 'export PATH=$HOME/.local/bin:$PATH; ego-browser nodejs -e "1" </dev/null 2>&1' | grep -q onboarding; then
  say "  还没完成：请到 mini 屏幕上把 ego lite 的首次引导点完，再重新运行 rmq-mini。"; exit 1; fi
say "2/4 检查 mini 上的复核模型……"
if ssh $M 'grep -q review_api ~/.rmq/config.json && test -s ~/.config/glm/rmq.json'; then say "  复核走 GLM 5.3 Flash，不用登录 Claude。"
elif ! ssh $M '~/.local/bin/claude auth status 2>/dev/null' | grep -q '"loggedIn": true'; then
  say "  还没登录：请在 mini 上打开终端，输入 claude，按提示登录你的 Claude 账号，登录完退出，再重新运行 rmq-mini。"; exit 1; fi
say "2.5 检查 mini 能不能看到手机画面……"
if ! ssh $M 'export PATH=$HOME/.local/bin:/opt/homebrew/bin:$PATH; kp look 2>&1' | grep -qE '^[0-9]+,[0-9]+'; then
  say "  看不到：请在 mini 的 ego lite 里打开 https://glkvm.local ，信任证书并登录 GLKVM，能看到手机画面后再运行 rmq-mini。"; exit 1; fi
say "3/4 在 mini 上装 JEV 与 rmq worker 两个常驻服务……"
ssh $M 'set -e; U=$(id -u); LA=~/Library/LaunchAgents; mkdir -p $LA ~/.rmq ~/Library/Logs/jevjudge
if [ -f ~/rmq1/jevjudge/server.mjs ]; then  # JEV 判屏服务可选，不在本仓库里；没有就只装 worker
cat > $LA/local.rmq1.jevjudge.plist <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>Label</key><string>local.rmq1.jevjudge</string>
<key>ProgramArguments</key><array><string>/opt/homebrew/bin/node</string><string>$HOME/rmq1/jevjudge/server.mjs</string></array>
<key>WorkingDirectory</key><string>$HOME/rmq1/jevjudge</string>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin</string><key>JEV_PROXY</key><string>http://127.0.0.1:7890</string></dict>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>10</integer>
<key>StandardOutPath</key><string>$HOME/Library/Logs/jevjudge/out.log</string><key>StandardErrorPath</key><string>$HOME/Library/Logs/jevjudge/err.log</string>
</dict></plist>
PL
launchctl bootout gui/$U/local.rmq1.jevjudge 2>/dev/null || true; for i in 1 2 3 4 5 6 7 8 9 10; do launchctl print gui/$U/local.rmq1.jevjudge >/dev/null 2>&1 || break; sleep 1; done
for i in 1 2 3; do launchctl bootstrap gui/$U $LA/local.rmq1.jevjudge.plist 2>/dev/null && break; sleep 2; done; launchctl print gui/$U/local.rmq1.jevjudge >/dev/null
fi
sh ~/rmq1/rmq/install-service.sh on worker'
say "4/4 本机切成入口 + 交付中继……"
launchctl bootout "gui/$(id -u)/local.rmq1.rmq" 2>/dev/null || true
python3 - <<'P'
import json,os;p=os.path.expanduser("~/.rmq/config.json");c=json.load(open(p));c["remote"]="macmini";json.dump(c,open(p,"w"),ensure_ascii=False)
P
sh "$HERE/install-service.sh" on relay
sleep 8; ssh $M 'curl -s -m 5 127.0.0.1:7677/health' | grep -q '"ok":true' && say "完成：mini 的 JEV 在线，手机任务从现在起在 mini 上执行。" || say "装好了，但 mini 的 JEV 还没响应，过一分钟用 ssh macmini curl 127.0.0.1:7677/health 再看。"
