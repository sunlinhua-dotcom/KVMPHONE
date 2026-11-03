---
name: boss-zhipin
display: BOSS直聘
description: 招聘求职 App
aliases: ["BOSS直聘", "Boss直聘", "boss直聘", "BOSS", "直聘", "bosszhipin"]
brand: ["BOSS直聘", "BOSS 直聘"]
detect: ["职位", "立即沟通", "继续沟通", "牛人", "打招呼", "薪资"]
open: 主屏点「BOSS直聘」图标
pace: careful
source: Mika（Apache-2.0）内置知识，按 iPhone 改写头部
---
## 是什么
BOSS直聘是招聘求职 App。

## 已知现象
- 没登录时打开就是登录页:「微信登录」「一键登录」「其他方式登录」,下面要勾选同意用户协议和隐私政策。
- 登录页左上角返回进的是手机号登录页(手机号、「下一步」、「接收不到短信」,以及微信、QQ 等其他登录方式),再按返回就退出 App。没有不登录浏览的入口。
