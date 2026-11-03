# kp 的 App 知识

格式照 Mika（github.com/wangzan101/Mika docs/knowledge-format.md），只写事实，不写规定。每个 App 一个目录 `apps/<id>/APP.md`：

- 头部：`name` `display` `description` `aliases` `open`（怎么打开），`pace: careful`（这个 App 限制操作快慢，kp 自动放慢），
  以及 kp 自己加的 `brand`（品牌词，屏上出现一个算 3 分）和 `detect`（特征词，一个 1 分，三个字以下要整行相等）——iOS 拿不到包名，kp 靠读屏文字认 App，满 3 分算认出。
- 正文：是什么 / 界面 / 功能 / 已知现象，不超过 2500 字。

驾驶模型在任务结果里写的「学到」由 rmq 记进运行机器的 `~/.config/kvmphone/learned/<id>.md`（不进本目录，不会被同步覆盖），kp 读说明时一并附上。
确认稳定的条目再手工搬进 APP.md。

xiaohongshu、boss-zhipin、douyin、feishu、whatsapp、telegram、bilibili、gmail 八份正文取自 Mika 的内置知识（Apache-2.0），头部按 iPhone 改写。
