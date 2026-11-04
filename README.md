# KVMPHONE · 让 AI 操控一台真 iPhone

在飞书里说一句「美团、淘宝闪购、京东秒送比一下公牛排插，今天要」，几分钟后飞书收到推荐和候选价格表——中间是 AI 在一台真 iPhone 上自己开 App、搜索、领券、进结算页读实付，**停在付款前**，付钱永远由你自己点。

不越狱、不装描述文件、不用 iPhone 上的任何辅助 App：手机只接了一个 IP-KVM（HDMI 采画面 + USB 当键鼠）。AI 看到的就是屏幕，按的就是「鼠标」。

> 作者内部代号 RMQ1（第一台「让手机去」的手机），代码和日志里随处可见。

## 能做什么

- 外卖 / 闪购 / 电商比价：按「今天到 / 明天到 / 无所谓」分档搜平台，领完券读结算页实付，替你选定一个，停在付款前。
- 查信息：小红书、抖音、B 站搜东西读前几条；查机票酒店、快递、App 里的订单状态。
- 代办：发微信消息（收件人和内容严格照原话）、填信息、加乘机人、预约到付款前。
- 遇到登录、验证码、滑块：截图推给你，你在手机上处理完点「好了」，它接着做。

## 架构

```
 飞书「手机」机器人
        │  (消息)
        ▼
 ┌────────────────────────── 入口机（笔记本） ──────────────────────────┐
 │ OpenClaw · rmq1 agent ── Gemini 3.8 Flash（经执行机 subgate，本机回落）│
 │        │ exec: rmq ask "整理好的完整要求"                               │
 │ rmq 客户端 + rmq relay（取回结果、推飞书）                              │
 └────────┼──────────────────────────────────────────────────────────────┘
          │ ssh
          ▼
 ┌────────────────────────── 执行机（Mac mini） ─────────────────────────┐
 │ rmq worker（LaunchAgent，每台手机一条队列）                            │
 │   ├─ 驾驶（默认）：agy + Gemini 3.8 Flash low，常驻一个预热进程         │
 │   ├─ 驾驶（--driver cc）：claude -p + DeepSeek（dsnothink 代理关思考）  │
 │   └─ kp ──HTTP :7690──► bridge.mjs（常驻在 EGO LITE 的 GLKVM 页面里）   │
 │                           ├─ <video> 抓帧 → 苹果 Vision OCR → 文字+坐标 │
 │                           └─ 页面内直发 HID：拟人轨迹点击 / 滑动 / 键盘 │
 │ subgate（CLIProxyAPI :8317，飞书机器人的模型入口）                      │
 └────────┼──────────────────────────────────────────────────────────────┘
          │ HTTPS / WebRTC
          ▼
      GLKVM ──HDMI 采集 / USB HID──► iPhone
```

几个关键设计：

- **读屏用 OCR，不靠截图喂模型**。`kp look` 返回「中心x,中心y<TAB>文字」，模型按文字点（`kp tap 搜索`）。看图只兜底。
- **每个动作自带读屏**。`kp tap` 做完等画面稳定（页面里比缩略图，0.25 秒不动就 OCR，约 0.7–0.9 秒）再打印新屏，省掉单独的读屏回合。
- **`kp open App 关键词` 直达**。经 Safari 敲 App 的 URL scheme，一步落到搜索结果页（约 15 秒），省掉点图标、点搜索框、打中文、点搜索四五步。表在 `kvmphone/schemes.json`，已真机验证：小红书、抖音、淘宝、京东、美团、拼多多、淘宝闪购、哔哩哔哩、大众点评、微信首页。
- **驾驶在执行机本机跑、带预热池**。agy 每次冷启动约 12 秒（谷歌认证、拉模型），worker 常驻一个已启动、等 stdin 的 agy（`--input-format stream-json`），任务来了直接塞进去；闲置 20 分钟换新。
- **原生 CC 驱动保留**：`rmq ask --driver cc` 走 Claude Code（`claude -p` 指 DeepSeek 的 Anthropic 兼容端点）。DeepSeek 经 claude -p 默认带思考、设置关不掉，所以经 `rmq/dsnothink.py` 代理把每个请求的 thinking 改成 disabled。
- **飞书机器人走执行机的 subgate**：执行机那条线连谷歌首字节约 0.4 秒；执行机不在线时秒回落到入口机本机的 subgate。
- **一台手机同一时刻只归一个调用方**：rmq 任务、你手动 kp、OpenClaw 共用时按租约排他（被占退出码 75，说清是谁）。

