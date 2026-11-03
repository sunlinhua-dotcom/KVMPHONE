---
name: wechat
display: 微信
description: 聊天、朋友圈、公众号与小程序
aliases: ["WeChat", "weixin", "微信", "wechat"]
brand: ["WeChat", "文件传输助手", "File Transfer"]
detect: ["微信", "通讯录", "发现", "朋友圈", "Chats", "Contacts", "Discover", "Moments", "按住 说话", "Hold to Talk", "视频号", "Channels"]
open: 主屏点微信图标；或 Safari 地址栏输入 weixin://
pace: careful
source: 界面与会话两节取自 Mika（Apache-2.0），其余为本机实测
---
## 是什么
微信是聊天与社交 App。这台 iPhone 系统语言是英文，微信界面也可能是英文：底部四个标签是 Chats（微信）、Contacts（通讯录）、Discover（发现）、Me（我）。

## 界面
顶部搜索会同时返回联系人、群聊、聊天记录、小程序和公众号，结果按分组显示（Contacts / Group Chats / Chat History…）。

## 功能
- **会话**：未读标记、置顶、草稿、服务通知和普通消息都在列表里，容易混淆。联系人显示名、备注名、群名和头像可能相似，进入会话后顶部标题才是实际对象。输入框里有文字只是草稿，发出后会话里才有本人的气泡。群聊中每条消息有发言人。
- **按微信号找人**：搜索框输入微信号，命中的人显示在 Contacts 分组下，名字是备注或昵称而不是微信号。
- **读群聊**：聊天记录从下往上是由新到旧，看更早的内容用 kp down。
- **公众号**：内容与个人会话是分开的。

## 已知现象
- 中文用 kp paste 走通用剪贴板，粘贴后输入框里能看到完整文字。
