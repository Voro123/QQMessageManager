# QQMessageManager

QQMessageManager 是一个基于 Python 桌面窗口的 QQ 消息统一管理客户端，用于通过 [NapCatQQ](https://github.com/NapNeko/NapCatQQ) 的 OneBot 正向 WebSocket 接口实时接收 QQ 私聊和群聊消息，并以接近 QQ 的会话列表 + 消息窗口形式展示和发送文本消息。

默认连接地址：

```text
ws://127.0.0.1:3001
```

端口和 Token 由用户在登录窗口中输入，Token 可以留空；程序会缓存上次成功点击连接时填写的登录配置，避免每次重复输入。

## 当前功能

- 登录页输入 NapCatQQ WebSocket 地址、端口与 Token
- 自动缓存上次输入的完整地址、Host、Port、Path、Token 和连接模式
- 默认使用 `ws://127.0.0.1:3001`
- 登录连接成功后，自动请求最近 20 个会话，并为每个会话加载最近 20 条历史消息
- 历史消息会按时间插入会话，不计入未读消息
- 通过 WebSocket 实时监听 NapCatQQ / OneBot 消息事件
- 自动区分群聊消息和私聊消息
- 自动调用 `get_group_info` 获取群名，并在会话列表和聊天标题中显示真实群名
- 自动补全私聊昵称/备注，私聊会话优先显示人名而不是 QQ 号
- 自动过滤图片消息段和 CQ 图片信息；开启“允许读取图片”后，AI 可结合真实图片内容回复并在窗口显示缩略图
- 新消息到达时，只有滚动条原本就在底部才会自动滚到底；正在翻历史时不会被打断
- 左侧 QQ 风格会话列表，显示群聊和私聊
- 支持右键会话置顶/取消置顶，置顶会话会排在列表顶部并使用浅黄色背景显示
- 支持全局 AI 代管配置，服务商支持 `Minimax-m3`、OpenAI、DeepSeek，以及自定义 OpenAI 兼容接口
- 支持在 AI 设置中选择本地角色 Skill，默认“无”，当前内置 `shuimen`
- 支持实际视觉输入：图片成功读取后会转换成标准 PNG/JPEG，并通过内部 `vision` Skill 要求视觉模型先看图再回复
- 支持可选图片生成 Skill：AI 设置中“允许生成图片”默认关闭；开启后，已代管会话中的明确画图请求不需要 @ 机器人即可触发
- 当前模型不支持图片生成时，会明确回复请求者当前模型不支持，不会伪造图片或链接
- 支持记忆收到过的 mface/marketface 表情包，最多 50 个
- 支持 AI 从已记忆表情包中选择一个追加发送，前端会先发送文字，再追加表情包
- 主窗口提供“表情包库”管理入口，可预览、锁定、解除锁定和删除已记忆表情包
- 锁定表情包不会参与自动淘汰；50 个全部锁定时，新表情包不会替换已有锁定记录
- 支持 AI 生成回复后按字数模拟打字延迟，开关默认关闭
- 支持点击“总结”按钮总结当前单个群聊或私聊，会按设置的时间区间和消息最大数量主动读取历史消息后交给 AI 总结
- 支持每个群聊/私聊单独开启 AI 代管，不影响其他未代管会话
- AI 上下文参考消息数范围为 1～999
- AI 配置、API Key、Skill 选择、表情包开关、图片生成开关、规则配置和被代管会话列表都会缓存到当前系统用户的 Qt 配置中
- 右侧消息窗口展示当前会话的消息时间、发送者和内容
- 底部输入框发送文本消息，群聊使用 `send_group_msg`，私聊使用 `send_private_msg`
- 未读消息计数
- 自动重连与连接状态提示
- 点击断开连接后返回登录窗口

## 运行环境

- Python 3.10+
- NapCatQQ 已启动并开启 WebSocket 服务

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

## NapCatQQ 连接说明

程序当前作为 WebSocket 客户端运行，所以 NapCatQQ 里应开启“WebSocket 服务端 / 正向 WS”，不要配置成“WebSocket 客户端 / 反向 WS”。

推荐在 NapCatQQ 网络配置中新建：

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

程序启动后会打开登录窗口：

- 完整地址：默认 `ws://127.0.0.1:3001`
- Host：默认 `127.0.0.1`
- Port：默认 `3001`
- Path：默认留空；只有你在 NapCatQQ 里设置了自定义路径时才填写
- Token：填写 NapCatQQ 正向 WS 配置中的 token，没有则留空

## AI 图片读取

开启“允许读取图片（需要视觉模型）”后，程序会：

1. 从 NapCat 图片消息段读取 `path` 或 `url`；
2. 下载并缓存图片；
3. 裁剪明显的透明或纯白画布；
4. 将 GIF、WebP 等转换为标准静态 PNG/JPEG；
5. 把真实图片作为多模态输入发送给当前模型。

当前模型或接口不接受图片时，日志会明确报错，不会假装识图成功。

## AI 图片生成

图片生成由 AI 设置中的“允许生成图片”控制，默认关闭。

开启后，需要同时满足：

- 当前群聊或私聊已开启 AI 代管；
- 消息明确要求生成、绘制或制作图片；
- 当前配置的模型和接口支持图片生成。

不再要求必须 @ 机器人。图片生成请求会接管本条消息，不会同时再排一条普通文本回复。

当前实现的能力判断：

- OpenAI `gpt-5` 系列及更新主模型：尝试通过 Responses API 图片生成工具执行；
- `gpt-image-*`、`dall-e-*` 或明确的自定义 OpenAI 兼容图片模型：尝试通过 Images API 执行；
- MiniMax-M3、DeepSeek 等当前文本模型：直接回复“当前模型不支持生成图片”；
- 运行时接口拒绝图片生成工具：同样按不支持处理。

生成成功后，图片临时保存到系统临时目录，并通过 OneBot `image` 消息段发送到当前会话。

## AI 表情包库

开启“记忆收到的表情包（最多 50 个）”后，程序会记录收到的 `mface` 或疑似 marketface 图片消息。

主窗口发送栏中的“表情包库”按钮可查看：

- 图片预览；
- 表情包 ID、类型、摘要和用途；
- 使用次数；
- 记录时间和最近使用时间；
- 当前锁定状态。

管理操作：

- **锁定**：该表情包不会在超过 50 个时被自动淘汰；
- **解除锁定**：恢复参与自动淘汰；
- **删除**：只删除 AI 记忆记录，不删除 QQ 中的原表情包。

锁定状态单独保存在：

```text
~/.qq_message_manager/sticker_memory.locks.json
```

表情包记忆本体仍保存在：

```text
~/.qq_message_manager/sticker_memory.json
```

如果 50 个记录全部锁定，新收到的表情包会被舍弃，不会替换锁定内容。

## 聊天总结

点击发送栏旁的“总结”按钮，可以总结当前单个群聊或私聊。

- 默认时间区间不限；
- 默认最大消息数量为 200；
- 程序会主动调用 NapCat 历史消息接口，而不是只总结当前窗口已显示的内容；
- 总结结果包含总览、主要话题、结论/待办、氛围变化和需要回看原文的点。

## 项目结构

```text
.
├── AGENTS.md
├── AI_RULES.md
├── main.py
├── requirements.txt
└── qq_message_manager
    ├── __init__.py
    ├── __main__.py
    ├── ai_client.py
    ├── ai_context_limit_patch.py
    ├── ai_rules_cleanup.py
    ├── ai_summary.py
    ├── ai_typing_delay.py
    ├── app.py
    ├── button_position_patch.py
    ├── chat_summary_feature.py
    ├── image_cache.py
    ├── image_generation_feature.py
    ├── image_generation_toggle_patch.py
    ├── image_layout_patch.py
    ├── models.py
    ├── napcat_client.py
    ├── return_to_login_patch.py
    ├── skills
    │   ├── image_generation
    │   │   └── SKILL.md
    │   ├── shuimen
    │   │   └── SKILL.md
    │   └── vision
    │       └── SKILL.md
    ├── sticker_library_feature.py
    ├── sticker_memory.py
    ├── styles.py
    ├── ui.py
    └── vision_input_patch.py
```

## 说明

本项目当前支持消息接收、最近历史消息加载、群名/私聊名展示、图片显示与视觉输入、会话置顶、AI 代管、角色 Skill、可选图片生成、表情包记忆/管理/发送、AI 打字延迟、聊天总结和文本消息发送。