## 实测速度

| 场景 | 结果 |
|---|---|
| 多平台任务：小红书 / 抖音 / 淘宝各搜一个词、读前 3 条 | **3 分 13 秒**，29 个模型回合，**9/9 正确**（Gemini 3.8 Flash low） |
| 同一题换高思考档（high） | 12 分 10 秒，84 个回合，抖音那栏读错屏——所以默认 low |
| 只读任务（读一屏回答） | **12 秒**（驾驶经入口机中转时 26 秒，最早未优化 3 分 35 秒） |
| 只读任务，`--driver cc` 原生驱动 | 8 秒 |
| 飞书机器人每轮回复 | **3.5–4.3 秒**（经执行机 subgate）；工具题 8.5 秒；入口机本机 subgate 4.0–6.3 秒；旧的 agy-cli Gemini 4 方案约 24 秒 |
| 单个动作 | 拟人点击 0.3–0.45 秒，滑动 0.56–0.66 秒，读屏 0.7–0.9 秒；含模型思考的一步中位约 3.5 秒 |

## 目录

| 路径 | 是什么 |
|---|---|
| `rmq/rmq.py` | 任务队列：客户端（ask / ls / show / say / stop）、执行机常驻 worker、入口机 relay、驾驶调度（agy / cc）、预热池、自检 |
| `rmq/prompt.md` | 驾驶模型的系统提示：kp 用法、付款边界、比价规则、result.json 格式 |
| `rmq/review.md` | 可选的复核提示（另一个模型对照操作记录查答案、查越界） |
| `rmq/dsnothink.py` | 本地代理：把发往 DeepSeek Anthropic 兼容端点的请求 thinking 改成 disabled |
| `rmq/install-service.sh` | 把 worker / relay 装成 LaunchAgent |
| `rmq/go-mini.sh` | 一键把执行搬到执行机（检查前置 → 装 worker → 入口机切 relay） |
| `kvmphone/kp.py` | 点屏读屏命令行；付款护栏、输入法判定、`kp open` 直达、租约、ssh 转发 |
| `kvmphone/kpx.py` | App 知识、节奏控制、风控 / 登录提示、人工接管、打码 |
| `kvmphone/bridge.mjs` | 常驻桥：在 EGO LITE 里接管 GLKVM 页面，抓帧、OCR、页面内发 HID |
| `kvmphone/wda_bridge.py` | 可选后端：iPhone 数据线插执行机，经 WebDriverAgent 直控 |
| `kvmphone/ocr.swift` | 常驻 OCR（苹果 Vision），编译成 `kvmphone/ocr` |
| `kvmphone/schemes.json` | 各 App 的搜索 URL scheme |
| `kvmphone/knowledge/apps/` | 每个 App 一份说明（界面、入口、已知坑），驾驶进 App 时自动附上 |
| `kvmphone/devices.json` | 设备表：哪台手机、桥在哪台机器、什么后端 |
| `kvmphone/glkvmpass.sh` | 存 GLKVM 登录密码（600 文件），桥掉登录时自动重登 |
| `openclaw/` | 飞书机器人：rmq1 agent 的指令 `AGENTS.md`、技能、配置片段 |
| `deploy/` | 执行机的 LaunchAgent / CLIProxyAPI 配置模板，`subgate-mini.sh` 一键常驻 |
| `skills/KVM/SKILL.md` | Claude Code 技能：让 Claude 会用 rmq / kp |
| `config.example.json` / `config.client.example.json` / `.env.example` | 配置示例，不含任何真值 |

## 安装

需要：一个 GL.iNet GLKVM（或同类能在网页里出画面、发 HID 的 IP-KVM）、一台 iPhone、一台 macOS 执行机（Mac mini，常开，登录图形界面）、一台入口机（可选，跑飞书机器人；也可以和执行机是同一台）。

以下 `YOUR_USER`、`your-mini.local`、`macmini` 都是占位符，换成你自己的。

### 1. 接硬件

GLKVM 的 HDMI 输入接 iPhone（Lightning / USB-C 转 HDMI），GLKVM 的 USB 口接 iPhone 当键鼠。iPhone 设置里开「辅助功能 → 触控 → 辅助触控」后鼠标指针可用；建议关自动锁屏。浏览器打开 GLKVM 网页能看到手机画面、鼠标能点即可。

