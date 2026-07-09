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
- 通过 WebSocket 实时监听 NapCatQQ / OneBot 消息事件
- 自动区分群聊消息和私聊消息
- 自动调用 `get_group_info` 获取群名，并在会话列表和聊天标题中显示真实群名
- 左侧 QQ 风格会话列表，显示群聊和私聊
- 右侧消息窗口展示当前会话的消息时间、发送者和内容
- 底部输入框发送文本消息，群聊使用 `send_group_msg`，私聊使用 `send_private_msg`
- 未读消息计数
- 自动重连与连接状态提示
- 支持常见 CQ 码/消息段的文本化展示，例如图片、语音、表情、@、回复等

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

如果你已经有完整地址，也可以直接在“完整 WebSocket 地址”中填写，例如：

```text
ws://127.0.0.1:3001
```

注意：NapCatQQ 的 WebUI 管理端口常见为 `6099`，不要把 WebUI 地址或调试地址当作本程序的消息接收地址。

## 使用说明

- 点击“连接”后，程序会把本次填写的完整地址、Host、Port、Path、Token 和连接模式保存到当前系统用户的 Qt 配置中；下次启动会自动填回登录窗口。
- 收到群消息后，程序会自动通过 `get_group_info` 请求群信息；请求成功后，左侧会话列表和右侧聊天标题会从“群聊 123456”更新成真实群名。
- 选择左侧任意私聊或群聊，在底部输入框输入文字并按 Enter 或点击“发送”即可发送文本消息。
- 建议 NapCatQQ 的 `reportSelfMessage` 保持 `false`，因为程序会在本地立即显示自己发出的消息，避免服务端再回推自己的消息导致重复显示。

## 项目结构

```text
.
├── main.py
├── requirements.txt
└── qq_message_manager
    ├── __init__.py
    ├── __main__.py
    ├── app.py
    ├── models.py
    ├── napcat_client.py
    └── ui.py
```

## 说明

本项目当前支持消息接收、群名展示和文本消息发送。后续可以扩展：

- 消息搜索
- 本地消息持久化
- 多账号管理
- 图片/文件发送
- 群聊过滤与置顶
- 托盘常驻提醒
