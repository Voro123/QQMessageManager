from __future__ import annotations

import base64
import json
from typing import Any

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QWidget

IMAGE_GENERATION_MODELS_KEY = "ai/image_generation_models"
MINIMAX_IMAGE_ENDPOINTS = (
    "https://api.minimax.io/v1/image_generation",
    "https://api.minimaxi.com/v1/image_generation",
    "https://api.minimax.chat/v1/image_generation",
)


def install_image_generation_model_selector(
    ui_module: Any,
    ai_module: Any,
    generation_module: Any,
) -> None:
    """安装与服务商联动的生图模型选择，并让图片生成后端使用该选择。"""
    _install_selector_ui(ui_module, ai_module)
    _install_generation_backend(ui_module, ai_module, generation_module)


def _install_selector_ui(ui_module: Any, ai_module: Any) -> None:
    dialog_cls = ui_module.AiSettingsDialog
    if getattr(dialog_cls, "_image_generation_model_selector_installed", False):
        return

    original_init = dialog_cls.__init__
    original_accept = dialog_cls.accept

    def init_with_generation_model_selector(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.image_generation_model_values = _read_model_values(self.settings)
        self.image_generation_model_input = QComboBox()
        self.image_generation_model_input.setMinimumWidth(190)
        self.image_generation_model_input.setToolTip(
            "生图模型与聊天模型相互独立。切换服务商时会自动刷新可用模型；"
            "自定义服务商可以直接输入模型名。"
        )

        _place_selector_next_to_provider(self)
        self.provider_input.currentTextChanged.connect(
            lambda provider: _sync_selector(self, ai_module, provider)
        )
        self.image_generation_model_input.currentTextChanged.connect(
            lambda _text: _remember_current_value(self)
        )
        generation_toggle = getattr(self, "image_generation_enabled", None)
        if generation_toggle is not None:
            generation_toggle.toggled.connect(
                lambda _checked: _sync_selector_enabled(self, ai_module)
            )

        _sync_selector(self, ai_module, self.provider_input.currentText(), remember_previous=False)

    def accept_with_generation_model_selector(self: Any) -> None:
        _remember_current_value(self)
        self.settings.setValue(
            IMAGE_GENERATION_MODELS_KEY,
            json.dumps(self.image_generation_model_values, ensure_ascii=False),
        )
        self.settings.sync()
        original_accept(self)

    dialog_cls.__init__ = init_with_generation_model_selector
    dialog_cls.accept = accept_with_generation_model_selector
    dialog_cls._image_generation_model_selector_installed = True


def _place_selector_next_to_provider(dialog: Any) -> None:
    connection_form = _find_group_form(dialog, "模型连接")
    if connection_form is None:
        fallback = _find_first_form(dialog)
        if fallback is not None:
            fallback.addRow("生图模型", dialog.image_generation_model_input)
        return

    row, role = connection_form.getWidgetPosition(dialog.provider_input)
    if row < 0:
        connection_form.insertRow(0, "生图模型", dialog.image_generation_model_input)
        return

    connection_form.removeWidget(dialog.provider_input)
    provider_row = QWidget(dialog)
    row_layout = QHBoxLayout(provider_row)
    row_layout.setContentsMargins(0, 0, 0, 0)
    row_layout.setSpacing(10)
    row_layout.addWidget(dialog.provider_input, 1)
    row_layout.addWidget(QLabel("生图模型", provider_row))
    row_layout.addWidget(dialog.image_generation_model_input, 1)
    connection_form.setWidget(row, QFormLayout.ItemRole.FieldRole, provider_row)


def _sync_selector(
    dialog: Any,
    ai_module: Any,
    provider: str,
    *,
    remember_previous: bool = True,
) -> None:
    if remember_previous:
        _remember_current_value(dialog)

    combo = dialog.image_generation_model_input
    combo.blockSignals(True)
    combo.clear()
    combo.setEditable(False)

    options = _models_for_provider(ai_module, provider)
    saved = str(dialog.image_generation_model_values.get(provider) or "").strip()

    if provider == ai_module.AI_PROVIDER_CUSTOM:
        combo.setEditable(True)
        if saved:
            combo.addItem(saved)
            combo.setCurrentText(saved)
        combo.setPlaceholderText("输入自定义生图模型名")
        combo.setToolTip(
            "自定义服务商无法自动识别模型列表，请填写该接口实际支持的图片生成模型名。"
        )
    elif options:
        combo.addItems(options)
        selected = saved if saved in options else options[0]
        combo.setCurrentText(selected)
        combo.setToolTip(
            "列表会随服务商联动。模型最终是否可用，以当前账号权限和接口响应为准。"
        )
    else:
        combo.addItem("无可用生图模型", "")
        combo.setCurrentIndex(0)
        combo.setToolTip("当前服务商在本程序中没有已配置的图片生成模型。")

    combo.blockSignals(False)
    dialog._image_generation_model_provider = provider
    _remember_current_value(dialog)
    _sync_selector_enabled(dialog, ai_module)


def _sync_selector_enabled(dialog: Any, ai_module: Any) -> None:
    provider = dialog.provider_input.currentText()
    generation_toggle = getattr(dialog, "image_generation_enabled", None)
    generation_enabled = generation_toggle is None or generation_toggle.isChecked()
    provider_supported = provider == ai_module.AI_PROVIDER_CUSTOM or bool(
        _models_for_provider(ai_module, provider)
    )
    dialog.image_generation_model_input.setEnabled(generation_enabled and provider_supported)


def _remember_current_value(dialog: Any) -> None:
    provider = getattr(dialog, "_image_generation_model_provider", "")
    if not provider:
        return
    value = dialog.image_generation_model_input.currentText().strip()
    if value and value != "无可用生图模型":
        dialog.image_generation_model_values[provider] = value
    elif provider in dialog.image_generation_model_values:
        dialog.image_generation_model_values.pop(provider, None)


def _models_for_provider(ai_module: Any, provider: str) -> tuple[str, ...]:
    if provider == ai_module.AI_PROVIDER_MINIMAX_M3:
        # image-01 是 MiniMax 官方文档模型；image-01-live 按用户需要保留为可选项。
        return ("image-01", "image-01-live")
    if provider == ai_module.AI_PROVIDER_OPENAI:
        return (
            "gpt-image-2",
            "gpt-image-1.5",
            "gpt-image-1",
            "gpt-image-1-mini",
        )
    if provider == ai_module.AI_PROVIDER_DEEPSEEK:
        return ()
    return ()


def _read_model_values(settings: Any) -> dict[str, str]:
    raw = settings.value(IMAGE_GENERATION_MODELS_KEY, "{}")
    try:
        value = json.loads(str(raw))
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(provider): str(model).strip()
        for provider, model in value.items()
        if str(provider).strip() and str(model).strip()
    }


