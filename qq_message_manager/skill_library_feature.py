from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

ENABLED_SKILLS_KEY = "ai/enabled_skills"
SUMMARY_SKILL_ID = "chat_summary"
VISION_SKILL_ID = "vision"
IMAGE_GENERATION_SKILL_ID = "image_generation"
FUNCTIONAL_SKILL_IDS = {SUMMARY_SKILL_ID, VISION_SKILL_ID, IMAGE_GENERATION_SKILL_ID}
DEFAULT_ENABLED_SKILLS = {SUMMARY_SKILL_ID}


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    skill_id: str
    name: str
    category: str
    description: str
    functional: bool = False


BUILTIN_SKILLS = (
    SkillDefinition(
        "shuimen",
        "shuimen",
        "角色 Skill",
        "加载水门角色的说话格式、表达风格和互动规则。",
    ),
    SkillDefinition(
        VISION_SKILL_ID,
        "图片理解",
        "能力 Skill",
        "允许支持视觉输入的模型读取聊天图片，并使用内部视觉规则理解截图和表情包。",
        functional=True,
    ),
    SkillDefinition(
        IMAGE_GENERATION_SKILL_ID,
        "图片生成",
        "能力 Skill",
        "识别明确的画图请求，调用已选择的生图模型并把结果发送到当前会话。",
        functional=True,
    ),
    SkillDefinition(
        SUMMARY_SKILL_ID,
        "聊天总结",
        "能力 Skill",
        "总结当前群聊或私聊最近指定数量的消息；默认 200 条，并把总结直接发送出去。",
        functional=True,
    ),
)


def install_skill_library_feature(ui_module: Any, ai_module: Any) -> None:
    """把角色/能力 Skill 统一到一个可多选加载的 Skill 库。"""
    _install_settings_library(ui_module, ai_module)
    _install_multi_skill_prompt(ai_module, ui_module)
    _install_main_window_state_sync(ui_module)


def available_skills(ai_module: Any) -> list[SkillDefinition]:
    definitions = {item.skill_id: item for item in BUILTIN_SKILLS}
    skills_dir = Path(ai_module.SKILLS_DIR)
    try:
        children = sorted(skills_dir.iterdir(), key=lambda path: path.name.lower())
    except OSError:
        children = []
    for child in children:
        if not child.is_dir() or not (child / "SKILL.md").is_file() or child.name in definitions:
            continue
        definitions[child.name] = SkillDefinition(
            child.name,
            _skill_heading(child / "SKILL.md") or child.name,
            "扩展 Skill",
            _skill_description(child / "SKILL.md") or "仓库中的可加载扩展 Skill。",
        )
    order = {item.skill_id: index for index, item in enumerate(BUILTIN_SKILLS)}
    return sorted(
        definitions.values(),
        key=lambda item: (order.get(item.skill_id, 10_000), item.category, item.name.lower()),
    )


def enabled_skill_ids(settings: Any, *, migrate: bool = True) -> set[str]:
    if settings.contains(ENABLED_SKILLS_KEY):
        return _parse_skill_ids(settings.value(ENABLED_SKILLS_KEY, "[]"))

    enabled = set(DEFAULT_ENABLED_SKILLS)
    legacy_skill = str(settings.value("ai/selected_skill", "") or "").strip()
    if legacy_skill:
        enabled.add(legacy_skill)
    if _setting_bool(settings, "ai/allow_image_read_enabled", False):
        enabled.add(VISION_SKILL_ID)
    if _setting_bool(settings, "ai/image_generation_enabled", False):
        enabled.add(IMAGE_GENERATION_SKILL_ID)

    if migrate:
        save_enabled_skill_ids(settings, enabled)
    return enabled


def save_enabled_skill_ids(settings: Any, skill_ids: set[str]) -> None:
    normalized = sorted({str(skill_id).strip() for skill_id in skill_ids if str(skill_id).strip()})
    settings.setValue(ENABLED_SKILLS_KEY, json.dumps(normalized, ensure_ascii=False))
    settings.sync()


def is_skill_enabled(settings: Any, skill_id: str) -> bool:
    return skill_id in enabled_skill_ids(settings)