本仓库默认 GLKVM 的地址是 `https://glkvm.local`。不是这个地址就在 `/etc/hosts` 里加一行，或全局替换 `glkvm.local`。

### 2. 执行机

```bash
git clone https://github.com/<you>/KVMPHONE ~/rmq1
cd ~/rmq1
python3 -m pip install --user pypinyin pillow
swiftc -O kvmphone/ocr.swift -o kvmphone/ocr
mkdir -p ~/.local/bin && ln -sf ~/rmq1/kvmphone/kp.py ~/.local/bin/kp && ln -sf ~/rmq1/rmq/rmq.py ~/.local/bin/rmq
```

- 装 EGO LITE（`ego-browser` 命令可用），在它里面打开 `https://glkvm.local`、信任证书、登录一次。然后 `kvmphone/glkvmpass.sh` 存密码，以后掉登录桥会自己重登。
- 编辑 `kvmphone/devices.json`：`hosts.macmini` 写执行机的 `hostname -s`；手机鼠标映射横向偏了就把 `mouse` 改成 `frame`。
- `kp start && kp look`：能打印出屏幕文字就通了。
- 驾驶模型（二选一或都装）：
  - Gemini：装 Google Antigravity CLI `agy` 并在执行机的图形界面里登录（登录态在钥匙串里，只在 GUI 会话可用，ssh 里跑会报未登录——所以 worker 必须装成 LaunchAgent）。
  - 原生 CC：装 Claude Code，DeepSeek key 写进 `~/.config/deepseek/api_key`（600）。
- `cp config.example.json ~/.rmq/config.json` 并改路径；`rmq doctor` 自检。
- `sh rmq/install-service.sh on worker` 装常驻。

### 3. 入口机（要飞书机器人才需要）

```bash
git clone https://github.com/<you>/KVMPHONE ~/rmq1
ln -sf ~/rmq1/rmq/rmq.py ~/.local/bin/rmq && ln -sf ~/rmq1/kvmphone/kp.py ~/.local/bin/kp
cp config.client.example.json ~/.rmq/config.json   # remote 写执行机的 ssh 别名
sh rmq/install-service.sh on relay
```

`~/.ssh/config` 里给执行机起别名 `macmini`（免密登录）。之后入口机上的 `rmq ask` / `kp look` 会自动经 ssh 转给执行机。

### 4. 飞书机器人（OpenClaw）

1. 装 OpenClaw，用它的飞书向导加一个账号 `rmq1`（机器人名随意，如「手机」）。
2. 把 `openclaw/workspace-rmq1/AGENTS.md` 拷到 `~/.openclaw/workspace-rmq1/`，里面的 `/Users/YOUR_USER/.local/bin/rmq` 换成你的路径。
3. 按 `openclaw/openclaw.rmq1.example.jsonc` 把 provider、secrets、rmq1 agent、bindings 合并进 `~/.openclaw/openclaw.json`，重启网关。
4. subgate：执行机和入口机各装一份 CLIProxyAPI，配置照 `deploy/mini/cliproxy.example.yaml`，用它自带的登录命令登录 Antigravity / Gemini 账号；client key 两边写同一个值到 `~/.config/subgate/client_key`（600）。执行机上的常驻在入口机跑 `deploy/subgate-mini.sh`。

### 5. 用

```bash
rmq ask -t "排插比价" "美团、淘宝闪购、京东秒送比公牛 1.8 米 2 插位排插，今天到，替我选一个，停在付款前"
rmq ls
rmq show <id>
rmq ask --driver cc -t "原生驱动" "打开小红书搜咖啡，读前 3 条标题"
```

`rmq ask` 输出两行：第一行是任务 id，第二行是排位——前面没任务时以「马上开始」开头，有任务时是「排队中：前面还有 N 个任务」。飞书机器人照第二行回复「已开始」或「已排队，前面还有 N 个」（见 `openclaw/workspace-rmq1/AGENTS.md`）。

## 付款护栏

付钱是唯一的红线，四层一起管：

