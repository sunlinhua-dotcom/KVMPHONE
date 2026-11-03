#!/bin/bash
# 等手机屏幕亮起（帧不再全黑），最多 $1 秒
ok=0
end=$(( $(date +%s) + ${1:-1800} ))
while [ $(date +%s) -lt $end ]; do
  curl -s 127.0.0.1:7690/frame >/dev/null
  m=$(python3 -W ignore -c "from PIL import Image; im=Image.open('/tmp/kvmphone/last.jpg').convert('L').resize((64,36)); print(int(sum(im.getdata())/2304))")
  if [ "$m" -gt 8 ]; then ok=$((ok+1)); else ok=0; fi
  if [ "$ok" -ge 3 ]; then echo "屏幕已持续亮起 mean=$m"; exit 0; fi
  sleep 5
done
echo "超时，屏幕仍黑"; exit 1
