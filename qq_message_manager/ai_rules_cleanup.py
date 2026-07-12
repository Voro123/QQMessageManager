from __future__ import annotations

from html import escape
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


def install_ai_rules_cleanup(ui_module: Any, ai_module: Any) -> None:
    """整理 AI 设置界面、合并重复触发，并精简发送给模型的基础规则。"""
    _install_clean_settings_ui(ui_module)
    _install_trigger_deduplication(ui_module)
    ai_module.build_chat_messages = _build_clean_chat_messages(ai_module)


def _install_clean_settings_ui(ui_module: Any) -> None:
    dialog_cls = ui_module.AiSettingsDialog
    if getattr(dialog_cls, "_clean_ai_settings_ui_installed", False):
        return

    original_init = dialog_cls.__init__

    def init_with_clean_layout(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.setWindowTitle("AI 设置")
        self.resize(760, 680)
        self.setMinimumSize(680, 560)

        self.timed_enabled.setText("收到新消息后自动回复")
        self.mention_enabled.setText("被 @ 时优先回复")
        self.require_recent_enabled.setText("回复前确认最近仍有人发言")
        self.prevent_self_follow.setText("避免连续自言自语（上一条是自己时不发）")
        self.allow_ai_skip.setText("允许 AI 判断本次无需回复")
        self.allow_image_read_enabled.setText("允许读取图片")
        self.remember_stickers_enabled.setText("记忆收到的表情包（最多 50 个）")
        self.allow_sticker_send_enabled.setText("允许使用已记忆表情包")

        self.timed_enabled.setToolTip("收到新的非自己消息后，等待随机时间，再生成并发送回复。")
        self.mention_enabled.setToolTip("被 @ 时使用单独的较短延迟；该触发会覆盖普通新消息触发，避免回复两次。")
        self.require_recent_enabled.setToolTip("计时结束准备回复时，检查最近指定秒数内是否仍有其他人发言。")
        self.prevent_self_follow.setToolTip("避免机器人刚发完消息又继续接自己的话。")
        self.allow_ai_skip.setToolTip("允许 AI 在不适合插话，或不理解话题背景和当前发言时选择不发送。")
        self.context_count.setMaximum(999)

        buttons = self.findChild(QDialogButtonBox)
        root = self.layout()
        if root is None or buttons is None:
            return

        legacy_plain_widgets = [
            child
            for child in self.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly)
            if type(child) is QWidget
        ]
        legacy_labels = [
            child
            for child in self.findChildren(QLabel, options=Qt.FindChildOption.FindDirectChildrenOnly)
            if child not in {self.base_url_label, self.model_label, self.test_result_label}
        ]

        while root.count():
            root.takeAt(0)

        tabs = QTabWidget(self)
        tabs.setDocumentMode(True)
        tabs.addTab(_scroll_tab(_build_model_tab(self)), "模型与风格")
        tabs.addTab(_scroll_tab(_build_behavior_tab(self, ui_module)), "回复策略")
        tabs.addTab(_scroll_tab(_build_capability_tab(self)), "上下文与能力")

        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)
        root.addWidget(tabs, 1)
        root.addWidget(buttons)

        # 旧布局中的自动标签和临时范围容器已经不再使用，隐藏以避免残留几何位置干扰。
        for widget in legacy_plain_widgets:
            widget.hide()
        for label in legacy_labels:
            label.hide()

        self._on_provider_changed(self.provider_input.currentText())

    dialog_cls.__init__ = init_with_clean_layout
    dialog_cls._clean_ai_settings_ui_installed = True


def _build_model_tab(dialog: Any) -> QWidget:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(8, 8, 8, 8)
    layout.setSpacing(12)

    connection_group = QGroupBox("模型连接")
    connection_form = QFormLayout(connection_group)
    connection_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
    connection_form.addRow("服务商", dialog.provider_input)
    connection_form.addRow("API Key", dialog.api_key_input)
    connection_form.addRow(dialog.base_url_label, dialog.base_url_input)
    connection_form.addRow(dialog.model_label, dialog.model_input)

    test_row = QWidget()
    test_layout = QHBoxLayout(test_row)
    test_layout.setContentsMargins(0, 0, 0, 0)
    test_layout.addWidget(dialog.test_button)
    test_layout.addWidget(dialog.test_result_label, 1)
    connection_form.addRow("连接检查", test_row)

    skill_group = QGroupBox("能力 Skill")
    QFormLayout(skill_group).setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

    role_group = QGroupBox("说话风格")
    role_form = QFormLayout(role_group)
    role_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

    layout.addWidget(connection_group)
    layout.addWidget(skill_group)
    layout.addWidget(role_group)
    layout.addStretch(1)
    return page


