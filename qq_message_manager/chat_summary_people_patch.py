from __future__ import annotations

import re
from typing import Any


def install_chat_summary_people_filter_patch(summary_skill_module: Any) -> None:
    """规范化人员过滤参数中的 QQ 前缀，并丢弃误识别的数量单位。"""
    if getattr(summary_skill_module, "_people_filter_normalization_installed", False):
        return
    summary_skill_module.GENERIC_PERSON_WORDS.add("条")
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
            if not cleaned or cleaned == "条" or cleaned in result:
                continue
            result.append(cleaned)
        return result

    summary_skill_module._split_people = split_people
    summary_skill_module._people_filter_normalization_installed = True
