# QQMessageManager

QQMessageManager 是一个基于 Python / PySide6 的 QQ 消息统一管理客户端。程序通过 NapCatQQ 的 OneBot 正向 WebSocket 接收和发送群聊、私聊消息，并提供会话列表、图片显示、AI 代管、Skill 库、聊天总结、图片生成和表情包记忆等功能。

默认连接地址：

```text
ws://127.0.0.1:3001
```

端口和 Token 由用户在登录窗口中填写。Token 可以留空，连接配置会自动保存。

## 当前功能

- 登录页配置 NapCatQQ 正向 WebSocket 地址、Host、Port、Path 和 Token。
- 自动缓存上次填写的连接配置；点击“断开连接”后返回登录窗口。
- 自动读取最近会话与历史消息，并持续接收实时群聊、私聊消息。
- 自动获取群名、私聊昵称和备注。
- QQ 风格会话列表、未读数量、会话置顶和聊天气泡。
- 支持聊天图片缓存、缩略图显示、透明/纯白画布裁剪。
- 支持 MiniMax、OpenAI、DeepSeek 和自定义 OpenAI 兼容接口。
- AI 上下文参考消息数可设置为 `1～999`。
- 支持多选 Skill 库，统一管理角色 Skill、图片理解、图片生成和聊天总结能力。
- 支持每个群聊或私聊单独开启 AI 代管。
- 支持普通消息自动回复、被 @ 优先回复、最近发言检查、避免连续自言自语和 AI 自主跳过。
- 支持按回复长度模拟打字延迟，默认关闭。
- 支持按会话设置 AI 发言最小间隔，默认关闭。
- 支持视觉模型读取真实聊天图片。
- 支持服务商联动的独立生图模型选择。
- 支持聊天总结 Skill：指定消息数量、时间范围和人员过滤，并把总结直接发送到当前会话。
- 支持记忆最多 50 个 `mface/marketface` 表情包。
- AI 表情包库以缩略图网格显示，可锁定、删除、编辑摘要和使用时机。

## 运行环境

- Python 3.10+
- NapCatQQ 已启动并开启 OneBot 正向 WebSocket 服务

## 安装

```bash
pip install -r requirements.txt
```

## 启动

```bash
python main.py
```

或：

```bash
python -m qq_message_manager
```

## NapCatQQ 连接

程序作为 WebSocket 客户端运行，因此 NapCatQQ 中应开启“WebSocket 服务端 / 正向 WS”，不要配置为反向 WebSocket。

推荐配置：

```text
类型：WebSocket 服务端 / 正向 WS
Host：127.0.0.1
Port：3001
Path：留空
messagePostFormat：array
reportSelfMessage：false
Token：没有鉴权需求时留空
启用：开启
```

登录窗口字段：

- **完整地址**：默认 `ws://127.0.0.1:3001`。
- **Host / Port / Path**：关闭“优先使用完整 WebSocket 地址”后使用。
- **Token**：填写 NapCatQQ 正向 WebSocket 配置中的 Token。

## AI 设置

AI 设置按三个标签页整理：

### 模型与角色

- 服务商、API Key、API 地址和聊天模型。
- 与服务商联动的独立生图模型。
- Skill 库。
- 自定义 Prompt。
- 连接测试。

### 回复策略

- 收到新消息后自动回复。
- 被 @ 时优先回复。
- 普通回复延迟和 @ 回复延迟。
- 回复前确认最近仍有人发言。
- 避免连续自言自语。
- 允许 AI 判断本次无需回复。
- 按回复长度模拟打字延迟。
- 发言最小间隔。

### 上下文与能力

- 参考最近消息数。
- 表情包记忆和使用相关配置。
- 图片理解、图片生成和聊天总结等能力由 Skill 库统一加载。

所有用户配置通过 `QSettings` 保存，重启后继续使用。

## Skill 库

AI 设置中的“Skill 库”支持同时加载多个 Skill。

内置 Skill：

- **shuimen**：角色和表达风格 Skill。
- **图片理解**：允许视觉模型读取聊天中的真实图片。
- **图片生成**：识别明确画图请求并调用已选择的生图模型。
- **聊天总结**：读取历史消息、调用 AI 总结并把结果发回当前会话。

仓库中新增下面结构后，也会作为扩展 Skill 显示：

```text
qq_message_manager/skills/<skill_id>/SKILL.md
```

角色/扩展 Skill 会注入普通聊天提示词；能力 Skill 控制对应的程序功能。

## AI 发言最小间隔

“启用发言最小间隔”默认关闭，默认间隔值为 60 秒。

开启后：

- 每个群聊和私聊分别计算，不会互相影响。
- 从 AI 上次在该会话中实际发送内容开始计时。
- 指定时间内，普通 AI 回复、追加表情包、图片生成结果、聊天总结和 AI 错误提示都不会再次发送。
- 已经开始生成但尚未发送的结果，会在最终发送前再次检查间隔。
- 用户手动点击“发送”或按 Enter 发送的消息不受该规则影响，也不会重置 AI 的间隔计时。
- 断开连接后，本次运行中的上次发言时间会清空；开关和间隔秒数仍会保留。