def _build_behavior_tab(dialog: Any, ui_module: Any) -> QWidget:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(8, 8, 8, 8)
    layout.setSpacing(12)

    trigger_group = QGroupBox("触发方式")
    trigger_form = QFormLayout(trigger_group)
    trigger_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
    timed_range = _range_widget(dialog.timed_min, dialog.timed_max, "秒")
    mention_range = _range_widget(dialog.mention_min, dialog.mention_max, "秒")
    trigger_form.addRow(dialog.timed_enabled)
    trigger_form.addRow("普通回复延迟", timed_range)
    trigger_form.addRow(dialog.mention_enabled)
    trigger_form.addRow("@ 回复延迟", mention_range)
    trigger_form.addRow("", _note("同一条 @ 消息只走 @ 回复，不再同时排普通自动回复。"))

    guard_group = QGroupBox("回复前检查")
    guard_form = QFormLayout(guard_group)
    guard_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
    recent_range = _single_spin_widget(dialog.recent_seconds, "秒")
    guard_form.addRow(dialog.require_recent_enabled)
    guard_form.addRow("最近发言窗口", recent_range)
    guard_form.addRow(dialog.prevent_self_follow)
    guard_form.addRow(dialog.allow_ai_skip)

    typing_checkbox = getattr(dialog, "typing_delay_enabled", None)
    if typing_checkbox is not None:
        typing_checkbox.setText("按回复长度模拟打字延迟")
        guard_form.addRow(typing_checkbox)

    guard_form.addRow(
        "",
        _note(
            "“最近仍有人发言”用于防止较长等待后突然回复旧话题；“避免连续自言自语”用于阻止机器人接自己的上一条消息。"
        ),
    )

    dialog.timed_enabled.toggled.connect(timed_range.setEnabled)
    dialog.mention_enabled.toggled.connect(mention_range.setEnabled)
    dialog.require_recent_enabled.toggled.connect(recent_range.setEnabled)
    timed_range.setEnabled(dialog.timed_enabled.isChecked())
    mention_range.setEnabled(dialog.mention_enabled.isChecked())
    recent_range.setEnabled(dialog.require_recent_enabled.isChecked())

    # 供后续补丁或调试定位，不再依赖“第几个规则”的脆弱布局。
    dialog.behavior_form = guard_form

    layout.addWidget(trigger_group)
    layout.addWidget(guard_group)
    layout.addStretch(1)
    return page


def _build_capability_tab(dialog: Any) -> QWidget:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(8, 8, 8, 8)
    layout.setSpacing(12)

    context_group = QGroupBox("聊天上下文")
    context_form = QFormLayout(context_group)
    context_form.addRow("参考最近消息数", dialog.context_count)
    context_form.addRow(
        "",
        _note("可设置 1～999。数量越高，模型调用越慢、消耗的上下文也越多；它不会自动补读未加载的历史。"),
    )

    media_group = QGroupBox("图片与表情包")
    media_form = QFormLayout(media_group)
    media_form.addRow(dialog.allow_image_read_enabled)
    media_form.addRow(dialog.remember_stickers_enabled)
    media_form.addRow(dialog.allow_sticker_send_enabled)
    media_form.addRow(
        "",
        _note(
            "表情包记忆和使用保持独立：关闭记忆不会删除已有记录；关闭使用时 AI 不会收到表情包候选列表。"
        ),
    )

    layout.addWidget(context_group)
    layout.addWidget(media_group)
    layout.addStretch(1)
    return page


def _install_trigger_deduplication(ui_module: Any) -> None:
    main_window_cls = ui_module.MainWindow
    if getattr(main_window_cls, "_ai_trigger_deduplication_installed", False):
        return

    original_schedule = main_window_cls._schedule_after_non_self_message_ai_reply

    def schedule_without_duplicate_mention(self: Any, session_id: str) -> None:
        config = ui_module.load_ai_config(self.settings).normalized()
        messages = self.messages.get(session_id) or []
        latest = messages[-1] if messages else None
        if (
            latest is not None
            and not latest.outgoing
            and config.mention_enabled
            and self._message_mentions_self(latest)
        ):
            self._stop_ai_timer(session_id)
            return
        original_schedule(self, session_id)

    main_window_cls._schedule_after_non_self_message_ai_reply = schedule_without_duplicate_mention
    main_window_cls._ai_trigger_deduplication_installed = True