def _selected_generation_model(ui_module: Any, ai_module: Any, provider: str) -> str:
    settings = QSettings(ui_module.SETTINGS_ORGANIZATION, ui_module.SETTINGS_APPLICATION)
    values = _read_model_values(settings)
    selected = str(values.get(provider) or "").strip()
    options = _models_for_provider(ai_module, provider)
    if provider == ai_module.AI_PROVIDER_CUSTOM:
        return selected
    if selected in options:
        return selected
    return options[0] if options else ""


def _install_generation_backend(ui_module: Any, ai_module: Any, generation_module: Any) -> None:
    if getattr(generation_module, "_generation_model_selector_backend_installed", False):
        return

    original_generate_image = generation_module.generate_image

    def resolve_backend(config: Any, _ai_module: Any) -> Any:
        if not config.api_key:
            raise generation_module.ImageGenerationConfigurationError("图片生成未配置 API Key。")

        provider = config.provider
        model = _selected_generation_model(ui_module, ai_module, provider)
        if not model:
            raise generation_module.UnsupportedImageModel("未选择可用生图模型")

        if provider == ai_module.AI_PROVIDER_MINIMAX_M3:
            if model not in _models_for_provider(ai_module, provider):
                raise generation_module.UnsupportedImageModel(model)
            return generation_module.ImageGenerationBackend(
                "minimax_images",
                MINIMAX_IMAGE_ENDPOINTS[0],
                model,
                config.api_key,
            )

        if provider == ai_module.AI_PROVIDER_OPENAI:
            root = generation_module._api_root(config.base_url or ai_module.OPENAI_CHAT_ENDPOINT)  # noqa: SLF001
            return generation_module.ImageGenerationBackend(
                "images",
                f"{root}/images/generations",
                model,
                config.api_key,
            )

        if provider == ai_module.AI_PROVIDER_CUSTOM:
            if not config.base_url:
                raise generation_module.ImageGenerationConfigurationError("图片生成未配置 API 地址。")
            root = generation_module._api_root(config.base_url)  # noqa: SLF001
            kind = "responses" if generation_module.GPT5_MODEL_RE.search(model) else "images"
            endpoint = f"{root}/responses" if kind == "responses" else f"{root}/images/generations"
            return generation_module.ImageGenerationBackend(kind, endpoint, model, config.api_key)

        raise generation_module.UnsupportedImageModel(model)

    def current_model_name(config: Any, _ai_module: Any) -> str:
        return _selected_generation_model(ui_module, ai_module, config.provider) or "未选择生图模型"

    def generate_image(backend: Any, prompt: str) -> str:
        if backend.kind != "minimax_images":
            return original_generate_image(backend, prompt)
        return _generate_minimax_image(generation_module, backend, prompt)

    generation_module._resolve_backend = resolve_backend
    generation_module._current_model_name = current_model_name
    generation_module.generate_image = generate_image
    generation_module._generation_model_selector_backend_installed = True


