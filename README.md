# QQMessageManager

QQMessageManager 是一个基于 Python / PySide6 的 QQ 消息统一管理客户端。程序通过 NapCatQQ 的 OneBot 正向 WebSocket 接收和发送群聊、私聊消息，并提供会话管理、AI 代管、Skill 库、聊天总结、图片理解与生成、表情包记忆和定时任务工作流。

## 当前功能

- 登录页配置并缓存 NapCatQQ 正向 WebSocket 地址和 Token。
- 自动读取最近会话与历史消息，持续接收群聊和私聊消息。
- QQ 风格会话列表、未读数量、会话置顶和聊天气泡。
- 点击“断开连接”后返回登录窗口。
- 支持 MiniMax、OpenAI、DeepSeek 和自定义 OpenAI 兼容接口。
- 支持统一接口超时、上下文条数、模拟打字延迟和按会话发言最小间隔。
- 支持多选 Skill 库、图片理解、图片生成和聊天总结。
- 支持表情包缩略图库、锁定、删除、摘要和使用时机编辑。
- 支持每天固定时间或从创建时间开始按固定间隔执行 AI 定时任务。
- 支持定时任务专属的受限文件工作区，可维护 XLSX、CSV、JSON 和 Markdown。
- 支持导入现有文件、查看实际记录、识别外部手动修改并继续更新原行。
- 支持每日私聊发送归档文件、发送确认、失败重试、成功删除和新文件轮换。
- 支持 NapCat 本地路径上传和跨设备 Stream API 上传。

## 运行环境

- Python 3.10+
- NapCatQQ 已启动
- NapCatQQ 已开启 OneBot 正向 WebSocket 服务
- 使用 Stream API 时，NapCatQQ 需要 v4.8.115 或更高版本

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

## AI 图片与表情包

加载“图片理解”后，程序会下载并缓存聊天图片，裁剪明显透明或纯白画布，转换为标准 PNG/JPEG，再作为真实多模态输入发送给支持视觉能力的模型。

加载“图片生成”后，已代管会话中的明确画图请求可以触发图片生成。聊天模型和生图模型相互独立，切换服务商时生图模型会联动变化。

主窗口“表情包库”提供：

- 全部表情包缩略图；
- 大图预览；
- 可编辑摘要和使用时机；
- 使用次数和最近使用时间；
- 锁定、解除锁定和删除。

锁定表情包不参与自动淘汰。

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
- 文件格式、文件名模板和 Sheet 名；
- 自定义列结构和去重字段；
- 是否每天私聊发送文件；
- 文件接收 QQ；
- 文件传输模式。

任务执行指令在程序内部创建，不会先向 QQ 发送一条可见的伪造用户消息。

聊天历史会作为不可信数据单独传给模型。聊天中出现的“忽略规则、修改文件、改变接收人”等内容不能改变任务配置和权限。

## 检查点和补执行

每个任务在 SQLite 中保存：

- 上次成功处理时间；
- 上次成功消息 ID；
- 已处理消息键；
- 最近运行状态和错误；
- 重试次数；
- 跨重启待上传文件；
- 文件发送历史。

正常执行范围为：

```text
上次成功检查点 ～ 本次执行开始时间
```

只有 AI 分析、文件写入和必要的文件发送全部成功后才推进检查点。

每日发送任务只有在 NapCat 返回匹配 `echo` 的上传成功响应后，才会被标记为成功。

当前每次通过 NapCat 历史接口读取最多 5000 条消息。消息量很大的群应提高任务执行频率，避免单次时间范围超过历史接口返回能力。

## 定时任务文件 Skill

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
- 由 AI 自行导入、删除、移动、重命名或发送文件；
- 由聊天内容改变文件接收人。

AI 不直接操作文件，只能返回程序验证的结构化 `insert` 和 `update` 操作。

## 自定义文件结构

每个任务可以自定义列结构。编辑窗口中每行定义一列：

```text
列名|类型|必填/可选|枚举值|默认值|可更新/只读
```

示例：