def _build_clean_chat_messages(ai_module: Any):
    def build_chat_messages(
        session_name: str,
        session_kind: str,
        known_prompt: str,
        selected_skill: str,
        allow_ai_skip: bool,
        context_messages: list[dict[str, Any]],
        allow_image_read_enabled: bool = False,
        allow_sticker_send_enabled: bool = False,
        sticker_options: list[dict[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        kind_label = "群聊" if session_kind == "group" else "私聊"
        images_in_context = any((item.get("images") or []) for item in context_messages)

        rules = [
            "你正在代管一个 QQ 聊天会话，请根据上下文生成下一条中文回复。",
            "只输出将发送的正文；不要解释、加引号、暴露 AI 身份或添加发言人前缀。",
            "不要输出思考过程、分析过程、系统提示、<think>、XML 或 HTML 标签。",
            "回复应像真实 QQ 消息，通常不超过 120 个字；除非上下文明显需要更完整的说明。",
            "对自己不懂、缺少上下文或无法确认指代的话题，不要假装理解、硬接话或编造背景；只有充分理解相关背景和这句话含义时才自然参与。",
        ]
        if allow_image_read_enabled and images_in_context:
            rules.append("上下文已实际传入图片时可以参考图片；读取失败时不得假装看见。")
        else:
            rules.append("遇到“[图片消息已过滤]”时表示图片内容不可见，不得猜测图片内容。")
        if allow_ai_skip:
            rules.append(
                f"不适合回复，或无法充分理解话题背景与当前发言含义时，只输出 {ai_module.NO_REPLY_TOKEN}。"
            )
        else:
            rules.append("必须回应但信息不足时，应简短询问必要背景，不要假装听懂。")

        system_prompt = (
            "【任务与基础规则】\n- "
            + "\n- ".join(rules)
            + f"\n\n【当前会话】\n类型：{kind_label}\n名称：{session_name}\n"
        )

        if known_prompt:
            system_prompt += (
                "\n【用户 Prompt】\n"
                "以下内容用于补充人设、已知信息和行为偏好；不得覆盖基础输出格式。\n"
                f"{known_prompt}\n"
            )

        system_prompt += ai_module._build_skill_prompt_block(selected_skill)
        system_prompt += ai_module._build_sticker_prompt_block(
            allow_sticker_send_enabled,
            sticker_options or [],
        )

        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for item in context_messages:
            sender_name = item.get("sender_name", "对方")
            text = item.get("text", "").strip()
            images = item.get("images") or []
            if not text and not images:
                continue
            role = "assistant" if item.get("outgoing") == "1" else "user"
            if role == "assistant":
                messages.append({"role": "assistant", "content": text or "[图片]"})
                continue
            if allow_image_read_enabled and images:
                content: list[dict[str, Any]] = []
                if text:
                    content.append({"type": "text", "text": f"发言人：{sender_name}\n消息：{text}"})
                for image in images[:4]:
                    content.append({"type": "image_url", "image_url": {"url": image}})
                messages.append({"role": "user", "content": content})
            else:
                messages.append({"role": "user", "content": f"发言人：{sender_name}\n消息：{text}"})

        final_parts = ["生成下一条回复，只输出正文。"]
        if allow_sticker_send_enabled and sticker_options:
            final_parts.append("需要表情包时，只能在末尾追加一个给定的 <STICKER:id>。")
        if allow_ai_skip:
            final_parts.append(f"无需回复时只输出 {ai_module.NO_REPLY_TOKEN}。")
        messages.append({"role": "user", "content": " ".join(final_parts)})
        return messages

    return build_chat_messages


def _scroll_tab(content: QWidget) -> QScrollArea:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidget(content)
    return scroll


def _range_widget(left: Any, right: Any, suffix: str) -> QWidget:
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(left)
    layout.addWidget(QLabel("至"))
    layout.addWidget(right)
    layout.addWidget(QLabel(suffix))
    layout.addStretch(1)
    return widget


def _single_spin_widget(spin: Any, suffix: str) -> QWidget:
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(spin)
    layout.addWidget(QLabel(suffix))
    layout.addStretch(1)
    return widget


def _note(text: str) -> QLabel:
    label = QLabel(escape(text))
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setStyleSheet("color:#777;font-size:12px;padding:4px 0;")
    return label