1. **kp 拒点**：按钮文字命中「提交订单 / 去支付 / 立即支付 / 免密支付 / 立即购买 / 开通月付…」（含繁体、英文 Pay now / Place order / Buy now，以及「支付 ¥12」「Pay S$8.50」这类带金额的）一律拒绝，退出码 3。
2. **按点击那一刻的真屏判断**：kp 每次点击前都实时读屏（`/read?settle=200`），不许用缓存的上一屏。起因是一次真实事故：模型连发三下坐标点击，第三下落在刚弹出的「立即支付」上，护栏还拿着上一屏判断，开着免密支付的 App 当场扣款（后来取消退款）。
3. **结算页只读**：结算 / 收银台页上，坐标点击只放行左上角返回；文字点击不放行泛泛的「确定 / 确认」；提示词要求结算页上不连点、每点一下先看结果。
4. **收尾只撤销自己做过的**：购物车里原有的东西不删、不改数量，不许「清空购物车 / 全选删除」（另一次事故里模型把用户原有的商品一起清掉了）。worker 收尾还会扫一遍日志里的「支付成功 / 下单成功」痕迹，有就判不通过。

此外：验证码、16–19 位卡号、18 位证件号在 kp 输出和日志里自动打码。

## 已知坑

| 坑 | 现象 | 解法 |
|---|---|---|
| iOS 拼音输入法吞 `=` | `kp open` 在 Safari 地址栏敲网址，`=` 被拼音键盘当候选翻页键吞掉，网址缺 `=` 不跳转 | kp 的 `ime_en`：先敲一个 `a`，看「123」键上方 200–330 px 有没有短汉字候选来判定是不是拼音（空格键上的「拼」OCR 读不到）；是就点地球键切英文 |
| ⌘L 不选中残字 | 地址栏留着上次敲了一半的网址 | ⌘L 之后再 ⌘A + 退格清空 |
| 京东「允许粘贴」弹窗 | 跳进京东时弹窗挡住点击 | 点「不允许」 |
| OpenClaw 拦局域网地址 | 飞书机器人一直走回落模型，日志 `SsrFBlockedError`，不报错 | 指向局域网的 provider 加 `"request": {"allowPrivateNetwork": true}` |
| 经 OpenClaw 网关 exec 跑 ssh 被 SIGTERM | 机器人调 `rmq ask` 偶发被杀、拿不到任务 id | ssh 的输出不要直接接 exec 输出流：rmq 客户端用 subprocess 收齐再打印 |
| 机器人自己去排查 | Gemini 3.8 Flash 卡住时会去翻源码、乱 ssh | `AGENTS.md` 写死「不许自己排查，失败两次就把报错告诉用户」 |
| GLKVM H.264 模式下 snapshot 接口只回 76 字节 | 拿不到 JPEG | 不改用户的推流设置，在页面里用 canvas 从 `<video>` 抓帧（约 0.2 秒） |
| GLKVM 网页不转发滚轮；慢拖被 iOS 当长按 | 滚不动、弹出长按菜单 | 页面内直发 HID 拖动，0.56–0.66 秒到位再松手；快于约 0.45 秒会触发惯性滚 2–3 秒 |
| EGO 的 `page.mouse.move` 每隔一次卡约 1 秒 | 一条拟人轨迹拖到 10–20 秒 | 在页面里直接经 GLKVM 的 WebSocket 发 HID |
| 桥脚本杀不掉 | `ego-browser nodejs` 的脚本跑在 EGO 的 Node 子进程里，杀命令行进程脚本照跑 | 桥启动时登记编号，发现有更新的桥就自己停手 |
| 横向点击全偏向中线 | 键盘上点 q 出 r | iPhone 的绝对坐标映射会在「铺满 HDMI 画面」和「铺满手机屏」之间变；devices.json 的 `mouse: phone|frame` 切换 |
| agy 加载全局规则 | 模型先联网搜索、写 LOG.md，白花约 35 秒 | 驾驶提示词前置「本任务优先：不联网、不写日志」并内嵌 kp 用法 |
| DeepSeek 经 claude -p 思考关不掉 | 一单 25 分钟、几百条命令，大头是思考 | `rmq/dsnothink.py` 代理改请求 |

## 安全与免责

- 只操控你自己的手机和账号。自动化操作可能违反部分 App 的服务条款，风控严的 App（微信、美团、小红书、抖音等）kp 会自动放慢，但风险自负。
- 仓库里没有任何密钥；所有 key 都从 600 文件读，路径写在配置里。subgate 的 `auth/` 登录凭据和 `client_key` 不要提交、不要拷给别人。
- 付款永远由人确认。改 kp 的点击逻辑前先读上面的「付款护栏」。

## 许可

MIT，见 `LICENSE`。第三方内容见 `THIRD_PARTY.md`。