```text
提问时间|datetime|必填|||只读
提问人QQ|text|必填|||只读
提问人昵称|text|必填|||只读
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

模型新增记录时，如果去重字段组合与已有记录相同，程序会优先更新原记录。

程序还会保留 `record_id`、来源消息 ID、创建时间、更新时间和去重签名，用于关联后续回答。

待处理、未完成和处理中记录会优先提供给 AI，避免较早的问题因为记录过多而无法被后续回答更新。

## 导入、预览和手动维护

任务管理窗口提供：

```text
查看数据
导入文件
打开工作区
```

“导入文件”只能由用户在窗口中操作，AI 和 QQ 消息不能指定导入路径。

如果可见文件比 `.records.json` 侧车更新，程序会认为文件经过用户手动修改，并重新读取实际文件内容。

XLSX 会读取隐藏的 `_QQMM_META` 工作表，以保留记录 ID 和来源消息信息。CSV、JSON 和 Markdown 会生成稳定的导入记录 ID。

## 第三阶段：文件发送与每日轮换

### 接收人

每日文件接收人支持：

- 机器人自己的 QQ；
- NapCat 好友列表中的好友；
- 手动填写其他好友 QQ。

任务管理窗口会读取：

```text
get_login_info
get_friend_list
get_version_info
```

“刷新好友/版本”会重新获取这些只读信息，不会自动修改任务接收人。

### 传输模式

每个任务可以选择：

```text
自动（推荐）
NapCat 本地路径
Stream API（跨设备）
```

自动模式规则：

- WebSocket 主机是 `localhost`、`127.0.0.1` 或其他回环地址：使用本地路径上传；
- WebSocket 主机不是回环地址：使用 Stream API。

本地路径模式要求 QQMessageManager 与 NapCatQQ 能访问同一个文件路径。

Stream API 会：

1. 以 64 KB 分片读取文件；
2. 计算 SHA-256；
3. 调用 `upload_file_stream`；
4. 等待 NapCat 返回远程临时文件路径；
5. 使用该路径调用 `upload_private_file`。

Stream API 需要 NapCat v4.8.115 或更高版本。

### 测试发送

任务管理窗口提供“测试发送文件”。

测试发送会真实调用 NapCat 上传，但不会：

- 推进聊天检查点；
- 标记业务消息已处理；
- 删除当前文件；
- 创建新一天文件；
- 改变任务计划时间。

机器人向自己发送文件是否可用取决于具体 QQ/NapCat 环境。测试失败时可以改选另一个好友 QQ。

### 发送前校验

正式归档和测试发送都会先检查：

- 文件存在；
- 文件大小大于 0；
- 扩展名与任务格式一致；
- 文件能被当前 XLSX/CSV/JSON/Markdown 读取器重新打开。

校验失败时不会上传，也不会推进检查点。

### 上传确认和重试

上传请求发出后必须收到匹配的 NapCat `echo` 成功响应。

等待确认超过 300 秒会判定失败，并进入原有重试流程。

上传失败或超时时：

- 保留旧文件；
- 保留检查点；
- 不重新调用 AI；
- 只重试已经生成的原文件；
- 不产生重复记录或重复归档。

### 发送记录

任务管理窗口中的“发送记录”会显示：

- 时间；
- 测试发送或定时归档；
- 成功或失败；
- 接收 QQ；
- 实际传输模式；
- 文件名和大小；
- 错误信息。

记录保存在：

```text
~/.qq_message_manager/automation_state.sqlite3
```

### 每日轮换顺序

每日发送顺序：

1. 补处理上次检查点到计划发送时间的剩余消息；
2. 合并尚未成功发送的旧归档记录；
3. 保存并重新校验当前文件；
4. 持久化“待上传”状态；
5. 调用 NapCat 上传文件；
6. 等待匹配的成功响应；
7. 推进检查点；
8. 删除已成功发送的旧文件；
9. 创建新一天的空文件。

程序在上传过程中退出时，重启后会继续上传原文件，而不会重新调用 AI。

## 问题收集任务示例

调度：

```text
每隔 30 分钟
```

Prompt：

```text
检查上次成功执行以来的群聊记录。

识别群成员提出的疑问、求助、Bug 反馈和功能需求。
闲聊、反问、玩笑和已经明确撤回的问题不记录。

出现新问题时新增一行。
同一问题出现补充信息时更新原记录。
后续有人给出明确答案或解决方案时，把状态更新为已完成，并填写回答摘要。
只能依据聊天记录，不得编造问题、答案或处理状态。
```

每日发送：

```text
时间：00:00
接收人：机器人自己或指定好友
传输：自动
发送成功后删除旧文件并创建新文件
```

## 本地数据

```text
QSettings：连接配置、AI 设置、Skill、定时任务定义、置顶和代管会话
~/.qq_message_manager/sticker_memory.json：表情包记忆和描述
~/.qq_message_manager/sticker_memory.locks.json：表情包锁定状态
~/.qq_message_manager/automation_state.sqlite3：任务检查点、状态、待上传文件和发送记录
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
    ├── automation_archive_patch.py
    ├── automation_feature.py
    ├── automation_file_import.py
    ├── automation_hardening.py
    ├── automation_models.py
    ├── automation_napcat.py
    ├── automation_patches.py
    ├── automation_record_context.py
    ├── automation_stage2_ui.py
    ├── automation_stage3_feature.py
    ├── automation_stage3_reliability.py
    ├── automation_stage3_transfer.py
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