def _install_settings_library(ui_module: Any, ai_module: Any) -> None:
    dialog_cls = ui_module.AiSettingsDialog
    if getattr(dialog_cls, "_skill_library_installed", False):
        return

    original_init = dialog_cls.__init__
    original_accept = dialog_cls.accept

    def init_with_skill_library(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.pending_enabled_skills = enabled_skill_ids(self.settings)
        self.skill_library_button = QPushButton("选择加载…")
        self.skill_library_button.setToolTip("打开 Skill 库，可同时加载多个角色或能力 Skill")
        self.skill_library_summary = QLabel()
        self.skill_library_summary.setWordWrap(True)
        self.skill_library_summary.setStyleSheet("color:#667085;")
        self.skill_library_button.clicked.connect(lambda: _open_skill_library(self, ai_module))

        row = QWidget(self)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(self.skill_library_button)
        row_layout.addWidget(self.skill_library_summary, 1)

        role_form = _find_group_form(self, "角色与表达")
        if role_form is not None:
            role_form.insertRow(0, "Skill 库", row)
        else:
            fallback = _find_first_form(self)
            if fallback is not None:
                fallback.addRow("Skill 库", row)

        _hide_legacy_skill_controls(self)
        _sync_dialog_controls(self)
        _refresh_library_summary(self, ai_module)

    def accept_with_skill_library(self: Any) -> None:
        selected = set(getattr(self, "pending_enabled_skills", set()))
        _sync_dialog_controls(self)
        save_enabled_skill_ids(self.settings, selected)
        original_accept(self)

    dialog_cls.__init__ = init_with_skill_library
    dialog_cls.accept = accept_with_skill_library
    dialog_cls._skill_library_installed = True


def _open_skill_library(dialog: Any, ai_module: Any) -> None:
    library = SkillLibraryDialog(
        available_skills(ai_module),
        set(getattr(dialog, "pending_enabled_skills", set())),
        dialog,
    )
    if library.exec() != QDialog.DialogCode.Accepted:
        return
    dialog.pending_enabled_skills = library.selected_skill_ids()
    _sync_dialog_controls(dialog)
    _refresh_library_summary(dialog, ai_module)


def _sync_dialog_controls(dialog: Any) -> None:
    selected = set(getattr(dialog, "pending_enabled_skills", set()))

    skill_input = getattr(dialog, "skill_input", None)
    if skill_input is not None:
        prompt_skill = "shuimen" if "shuimen" in selected else ""
        index = skill_input.findData(prompt_skill)
        skill_input.setCurrentIndex(index if index >= 0 else 0)

    vision_checkbox = getattr(dialog, "allow_image_read_enabled", None)
    if vision_checkbox is not None:
        vision_checkbox.setChecked(VISION_SKILL_ID in selected)

    image_generation_checkbox = getattr(dialog, "image_generation_enabled", None)
    if image_generation_checkbox is not None:
        image_generation_checkbox.setChecked(IMAGE_GENERATION_SKILL_ID in selected)


def _hide_legacy_skill_controls(dialog: Any) -> None:
    skill_input = getattr(dialog, "skill_input", None)
    if skill_input is not None:
        parent_layout = skill_input.parentWidget().layout() if skill_input.parentWidget() is not None else None
        if isinstance(parent_layout, QFormLayout):
            label = parent_layout.labelForField(skill_input)
            if label is not None:
                label.hide()
        skill_input.hide()

    for name in ("allow_image_read_enabled", "image_generation_enabled"):
        widget = getattr(dialog, name, None)
        if widget is not None:
            widget.hide()


def _refresh_library_summary(dialog: Any, ai_module: Any) -> None:
    selected = set(getattr(dialog, "pending_enabled_skills", set()))
    names = [item.name for item in available_skills(ai_module) if item.skill_id in selected]
    dialog.skill_library_summary.setText("已加载：" + "、".join(names) if names else "当前未加载 Skill")


def _install_multi_skill_prompt(ai_module: Any, ui_module: Any) -> None:
    if getattr(ai_module, "_multi_skill_prompt_installed", False):
        return
    original_builder = ai_module._build_skill_prompt_block

    def build_multi_skill_prompt(selected_skill: str) -> str:
        settings = QSettings(ui_module.SETTINGS_ORGANIZATION, ui_module.SETTINGS_APPLICATION)
        selected = enabled_skill_ids(settings, migrate=False)
        prompt_skill_ids = [
            item.skill_id
            for item in available_skills(ai_module)
            if item.skill_id in selected and item.skill_id not in FUNCTIONAL_SKILL_IDS
        ]
        if not prompt_skill_ids:
            return original_builder(selected_skill) if selected_skill else ""

        blocks: list[str] = []
        for skill_id in prompt_skill_ids:
            skill_text = ai_module._load_skill_text(skill_id)
            if not skill_text:
                continue
            blocks.append(
                f"【已加载 Skill：{skill_id}】\n"
                "以下内容用于规定角色、表达风格、口癖或扩展行为。"
                "多个 Skill 同时加载时都应遵守；冲突时以更具体的规则为准。\n"
                f"{skill_text}\n"
                f"【已加载 Skill 结束：{skill_id}】"
            )
        if not blocks:
            return ""
        return "\n\n" + "\n\n".join(blocks) + "\n"

    ai_module._build_skill_prompt_block = build_multi_skill_prompt
    ai_module._multi_skill_prompt_installed = True


def _install_main_window_state_sync(ui_module: Any) -> None:
    main_window_cls = ui_module.MainWindow
    if getattr(main_window_cls, "_skill_library_window_sync_installed", False):
        return

    original_init = main_window_cls.__init__
    original_open_ai_settings = main_window_cls.open_ai_settings

    def init_with_skill_state(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        _sync_window_skill_state(self)

    def open_ai_settings_with_skill_state(self: Any) -> None:
        original_open_ai_settings(self)
        _sync_window_skill_state(self)

    main_window_cls.__init__ = init_with_skill_state
    main_window_cls.open_ai_settings = open_ai_settings_with_skill_state
    main_window_cls._skill_library_window_sync_installed = True


def _sync_window_skill_state(window: Any) -> None:
    summary_button = getattr(window, "summary_button", None)
    if summary_button is not None:
        loaded = is_skill_enabled(window.settings, SUMMARY_SKILL_ID)
        summary_button.setVisible(loaded)
        summary_button.setEnabled(loaded)
        summary_button.setToolTip(
            "总结当前会话并把结果发送出去"
            if loaded
            else "请先在 AI 设置的 Skill 库中加载“聊天总结”"
        )


class SkillLibraryDialog(QDialog):
    def __init__(
        self,
        definitions: list[SkillDefinition],
        selected: set[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.definitions = definitions
        self.setWindowTitle("Skill 库")
        self.resize(720, 560)
        self.setMinimumSize(620, 460)

        heading = QLabel("选择需要加载的 Skill")
        heading.setStyleSheet("font-size:18px;font-weight:600;")
        tip = QLabel(
            "可以同时加载多个 Skill。能力 Skill 控制图片理解、图片生成和聊天总结；"
            "角色/扩展 Skill 会注入普通聊天提示词。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#667085;")

        self.list_widget = QListWidget()
        self.list_widget.setSpacing(6)
        for definition in definitions:
            item = QListWidgetItem(
                f"{definition.name}  ·  {definition.category}\n{definition.description}"
            )
            item.setData(Qt.ItemDataRole.UserRole, definition.skill_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if definition.skill_id in selected else Qt.CheckState.Unchecked
            )
            item.setToolTip(str((Path(__file__).resolve().parent / "skills" / definition.skill_id / "SKILL.md")))
            self.list_widget.addItem(item)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(tip)
        layout.addWidget(self.list_widget, 1)
        layout.addWidget(buttons)

    def selected_skill_ids(self) -> set[str]:
        selected: set[str] = set()
        for index in range(self.list_widget.count()):
            item = self.list_widget.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                skill_id = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
                if skill_id:
                    selected.add(skill_id)
        return selected


def _find_group_form(dialog: Any, title: str) -> QFormLayout | None:
    for group in dialog.findChildren(QGroupBox):
        if group.title() != title:
            continue
        layout = group.layout()
        if isinstance(layout, QFormLayout):
            return layout
    return None


def _find_first_form(dialog: Any) -> QFormLayout | None:
    forms = dialog.findChildren(QFormLayout)
    return forms[0] if forms else None


def _parse_skill_ids(raw: Any) -> set[str]:
    try:
        values = json.loads(str(raw))
    except json.JSONDecodeError:
        return set()
    if not isinstance(values, list):
        return set()
    return {str(value).strip() for value in values if str(value).strip()}


def _setting_bool(settings: Any, key: str, default: bool) -> bool:
    value = settings.value(key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _skill_heading(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip()
    except OSError:
        pass
    return ""


def _skill_description(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:120]
    return ""