相关设置键：

```text
ai/min_speech_interval_enabled
ai/min_speech_interval_seconds
```

## AI 图片读取

加载“图片理解” Skill 后，程序会：

1. 从 NapCat 图片消息段读取本地路径或 URL。
2. 下载并缓存图片。
3. 裁剪明显透明边缘或纯白画布。
4. 将 GIF、WebP 等转换成标准静态 PNG/JPEG。
5. 把真实图片作为多模态内容发送给当前聊天模型。

当前模型和接口必须支持视觉输入。图片没有成功进入请求时，程序不会告诉模型它已经看到了图片。

## AI 图片生成

加载“图片生成” Skill 后，当前已代管会话中的明确画图请求可以直接触发图片生成，不要求必须 @ 机器人。

生图模型和聊天模型相互独立。切换服务商时，生图模型下拉框会自动联动：

- MiniMax：`image-01`、`image-01-live`。
- OpenAI：预设 GPT Image 模型。
- DeepSeek：当前显示无可用生图模型。
- 自定义服务商：可手动输入接口支持的生图模型名称。

图片生成请求会接管本条消息，避免同时触发普通文本回复。模型或接口不支持图片生成时，会明确告诉聊天对象。

## AI 表情包库

开启表情包记忆后，程序会记录收到的 `mface` 或疑似 marketface 图片消息，最多保留 50 个。

主窗口“表情包库”提供缩略图网格，可以直接查看所有已记录表情包。选择缩略图后可查看：

- 大图预览；
- ID 和消息类型；
- 摘要；
- 使用时机；
- 使用次数；
- 记录时间和最近使用时间；
- 锁定状态。

管理操作：

- **保存描述**：修改摘要和使用时机。保存后 AI 下次收到表情包候选列表时会立即使用新描述。
- **锁定**：该表情包不会在超过 50 个时被自动淘汰。
- **解除锁定**：恢复参与自动淘汰。
- **删除**：只删除 AI 记忆记录，不删除 QQ 中的原表情包。

摘要用于简短说明图片表达的情绪或含义；“使用时机”可以写得更具体，例如：

```text
摘要：震惊到说不出话
使用时机：适合对方说出非常离谱或出乎意料的内容时使用；不要在严肃道歉场景使用。
```

AI 获得的候选数据包含：

```text
id
summary
usage_hint
```

本地保存位置：

```text
~/.qq_message_manager/sticker_memory.json
~/.qq_message_manager/sticker_memory.locks.json
```

锁定状态使用单独文件保存；摘要和使用时机直接保存在 `sticker_memory.json` 中。

## 聊天总结 Skill

聊天总结默认最多总结最近 200 条消息，范围为 `1～1000`。程序会主动调用 NapCat 历史消息接口，而不是只使用窗口中已经显示的消息。

可直接在已代管会话中发送：

```text
总结一下
总结最近 80 条
总结最近 100 条，只看张三、李四
总结 QQ 123456 的最近 50 条发言
```

也可以点击发送栏中的“总结”按钮，设置：

- 最近消息数量；
- 仅总结的群昵称或 QQ 号；
- 开始和结束时间。

指定人员后，程序会多读取一些历史消息，再按 `sender_name` 或 `sender_id` 过滤，最终传给模型的消息数量不会超过设定值。发起总结的指令本身不会计入被总结内容。

总结完成后会直接发送到当前群聊或私聊；内容较长时自动拆分成多条。

## 本地数据

主要本地数据：

```text
QSettings：连接配置、AI 设置、Skill 选择、规则配置、置顶和代管会话
~/.qq_message_manager/sticker_memory.json：表情包记忆和可编辑描述
~/.qq_message_manager/sticker_memory.locks.json：表情包锁定状态
系统临时目录：聊天图片预览、视觉输入转换文件和 AI 生成图片
```

请勿把真实 API Key、NapCat Token、QQ 号、表情包记忆文件或缓存文件提交到仓库。

## 项目结构

```text
.
├── AGENTS.md
├── AI_RULES.md
├── README.md
├── main.py
├── requirements.txt
└── qq_message_manager
    ├── ai_client.py
    ├── ai_context_limit_patch.py
    ├── ai_min_speech_interval.py
    ├── ai_rules_cleanup.py
    ├── ai_summary.py
    ├── ai_typing_delay.py
    ├── app.py
    ├── chat_summary_feature.py
    ├── chat_summary_people_patch.py
    ├── chat_summary_skill.py
    ├── image_cache.py
    ├── image_generation_feature.py
    ├── image_generation_model_selector.py
    ├── image_generation_toggle_patch.py
    ├── image_layout_patch.py
    ├── models.py
    ├── napcat_client.py
    ├── skill_library_feature.py
    ├── skills
    │   ├── chat_summary/SKILL.md
    │   ├── image_generation/SKILL.md
    │   ├── shuimen/SKILL.md
    │   └── vision/SKILL.md
    ├── sticker_library_feature.py
    ├── sticker_memory.py
    ├── sticker_metadata_editor.py
    ├── styles.py
    ├── ui.py
    └── vision_input_patch.py
```
