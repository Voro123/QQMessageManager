from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication

from . import ai_client as ai_module
from . import ai_summary as ai_summary_module
from . import ai_typing_delay as typing_delay_module
from . import automation_feature as automation_module
from . import automation_file_import as automation_file_import_module
from . import automation_stage3_feature as automation_stage3_feature_module
from . import automation_stage3_transfer as automation_stage3_transfer_module
from . import automation_storage as automation_storage_module
from . import chat_summary_feature as chat_summary_module
from . import chat_summary_skill as chat_summary_skill_module
from . import image_generation_feature as image_generation_module
from . import napcat_client as napcat_module
from . import skill_library_feature as skill_library_module
from . import sticker_library_feature as sticker_library_module
from . import sticker_memory as sticker_module
from . import ui as ui_module
from .ai_context_limit_patch import install_ai_context_message_limit
from .ai_min_speech_interval import install_ai_min_speech_interval
from .ai_request_timeout import install_ai_request_timeout
from .ai_rules_cleanup import install_ai_rules_cleanup
from .ai_typing_delay import install_ai_typing_delay
from .automation_archive_patch import install_automation_archive_patch
from .automation_behavior_fixes import install_automation_behavior_fixes
from .automation_editor_init_fix import install_automation_editor_init_fix
from .automation_editor_usability import install_automation_editor_usability
from .automation_feature import install_automation_feature
from .automation_file_import import install_automation_file_import
from .automation_hardening import install_automation_hardening
from .automation_message_buffer import install_automation_message_buffer
from .automation_patches import install_automation_patches
from .automation_record_context import install_automation_record_context
from .automation_stage2_ui import install_automation_stage2_ui
from .automation_stage3_feature import install_automation_stage3_feature
from .automation_stage3_reliability import install_automation_stage3_reliability
from .automation_stage3_transfer import install_automation_stage3_transfer
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
from .sticker_send_reliability import install_sticker_send_reliability
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
# 统一接口超时覆盖聊天、连接测试、总结和生图；保存后会即时更新运行时值。
install_ai_request_timeout(ui_module, ai_module, image_generation_module)
# Skill 库统一管理普通聊天角色和能力；定时文件 Skill 由任务系统单独隔离。
install_skill_library_feature(ui_module, ai_module)
install_chat_summary_skill(ui_module, chat_summary_module, ai_summary_module)
install_chat_summary_people_filter_patch(chat_summary_skill_module)
# 定时任务系统负责调度、可信任务上下文、受限文件工作区和 NapCat 文件上传。
install_automation_feature(ui_module, ai_module, napcat_module)
# 第三阶段传输层必须在基础 NapCat 自动化动作安装后接入。
install_automation_stage3_transfer(napcat_module)
install_automation_patches(automation_module, skill_library_module, ui_module)
install_automation_hardening(automation_module, ui_module)
# 第二阶段先安装真实文件读取、已有记录优先级和用户导入，再让归档层捕获新的读取器。
install_automation_file_import(automation_module, automation_storage_module)
install_automation_record_context(automation_module)
install_automation_stage2_ui(
    automation_module,
    automation_file_import_module,
    automation_storage_module,
)
install_automation_archive_patch(automation_module)
# 第三阶段统一增加发送前校验、测试发送、好友探测和发送记录。
install_automation_stage3_feature(
    automation_module,
    automation_file_import_module,
    automation_storage_module,
    ui_module,
)
# 最后安装上传确认超时和 Stream 多段响应保护，覆盖第三阶段所有发送入口。
install_automation_stage3_reliability(
    automation_module,
    automation_stage3_transfer_module,
    automation_stage3_feature_module,
    napcat_module,
)
# 目标会话使用可刷新的群聊/好友下拉框；调度方式只显示当前相关字段。
install_automation_editor_usability(automation_module, napcat_module, ui_module)
# 基础构造函数会在扩展控件创建前调用 _sync_controls，必须最后增加初始化保护。
install_automation_editor_init_fix(automation_module)
# 修复归档删除选项，并确保到点时即使没有新消息也照常执行任务指令。
install_automation_behavior_fixes(automation_module)
# 定时任务直接订阅与界面回显相同的实时消息信号，按会话缓存在内存中。
# 每个任务使用独立的本地递增序号游标；成功后推进，失败时保持不变。
install_automation_message_buffer(automation_module, ui_module)
# 表情包库先提供预览/锁定和摘要编辑，再把普通图片表情固化并用 base64 发送。
install_sticker_library_feature(ui_module, sticker_module)
install_sticker_metadata_editor(sticker_module, sticker_library_module)
install_sticker_send_reliability(ui_module, sticker_module)
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
