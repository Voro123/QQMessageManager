from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication

from . import ai_client as ai_module
from . import ai_summary as ai_summary_module
from . import ai_typing_delay as typing_delay_module
from . import chat_summary_feature as chat_summary_module
from . import chat_summary_skill as chat_summary_skill_module
from . import image_generation_feature as image_generation_module
from . import napcat_client as napcat_module
from . import sticker_library_feature as sticker_library_module
from . import sticker_memory as sticker_module
from . import ui as ui_module
from .ai_context_limit_patch import install_ai_context_message_limit
from .ai_min_speech_interval import install_ai_min_speech_interval
from .ai_rules_cleanup import install_ai_rules_cleanup
from .ai_typing_delay import install_ai_typing_delay
from .button_position_patch import install_summary_send_button_swap
from .chat_summary_feature import install_chat_summary_feature
from .chat_summary_people_patch import install_chat_summary_people_filter_patch
from .chat_summary_skill import install_chat_summary_skill
from .image_generation_feature import install_image_generation_feature
from .image_generation_model_selector import install_image_generation_model_selector
from .image_generation_toggle_patch import install_image_generation_toggle
from .image_layout_patch import install_image_layout_fix
from .return_to_login_patch import install_return_to_login
from .skill_library_feature import install_skill_library_feature
from .sticker_library_feature import install_sticker_library_feature
from .sticker_metadata_editor import install_sticker_metadata_editor
from .ui import QQMessageManagerApp, SETTINGS_APPLICATION, SETTINGS_ORGANIZATION
from .vision_input_patch import install_vision_input


# 先安装会给 AI 设置追加控件的补丁，再统一重排设置界面。
install_ai_typing_delay(ui_module)
install_ai_context_message_limit(ui_module)
install_ai_rules_cleanup(ui_module, ai_module)
install_chat_summary_feature(ui_module, napcat_module)
install_summary_send_button_swap(ui_module)
install_image_layout_fix(ui_module)
# 视觉输入必须在规则整理和图片裁剪之后安装，确保图片提示及裁剪预览进入最终请求。
install_vision_input(ui_module, ai_module)
# 先安装图片生成后端和开关，再增加与服务商联动的独立生图模型选择。
install_image_generation_feature(ui_module, ai_module, napcat_module)
install_image_generation_toggle(ui_module, image_generation_module, ai_module)
install_image_generation_model_selector(ui_module, ai_module, image_generation_module)
# Skill 库统一管理角色和能力；总结 Skill 安装在图片生成触发器之后，避免同一消息重复处理。
install_skill_library_feature(ui_module, ai_module)
install_chat_summary_skill(ui_module, chat_summary_module, ai_summary_module)
install_chat_summary_people_filter_patch(chat_summary_skill_module)
# 表情包库先提供预览/锁定能力，再增加摘要和使用时机编辑。
install_sticker_library_feature(ui_module, sticker_module)
install_sticker_metadata_editor(sticker_module, sticker_library_module)
install_return_to_login(ui_module)
# 最小发言间隔必须最后安装，以统一覆盖普通回复、表情包、生图和聊天总结的实际发送路径。
install_ai_min_speech_interval(
    ui_module,
    typing_delay_module,
    image_generation_module,
    chat_summary_skill_module,
)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    app = QApplication(sys.argv)
    app.setOrganizationName(SETTINGS_ORGANIZATION)
    app.setApplicationName(SETTINGS_APPLICATION)
    manager = QQMessageManagerApp(app)
    manager.show()
    return app.exec()