def _generate_minimax_image(generation_module: Any, backend: Any, prompt: str) -> str:
    user_prompt = prompt.strip()[:1350]
    generation_prompt = (
        f"{user_prompt}\n\n"
        "生成要求：严格遵循用户描述，只生成一张图；不要添加未要求的水印、签名、说明文字或额外主体。"
    )[:1500]
    payload = {
        "model": backend.model,
        "prompt": generation_prompt,
        "aspect_ratio": "1:1",
        "response_format": "base64",
        "n": 1,
        "prompt_optimizer": True,
    }

    errors: list[str] = []
    endpoints = (backend.endpoint,) + tuple(
        endpoint for endpoint in MINIMAX_IMAGE_ENDPOINTS if endpoint != backend.endpoint
    )
    for endpoint in endpoints:
        try:
            response = generation_module._post_json(endpoint, backend.api_key, payload)  # noqa: SLF001
            image_bytes = _extract_minimax_image(generation_module, response, backend.api_key)
            if not image_bytes:
                base_resp = response.get("base_resp") or {}
                message = str(base_resp.get("status_msg") or "接口没有返回可用图片")
                errors.append(f"{endpoint}: {message}")
                continue
            if len(image_bytes) > generation_module.IMAGE_GENERATION_MAX_BYTES:
                raise generation_module.ImageGenerationError("生成图片超过允许大小")
            return generation_module._save_generated_image(image_bytes, prompt)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{endpoint}: {exc}")

    raise generation_module.ImageGenerationError(
        "MiniMax 图片生成失败：" + "；".join(errors[-2:])
    )


def _extract_minimax_image(generation_module: Any, response: dict[str, Any], api_key: str) -> bytes:
    data = response.get("data")
    if not isinstance(data, dict):
        return b""

    encoded_values = data.get("image_base64")
    if isinstance(encoded_values, list) and encoded_values:
        encoded = encoded_values[0]
        if isinstance(encoded, str) and encoded:
            try:
                return base64.b64decode(encoded, validate=False)
            except Exception as exc:  # noqa: BLE001
                raise generation_module.ImageGenerationError(
                    "MiniMax 返回了无效的 Base64 图片"
                ) from exc

    image_urls = data.get("image_urls")
    if isinstance(image_urls, list) and image_urls:
        url = image_urls[0]
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            return generation_module._download_generated_image(url, api_key)  # noqa: SLF001
    return b""


def _find_group_form(dialog: Any, title: str) -> QFormLayout | None:
    for group in dialog.findChildren(QGroupBox):
        if group.title() != title:
            continue
        layout = group.layout()
        if isinstance(layout, QFormLayout):
            return layout
    return None


def _find_first_form(dialog: Any) -> QFormLayout | None:
    for form in dialog.findChildren(QFormLayout):
        return form
    return None
