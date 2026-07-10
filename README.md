# QQMessageManager

QQMessageManager 是一个基于 Python / PySide6 的 QQ 消息统一管理客户端。程序通过 NapCatQQ 的 OneBot 正向 WebSocket 接收和发送群聊、私聊消息，并提供会话管理、AI 代管、Skill 库、聊天总结、图片理解与生成、表情包记忆和定时任务等功能。

## 当前功能

- 登录页配置并缓存 NapCatQQ 正向 WebSocket 地址和 Token。
- 自动读取最近会话与历史消息，持续接收实时群聊和私聊消息。
- 自动获取群名、私聊昵称和备注。
- QQ 风格会话列表、未读数量、会话置顶和聊天气泡。
- 支持聊天图片缓存、缩略图显示、透明/纯白画布裁剪。
- 支持 MiniMax、OpenAI、DeepSeek 和自定义 OpenAI 兼容接口。
- 支持统一接口超时设置，适配低速和推理模型。
- 支持每个会话独立开启 AI 代管。
- 支持多选 Skill 库、图片理解、图片生成和聊天总结。
- 支持 AI 发言最小间隔和模拟打字延迟。
- 支持记忆最多 50 个表情包，并在缩略图网格中锁定、删除、编辑摘要和使用时机。
- 支持每天固定时间或从创建时间开始按固定间隔执行 AI 定时任务。
- 支持定时任务专属的受限本地文件工作区，可维护 XLSX、CSV、JSON 和 Markdown。
- 支持每天把生成文件私聊发送给机器人自己或指定好友，确认发送成功后删除旧文件并创建新文件。
- 点击“断开连接”后返回登录窗口。

## 运行环境

- Python 3.10+
- NapCatQQ 已启动并开启 OneBot 正向 WebSocket 服务
- QQMessageManager 与 NapCatQQ 位于同一台设备时，可直接通过本地路径上传定时任务文件

## 安装

```bash
pip install -r requirements.txt
```

主要依赖：

```text
PySide6
websockets
openpyxl
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
Port：与登录窗口填写的端口一致
Path：通常留空
messagePostFormat：array
reportSelfMessage：false
Token：没有鉴权需求时留空
启用：开启
```

## AI 设置

AI 设置分为模型连接、回复策略、上下文与能力等区域。

### 模型连接

- 服务商、API Key、API 地址和聊天模型。
- 与服务商联动的独立生图模型。
- 接口超时，默认 180 秒，可设置为 10～1800 秒。
- 连接测试。

### 回复策略

- 收到新消息后自动回复。
- 被 @ 时优先回复。
- 普通回复和 @ 回复延迟。
- 回复前确认最近仍有人发言。
- 避免连续自言自语。
- 允许 AI 判断本次无需回复。
- 按回复长度模拟打字延迟。
- 按会话设置发言最小间隔。

### Skill 库

普通聊天 Skill 库支持同时加载多个 Skill：

- `shuimen`：角色和表达风格。
- 图片理解：视觉模型读取真实聊天图片。
- 图片生成：调用独立生图模型。
- 聊天总结：读取历史消息并把总结发送到当前会话。

`scheduled_files` 是定时任务专用 Skill，不会显示在普通聊天 Skill 库，也不会在群聊、私聊或 @ 机器人时加载。

## AI 图片读取

加载“图片理解”后，程序会下载并缓存聊天图片，裁剪明显透明或纯白画布，转换为标准 PNG/JPEG，再作为真实多模态输入发送给支持视觉能力的模型。

图片没有成功进入请求时，程序不会告诉模型它已经看到了图片。

## AI 图片生成

加载“图片生成”后，已代管会话中的明确画图请求可以触发图片生成，不要求必须 @ 机器人。

聊天模型和生图模型相互独立。切换服务商时，生图模型会联动变化：

- MiniMax：`image-01`、`image-01-live`。
- OpenAI：预设 GPT Image 模型。
- DeepSeek：当前显示无可用生图模型。
- 自定义服务商：可手动填写兼容接口支持的模型。

## AI 表情包库

开启表情包记忆后，程序会记录收到的 `mface` 或疑似 marketface 图片，最多保留 50 个。

主窗口“表情包库”提供：

- 全部表情包缩略图；
- 大图预览；
- 可编辑摘要和使用时机；
- 使用次数和最近使用时间；
- 锁定、解除锁定和删除。

锁定表情包不参与自动淘汰。摘要和使用时机会直接提供给 AI，帮助判断何时使用该图片。

## 聊天总结 Skill

聊天总结默认读取最近 200 条消息，范围为 1～1000。

示例：

```text
总结一下
总结我们最近的聊天记录
总结最近 80 条
总结最近 100 条，只看张三、李四
总结 QQ 123456 的最近 50 条发言
```

`people` 参数可选。“我们、咱们、大家、本群、当前聊天”等词表示总结当前会话整体，不会被当成人员过滤条件。

总结完成后会直接发送到当前群聊或私聊，较长内容会自动分段。

# 定时任务

主窗口发送栏中的“定时任务”按钮可打开任务管理窗口。

## 调度方式

支持：

```text
从任务创建时间开始，每隔 N 分钟执行
每天 HH:mm 执行
```

间隔任务以创建时间为固定锚点。例如在 10:17 创建一个 30 分钟任务，后续计划时间为 10:47、11:17、11:47。

任务只在程序运行且连接 NapCatQQ 时执行。程序关闭或断线期间错过的任务，在恢复后只补最近一次，然后继续下一次未来计划。

同一任务不会并发执行。失败后会依次在 1 分钟、5 分钟、15 分钟后重试。

