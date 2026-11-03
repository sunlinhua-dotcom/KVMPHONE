---
name: ios
display: iOS 系统
description: 主屏、设置与系统弹窗
aliases: ["设置", "Settings", "主屏", "系统"]
brand: ["Privacy & Security", "隐私与安全性"]
detect: ["General", "Bluetooth", "Wi-Fi", "Cellular", "Screen Time", "通用", "蓝牙", "无线局域网", "蜂窝网络", "屏幕使用时间"]
open: 回主屏 kp key Meta+KeyH；设置在主屏点 Settings 图标
---
## 是什么
这台 iPhone 系统语言是英文，自动锁定设为永不。

## 界面
- 主屏第一页约 19 个图标（Calendar、Photos、Messages、印象笔记等）。App 名字在图标下方，点名字不会打开，要点图标本身。
- Cmd+H（kp key Meta+KeyH）回主屏；Cmd+Space 打开 Spotlight，但 Spotlight 会翻出邮件、短信等隐私内容。
- 从左边缘右滑返回在 KVM 上不生效，返回点左上角「<」（约 28,88）。

## 已知现象
- Safari 输入 App 的 URL scheme 回车后，会弹「Open this page in "App"?」，点 Open 才跳转。
- 息屏后 HID 唤不醒，要用户点亮。
