from __future__ import annotations

import json
from typing import Any

from .folder_access_models import FolderGrant
from .folder_access_service import canonical_folder_root

FOLDER_ACCESS_GRANTS_KEY = "ai/folder_access_grants"


class FolderGrantStore:
    def __init__(self, settings: Any, key: str = FOLDER_ACCESS_GRANTS_KEY) -> None:
        self.settings = settings
        self.key = key

    def load(self) -> list[FolderGrant]:
        try:
            raw = json.loads(str(self.settings.value(self.key, "[]") or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(raw, list):
            return []
        grants: list[FolderGrant] = []
        aliases: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            grant = FolderGrant.from_dict(item)
            if not _valid_alias(grant.alias) or grant.alias.casefold() in aliases:
                continue
            canonical_root = canonical_folder_root(grant.root_path)
            if not canonical_root:
                continue
            grant.root_path = canonical_root
            aliases.add(grant.alias.casefold())
            grants.append(grant)
        return grants

    def save(self, grants: list[FolderGrant]) -> None:
        normalized = self.validate(grants)
        self.settings.setValue(
            self.key,
            json.dumps([grant.to_dict() for grant in normalized], ensure_ascii=False),
        )
        self.settings.sync()

    def validate(self, grants: list[FolderGrant]) -> list[FolderGrant]:
        result: list[FolderGrant] = []
        aliases: set[str] = set()
        ids: set[str] = set()
        for source in grants:
            grant = source.normalized()
            if not _valid_alias(grant.alias):
                raise ValueError("文件夹关联名不能为空，且不能包含路径符号或换行")
            folded = grant.alias.casefold()
            if folded in aliases:
                raise ValueError(f"文件夹关联名重复：{grant.alias}")
            if grant.grant_id in ids:
                raise ValueError("文件夹授权 ID 重复")
            canonical_root = canonical_folder_root(grant.root_path)
            if not canonical_root:
                raise ValueError(f"授权文件夹不存在或不是目录：{grant.alias}")
            grant.root_path = canonical_root
            aliases.add(folded)
            ids.add(grant.grant_id)
            result.append(grant)
        return result

    def find_alias(self, alias: str) -> FolderGrant | None:
        folded = str(alias or "").strip().casefold()
        return next((grant for grant in self.load() if grant.alias.casefold() == folded), None)


def _valid_alias(alias: str) -> bool:
    value = str(alias or "").strip()
    return bool(value and len(value) <= 80 and not any(char in value for char in "\\/:\x00\r\n"))
