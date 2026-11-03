# 第三方内容与依赖

本仓库自己的代码按 MIT 发布（见 `LICENSE`）。下面这些不是本仓库写的：

## 收录在仓库里的

| 内容 | 来源 | 许可 |
|---|---|---|
| `kvmphone/knowledge/apps/` 下 xiaohongshu、boss-zhipin、douyin、feishu、whatsapp、telegram、bilibili、gmail 八份 `APP.md` 的正文 | [Mika](https://github.com/wangzan101/Mika) 的内置 App 知识，头部按 iPhone 改写 | Apache-2.0，全文见 `LICENSE-Apache-2.0-Mika.txt` |

`kvmphone/kpx.py` 的 App 知识格式、节奏控制、人工接管、任务小结等设计参考了 Mika，代码是重写的。

## 运行时依赖（不在仓库里，需要自己装）

| 依赖 | 用在哪 | 许可 |
|---|---|---|
| [pypinyin](https://github.com/mozillazg/python-pinyin) | `kp paste` 把中文转成拼音敲进 iOS 输入法 | MIT |
| [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) | subgate：把 Antigravity / Gemini 账号包成 OpenAI 兼容接口给飞书机器人用 | MIT |
| [OpenClaw](https://github.com/openclaw/openclaw) | 飞书机器人网关（rmq1 agent） | 见上游 |
| EGO LITE（`ego-browser`） | 常驻打开 GLKVM 网页、跑 `bridge.mjs` | 见上游 |
| Google Antigravity CLI（`agy`） | Gemini 驾驶模型 | 见上游 |
| Claude Code（`claude`） | `--driver cc` 原生驱动、复核 | 见上游 |
| WebDriverAgent、pymobiledevice3 | 可选的 `wda` 后端（数据线直控） | 见上游 |
