#!/bin/bash
# kpcert：查看 / 重新登记 GLKVM 证书指纹（bridge 只在指纹与登记一致时放行自签证书，见 glkvm_cert.mjs）
# 用法：在 Mac 终端敲 kpcert。指纹和登记一致就只报状态；不一致时显示新旧指纹，你确认是设备自己换的证书（比如 GLKVM 重置过）才登记。
set -u
HOST=glkvm.local
PINF='~/rmq1/kvmphone/.glkvm_cert_pin'
LOCALPIN="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)/.glkvm_cert_pin"

now=$(echo | openssl s_client -connect $HOST:443 2>/dev/null | openssl x509 -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)
if [ -z "$now" ]; then
  echo "连不上 GLKVM（$HOST:443）。先确认 GLKVM 通电、和 Mac 在同一局域网。"; exit 1
fi
pin=$(ssh -o ConnectTimeout=5 macmini "cat $PINF 2>/dev/null" | tr -d '[:space:]')
mis=$(ssh -o ConnectTimeout=5 macmini 'cat /tmp/kvmphone/cert_mismatch.json 2>/dev/null')

echo "GLKVM 现在的证书指纹：$now"
echo "mini 上登记的指纹：  ${pin:-（没有登记）}"
[ -n "$mis" ] && echo "最近一次拒绝放行记录：$mis"

if [ "$now" = "$pin" ]; then
  echo "一致。bridge 会在授权过期时自动续上，不用做什么。"
  exit 0
fi

echo
echo "不一致。只有你确认 GLKVM 最近重置过 / 换过固件，才登记新指纹；拿不准就别登记，先查清楚局域网里是谁在冒充。"
read -r -p "确认登记新指纹？输入 yes 登记，其他任何输入取消：" ans
if [ "$ans" != "yes" ]; then echo "已取消，没有改动。"; exit 0; fi
ssh macmini "printf '%s\n' '$now' > $PINF && chmod 600 $PINF && rm -f /tmp/kvmphone/cert_mismatch.json" || { echo "写 mini 失败（ssh macmini 不通？）"; exit 1; }
printf '%s\n' "$now" > "$LOCALPIN" && chmod 600 "$LOCALPIN"
echo "已登记。重发任务时 bridge 会自动放行。"
