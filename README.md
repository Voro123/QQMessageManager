# QQMessageManager

QQMessageManager 是一个基于 Python 桌面窗口的 QQ 消息统一管理客户端，用于通过 [NapCatQQ](https://github.com/NapNeko/NapCatQQ) 的 WebSocket 接口实时接收 QQ 私聊和群聊消息，并以接近 QQ 的会话列表 + 消息窗口形式展示。

默认连接地址：

```text
ws://127.0.0.1:6099/api/Debug/ws
```

端口和 Token 由用户在登录窗口中输入，Token 可以留空。

## 当前功能

- 登录页输入 NapCatQQ WebSocket 地址、端口与 Token
- 默认使用 `ws://127.0.0.1:6099/api/Debug/ws`
- 通过 WebSocket 实时监听 NapCatQQ / OneBot 消息事件
- 自动区分群聊消息和私聊消息
- 左侧 QQ 风格会话列表，显示群聊和私聊
- 右侧消息窗口展示当前会话的消息时间、发送者和内容
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

程序启动后会打开登录窗口：

- Host：默认 `127.0.0.1`
- Port：默认 `6099`
- Path：默认 `/api/Debug/ws`
- Token：填写 NapCatQQ 中配置的 access token，没有则留空

如果你已经有完整地址，也可以直接在“完整 WebSocket 地址”中填写，例如：

```text
ws://127.0.0.1:6099/api/Debug/ws
```

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

本项目当前是消息接收与统一展示客户端，不会主动发送 QQ 消息，也不会修改 NapCatQQ 配置。后续可以扩展：

- 消息搜索
- 本地消息持久化
- 多账号管理
- 快捷回复/发送消息
- 群聊过滤与置顶
- 托盘常驻提醒
