---
name: KVM
description: 经 GLKVM 让 AI 操控一台真 iPhone：外卖 / 闪购比价、查机票酒店、查快递、看 App 信息。命令 rmq（排队交给执行机常驻跑，做完推飞书）与 kp（直接点屏读屏）。触发词：KVM、RMQ1、rmq、kp、控手机、操作手机、用手机查、手机比价、让手机去做。
---

# KVM · AI 操控真 iPhone（Claude Code 技能）

把本文件放到 `~/.claude/skills/KVM/SKILL.md`，Claude Code 就会在你说「用手机查一下…」时按这里的方法调用 `rmq` / `kp`。

GLKVM（GL.iNet Comet 一类 IP-KVM）把 iPhone 画面经 HDMI 采进来、用 USB HID 当键鼠发回去。EGO LITE 浏览器打开 GLKVM 网页，`kvmphone/bridge.mjs` 常驻在页面里从 `<video>` 抓帧，苹果 Vision OCR 读字，`kp` 按文字点。

## 两种用法

**1. 交一个完整任务（默认）**：`rmq ask -t "<10 字标题>" "<把要求写完整：平台、地址、时间、硬条件、要比什么>"`，打印任务 id 就结束，不要等。只读任务十几秒，多平台比价几分钟，做完推飞书。
- 查：`rmq ls`、`rmq show <id>`；取消排队：`rmq cancel <id>`；自检：`rmq doctor`。
- 跑着时补一句：`rmq say <id> "话"`；叫停：`rmq stop <id> [原因]`（手机马上停、已拿到的照常交付）。
- 换驾驶模型：默认 Gemini 3.8 Flash（low，执行机本机跑 agy，带预热池）；`--driver gemini-3.8-flash-high` 换高思考档；`--driver cc` 走原生 Claude Code 驱动（claude -p + DeepSeek 兼容端点）。
- 一台手机一条队列，任务串行。

**2. 自己一步步点（调试 / 很短的操作）**：`kp look`（读屏 + 判屏）→ `kp open 小红书 关键词`（直达搜索结果）/ `kp tap 文字` / `kp tap X Y` / `kp up` / `kp key Meta+KeyH`（回主屏）。坐标是手机裁图像素（宽约 500、高 1080）。先用文字判断，看图（`kp shot`）只兜底。**有 rmq 任务在跑时不要手动 kp**：同一台手机按租约排他，被占会退出码 75。

## 边界

- **只有付钱要用户确认**：提交订单、付款、立即购买、开通月付或金融服务——kp 拒点（退出码 3），任务停在付款前把金额交给用户。
- 发消息、分享、填信息、新增地址、加乘机人都直接做；收件人和内容严格照原话，发送前核对聊天顶部名字。
- 购物车里原有的东西不删、不改数量，只撤销自己加的。
- 权限弹窗一律点允许；补贴 / 红包 / 免费券都领；要花钱的不领。
- 中文用 `kp paste 中文`（自动切拼音、点候选、切回英文）；英文 `kp type`。

## 排障

- **看不到手机画面**：GLKVM 掉登录。跑一次 `kvmphone/glkvmpass.sh` 存密码（600 文件），桥会自动重登。
- **整屏黑**：手机在加载或息屏；`kp key Meta+KeyH` 回主屏，息屏 HID 唤不醒，要人点亮。
- **桥卡住**：`kp stop` 再 `kp start`。
- **点击横向全偏**：iPhone 的绝对坐标映射变了，devices.json 里 `mouse` 在 `phone` / `frame` 之间切换后 `kp stop`。
- **飞书机器人不回 / 慢**：看 OpenClaw 日志有没有 `SsrFBlockedError`（局域网 provider 要加 `request.allowPrivateNetwork: true`）。
