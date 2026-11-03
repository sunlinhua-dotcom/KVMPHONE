#!/bin/bash
# glkvmpass：把 GLKVM 登录密码存进本机和 Mac mini 的 600 文件，桥在 GLKVM 重启后用它自动重登。
# 密码只在这里由用户本人输入，不进命令行参数、不打印、不进日志。
set -u
F="$HOME/.config/kvmphone/glkvm_pass"
HOST=https://glkvm.local

echo "存 GLKVM 登录密码（就是网页 https://glkvm.local 的登录密码）。"
echo "输入时屏幕不显示是正常的，输完按回车。"
read -rs -p "GLKVM 密码：" PW; echo
[ -n "$PW" ] || { echo "没输入，什么都没改。"; exit 1; }

mkdir -p "$(dirname "$F")"; chmod 700 "$(dirname "$F")"
TMP=$(mktemp "$(dirname "$F")/.try.XXXXXX"); chmod 600 "$TMP"
printf '%s' "$PW" > "$TMP"; unset PW

# 先试登一次，确认密码对再存（只试一次，GLKVM 失败多次会锁登录）
R=$(curl -sk -m 15 -F user=admin -F "passwd=<$TMP" "$HOST/api/auth/login")
if ! printf '%s' "$R" | grep -q '"ok": *true'; then
  rm -f "$TMP"
  echo "登录没通过，密码没存。常见原因：密码输错；失败次数太多被锁（等提示的秒数再试）；GLKVM 不在线。"
  printf '%s\n' "$R" | grep -o '"error_msg": *"[^"]*"' | head -1
  exit 2
fi
mv -f "$TMP" "$F"; chmod 600 "$F"
echo "本机已存：$F"

if ssh -o ConnectTimeout=5 macmini 'umask 077; mkdir -p ~/.config/kvmphone && cat > ~/.config/kvmphone/glkvm_pass' < "$F"; then
  echo "Mac mini 也已存，重启那边的手机桥："
  ssh macmini 'export PATH=$HOME/.local/bin:/opt/homebrew/bin:$PATH; kp stop >/dev/null 2>&1; kp start'
else
  echo "Mac mini 没连上，那边没存（mini 的 RMQ1 任务掉登录时还会失败）。"
fi

kp stop >/dev/null 2>&1; kp start
