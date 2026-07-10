from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QFormLayout, QGroupBox, QSpinBox

AI_REQUEST_TIMEOUT_KEY = "ai/request_timeout_seconds"
DEFAULT_AI_REQUEST_TIMEOUT_SECONDS = 180
MIN_AI_REQUEST_TIMEOUT_SECONDS = 10
MAX_AI_REQUEST_TIMEOUT_SECONDS = 1800


def install_ai_request_timeout(
    ui_module: Any,
    ai_module: Any,
    image_generation_module: Any,
) -> None:
    """增加统一接口超时设置，并让文本、总结、测试和生图请求使用该值。"""
    _install_client_timeout_support(ui_module, ai_module)
    _install_timeout_setting(ui_module, ai_module, image_generation_module)
    _apply_runtime_timeout(
        ai_module,
        image_generation_module,
        _configured_timeout_seconds(ui_module),
    )


def _install_timeout_setting(
    ui_module: Any,
    ai_module: Any,
    image_generation_module: Any,
) -> None:
    dialog_cls = ui_module.AiSettingsDialog
    if getattr(dialog_cls, "_ai_request_timeout_setting_installed", False):
        return

    original_init = dialog_cls.__init__
    original_accept = dialog_cls.accept

    def init_with_request_timeout(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.api_timeout_input = QSpinBox(self)
        self.api_timeout_input.setRange(
            MIN_AI_REQUEST_TIMEOUT_SECONDS,
            MAX_AI_REQUEST_TIMEOUT_SECONDS,
        )
        self.api_timeout_input.setValue(
            _setting_int(
                self.settings,
                AI_REQUEST_TIMEOUT_KEY,
                DEFAULT_AI_REQUEST_TIMEOUT_SECONDS,
            )
        )
        self.api_timeout_input.setSuffix(" 秒")
        self.api_timeout_input.setAccelerated(True)
        self.api_timeout_input.setToolTip(
            "单次 AI HTTP 请求允许等待的最长时间。聊天回复、连接测试、聊天总结和图片生成都会使用该值；"
            "低速或推理模型建议设置为 180～600 秒。数值越大，接口失联时等待也越久。"
        )

        connection_form = _find_group_form(self, "模型连接") or _find_first_form(self)
        if connection_form is not None:
            connection_form.addRow("接口超时", self.api_timeout_input)

    def accept_with_request_timeout(self: Any) -> None:
        timeout_input = getattr(self, "api_timeout_input", None)
        if timeout_input is not None:
            seconds = _normalize_timeout(timeout_input.value())
            self.settings.setValue(AI_REQUEST_TIMEOUT_KEY, seconds)
            self.settings.sync()
            _apply_runtime_timeout(ai_module, image_generation_module, seconds)
        original_accept(self)

    dialog_cls.__init__ = init_with_request_timeout
    dialog_cls.accept = accept_with_request_timeout
    dialog_cls._ai_request_timeout_setting_installed = True


def _install_client_timeout_support(ui_module: Any, ai_module: Any) -> None:
    minimax_cls = ai_module.MinimaxM3Client
    openai_cls = ai_module.OpenAICompatibleClient

    if not getattr(minimax_cls, "_configurable_timeout_installed", False):
        original_minimax_init = minimax_cls.__init__

        def minimax_init_with_timeout(self: Any, *args: Any, **kwargs: Any) -> None:
            original_minimax_init(self, *args, **kwargs)
            self.timeout_seconds = _configured_timeout_seconds(ui_module)

        def minimax_post_json(
            self: Any,
            endpoint: str,
            payload: dict[str, Any],
        ) -> dict[str, Any]:
            return _post_json(
                endpoint,
                payload,
                {
                    "Authorization": "Bearer " + self.api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                _normalize_timeout(getattr(self, "timeout_seconds", DEFAULT_AI_REQUEST_TIMEOUT_SECONDS)),
                ai_module.AiProviderError,
            )

        minimax_cls.__init__ = minimax_init_with_timeout
        minimax_cls._post_json = minimax_post_json
        minimax_cls._configurable_timeout_installed = True

    if not getattr(openai_cls, "_configurable_timeout_installed", False):
        original_openai_init = openai_cls.__init__

        def openai_init_with_timeout(self: Any, *args: Any, **kwargs: Any) -> None:
            configured = _configured_timeout_seconds(ui_module)
            positional = list(args)
            if len(positional) >= 5:
                positional[4] = configured
            else:
                kwargs["timeout_seconds"] = configured
            original_openai_init(self, *positional, **kwargs)

        def openai_post_json(
            self: Any,
            endpoint: str,
            payload: dict[str, Any],
        ) -> dict[str, Any]:
            return _post_json(
                endpoint,
                payload,
                {
                    "Authorization": f"{self.auth_scheme} {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                _normalize_timeout(getattr(self, "timeout_seconds", DEFAULT_AI_REQUEST_TIMEOUT_SECONDS)),
                ai_module.AiProviderError,
            )

        openai_cls.__init__ = openai_init_with_timeout
        openai_cls._post_json = openai_post_json
        openai_cls._configurable_timeout_installed = True


def _post_json(
    endpoint: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout_seconds: int,
    error_type: type[RuntimeError],
) -> dict[str, Any]:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise error_type(f"HTTP {exc.code}: {body[:500]}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise error_type(
            f"接口请求超时（当前设置 {timeout_seconds} 秒），可在 AI 设置中调大“接口超时”"
        ) from exc
    except urllib.error.URLError as exc:
        if _is_timeout_reason(exc.reason):
            raise error_type(
                f"接口请求超时（当前设置 {timeout_seconds} 秒），可在 AI 设置中调大“接口超时”"
            ) from exc
        raise error_type(str(exc.reason)) from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise error_type(f"接口返回不是 JSON：{body[:500]}") from exc
    if not isinstance(parsed, dict):
        raise error_type("接口返回格式不是对象")
    return parsed


def _apply_runtime_timeout(
    ai_module: Any,
    image_generation_module: Any,
    seconds: int,
) -> None:
    seconds = _normalize_timeout(seconds)
    # 这些函数在调用时读取模块变量，因此保存设置后无需重启即可影响后续请求。
    ai_module.AI_REPLY_TIMEOUT_SECONDS = seconds
    ai_module.AI_TEST_TIMEOUT_SECONDS = seconds
    image_generation_module.IMAGE_GENERATION_TIMEOUT_SECONDS = seconds


def _configured_timeout_seconds(ui_module: Any) -> int:
    settings = QSettings(ui_module.SETTINGS_ORGANIZATION, ui_module.SETTINGS_APPLICATION)
    return _normalize_timeout(
        _setting_int(
            settings,
            AI_REQUEST_TIMEOUT_KEY,
            DEFAULT_AI_REQUEST_TIMEOUT_SECONDS,
        )
    )


def _normalize_timeout(value: Any) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = DEFAULT_AI_REQUEST_TIMEOUT_SECONDS
    return max(MIN_AI_REQUEST_TIMEOUT_SECONDS, min(seconds, MAX_AI_REQUEST_TIMEOUT_SECONDS))


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


def _setting_int(settings: Any, key: str, default: int) -> int:
    value = settings.value(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
