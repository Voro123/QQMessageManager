from __future__ import annotations

import re
from typing import Any


# 这些词描述的是“当前聊天参与者/当前会话”，不是具体的人员筛选参数。
# 例如“总结我们最近的聊天记录”应等价于 people=[]，而不是 people=["我们"]。
NO_FILTER_PERSON_WORDS = {
    "条",
    "我",
    "你",
    "他",
    "她",
    "它",
    "我们",
    "咱们",
    "我俩",
    "我们俩",
    "咱俩",
    "我们两个",
    "咱们两个",
    "双方",
    "彼此",
    "大家",
    "全员",
    "所有人",
    "全部人",
    "群里大家",
    "群内成员",
    "当前会话",
    "当前聊天",
    "这个会话",
    "这个群",
    "本群",
}


def install_chat_summary_people_filter_patch(summary_skill_module: Any) -> None:
    """规范化可选人员参数，并优先使用 QQ 号或完整昵称精确匹配。"""
    if getattr(summary_skill_module, "_people_filter_normalization_installed", False):
        return
    summary_skill_module.GENERIC_PERSON_WORDS.update(NO_FILTER_PERSON_WORDS)
    original_split = summary_skill_module._split_people

    def split_people(value: str) -> list[str]:
        result: list[str] = []
        for person in original_split(value):
            cleaned = re.sub(
                r"^(?:qq|qq号|用户qq|用户)\s*[:：]?\s*(?=\d+$)",
                "",
                person.strip(),
                flags=re.IGNORECASE,
            ).strip()
            if not cleaned or cleaned in NO_FILTER_PERSON_WORDS or cleaned in result:
                continue
            result.append(cleaned)
        return result

    def filter_messages_by_people(messages: list[Any], people: list[str]) -> list[Any]:
        # people 为空代表不启用第二参数，直接总结当前范围内所有人的消息。
        if not people:
            return list(messages)

        selected_indices: set[int] = set()
        for person in people:
            token = summary_skill_module._normalize_person(person)  # noqa: SLF001
            if not token:
                continue

            exact_indices: list[int] = []
            for index, message in enumerate(messages):
                sender_id = summary_skill_module._normalize_person(message.sender_id)  # noqa: SLF001
                sender_name = summary_skill_module._normalize_person(message.sender_name)  # noqa: SLF001
                if token.isdigit() and token == sender_id:
                    exact_indices.append(index)
                elif not token.isdigit() and token == sender_name:
                    exact_indices.append(index)

            if exact_indices:
                selected_indices.update(exact_indices)
                continue
            if token.isdigit() or len(token) < 2:
                continue

            for index, message in enumerate(messages):
                sender_name = summary_skill_module._normalize_person(message.sender_name)  # noqa: SLF001
                if len(sender_name) >= 2 and (token in sender_name or sender_name in token):
                    selected_indices.add(index)

        return [message for index, message in enumerate(messages) if index in selected_indices]

    summary_skill_module._split_people = split_people
    summary_skill_module._filter_messages_by_people = filter_messages_by_people
    summary_skill_module._people_filter_normalization_installed = True