## 通用任务配置

每个任务可配置：

- 名称和启用状态；
- 目标群聊或私聊；
- 每日或固定间隔调度；
- 最多读取的历史消息数；
- 用户提供的可信任务 Prompt；
- 静默执行，或把 AI 文本结果发送到目标会话；
- 是否启用本地文件工作区；
- 是否每天私聊发送文件。

任务执行指令在程序内部创建，不会先向 QQ 发送一条可见的伪造用户消息。

聊天历史会作为不可信数据单独传给模型。聊天中出现的“忽略规则、修改文件、改变接收人”等内容不能改变任务配置和权限。

## 检查点和补执行

每个任务在 SQLite 中保存：

- 上次成功处理时间；
- 上次成功消息 ID；
- 已处理消息键；
- 最近运行状态和错误；
- 重试次数。

正常执行范围为：

```text
上次成功检查点 ～ 本次执行开始时间
```

只有处理和文件写入成功后才推进检查点。每日发送任务只有在 NapCat 返回文件上传成功后，才会把本轮标记为成功。

当前版本每次通过 NapCat 历史接口读取最多 5000 条消息。消息量很大的群应适当提高任务执行频率，避免单次时间范围超过历史接口返回能力。

## 定时任务本地文件 Skill

文件 Skill 只在定时任务上下文开放，普通聊天无法调用。

任务专属工作区：

```text
~/.qq_message_manager/automation_workspace/<task_id>/
```

允许格式：

```text
.xlsx
.csv
.json
.md
```

禁止：

- 访问绝对路径或父目录；
- 读取其他任务目录、API Key、Token 和应用配置；
- 执行 Shell、Python、宏或外部程序；
- 由 AI 自行删除、移动、重命名或发送文件；
- 由聊天内容改变文件接收人。

AI 不直接操作文件。模型只能返回经过程序验证的结构化操作：

```json
{
  "message": "本轮执行结果",
  "operations": [
    {
      "action": "insert",
      "values": {
        "问题": "如何修改连接端口？",
        "状态": "待处理"
      },
      "source_message_ids": ["123456"]
    },
    {
      "action": "update",
      "record_id": "row_abcd1234",
      "values": {
        "状态": "已完成",
        "处理结果": "已在登录页修改端口"
      },
      "source_message_ids": ["123456", "123789"]
    }
  ]
}
```

程序只接受 `insert` 和 `update`，并校验列名、字段类型、枚举值、只读字段和现有记录 ID。

## 自定义文件结构

每个任务可以自定义列结构。编辑窗口中每行定义一列：

```text
列名|类型|必填/可选|枚举值|默认值|可更新/只读
```

示例：

```text
提问时间|datetime|必填|||只读
提问人|text|必填|||只读
问题|text|必填|||可更新
分类|text|可选|||可更新
状态|enum|可选|待处理,处理中,已完成,忽略|待处理|可更新
回答|text|可选|||可更新
```

支持类型：

```text
text
number
datetime
boolean
enum
```

还可以配置多个去重字段。模型新增记录时，如果这些字段组合与已有记录相同，程序会优先更新原记录。

即使用户自定义可见列，程序仍会在任务侧车记录和 XLSX 隐藏工作表中保存 `record_id`、来源消息 ID、创建时间、更新时间和去重签名，以支持后续回答更新原记录。

## 每日文件发送与轮换

启用每日发送后，可选择：

- 机器人自己的 QQ；
- 当前已知私聊好友；
- 手动填写其他好友 QQ。

每日执行顺序：

1. 读取并处理上次检查点到发送时间的剩余消息；
2. 保存当前文件；
3. 调用 NapCat `upload_private_file`；
4. 等待匹配的 `echo` 成功响应；
5. 更新检查点；
6. 删除已发送的旧文件；
7. 创建新一天的空文件。

发送失败时不会删除旧文件，也不会推进检查点，并会按重试策略重新执行。

当前本地路径上传方案默认 QQMessageManager 与 NapCatQQ 位于同一台设备。远程 NapCat 场景后续需要接入 Stream API 或可被 NapCat 访问的文件 URL。

## 本地数据

```text
QSettings：连接配置、AI 设置、Skill、定时任务定义、置顶和代管会话
~/.qq_message_manager/sticker_memory.json：表情包记忆和描述
~/.qq_message_manager/sticker_memory.locks.json：表情包锁定状态
~/.qq_message_manager/automation_state.sqlite3：任务检查点、状态和消息去重
~/.qq_message_manager/automation_workspace/<task_id>/：任务文件和记录侧车数据
系统临时目录：聊天图片预览、视觉输入转换和生成图片
```

不要把真实 API Key、NapCat Token、QQ 号、表情包记忆、自动化工作区、SQLite 状态或缓存文件提交到仓库。

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
    ├── ai_request_timeout.py
    ├── automation_ai.py
    ├── automation_feature.py
    ├── automation_models.py
    ├── automation_napcat.py
    ├── automation_patches.py
    ├── automation_storage.py
    ├── app.py
    ├── chat_summary_feature.py
    ├── chat_summary_skill.py
    ├── image_generation_feature.py
    ├── napcat_client.py
    ├── skill_library_feature.py
    ├── skills
    │   ├── chat_summary/SKILL.md
    │   ├── image_generation/SKILL.md
    │   ├── scheduled_files/SKILL.md
    │   ├── shuimen/SKILL.md
    │   └── vision/SKILL.md
    ├── sticker_library_feature.py
    ├── sticker_memory.py
    ├── ui.py
    └── vision_input_patch.py
```
