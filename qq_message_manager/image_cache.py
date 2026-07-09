from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

from .models import ChatImage

IMAGE_CACHE_DIR = Path(tempfile.gettempdir()) / "QQMessageManager" / "image_cache"
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 10


def get_image_cache_dir() -> Path:
    """图片缓存目录（位于系统临时目录下，不会随仓库提交）。"""
    IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return IMAGE_CACHE_DIR


def supported_format(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in SUPPORTED_EXTENSIONS


def short_id(img: ChatImage) -> str:
    """用于日志的短标识，避免打印完整私有 URL。"""
    return (img.file_unique or img.file_id or img.url or img.path or "?")[:8]


def _infer_extension(img: ChatImage) -> str:
    mime_map = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }
    lowered = (img.mime_type or "").lower()
    if lowered in mime_map:
        return mime_map[lowered]
    for source in (img.url, img.path):
        if source:
            ext = os.path.splitext(source.split("?")[0])[1].lower()
            if ext in SUPPORTED_EXTENSIONS:
                return ext
    return ".jpg"


def _cache_filename(img: ChatImage) -> str:
    stable = img.file_unique or img.file_id
    if stable:
        base = stable
    else:
        source = img.url or img.path or img.file or "unknown"
        base = hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]
    return f"{base}{_infer_extension(img)}"


def ensure_cached(
    img: ChatImage,
    token: str = "",
    timeout_seconds: int = DOWNLOAD_TIMEOUT_SECONDS,
    max_bytes: int = MAX_IMAGE_BYTES,
) -> str | None:
    """确保图片已落到本地缓存，返回本地路径；失败返回 None。

    来源优先级：path（本机可读）> url（http/https）> file（base64://）。
    文件名基于 file_unique/file_id/来源 hash，避免重复下载。
    """
    cache_dir = get_image_cache_dir()
    dest = cache_dir / _cache_filename(img)
    if dest.exists() and 0 < dest.stat().st_size <= max_bytes:
        img.local_path = str(dest)
        return str(dest)

    if img.file.startswith("base64://"):
        try:
            data = base64.b64decode(img.file[len("base64://"):].strip())
        except Exception:  # noqa: BLE001
            return None
        if len(data) > max_bytes:
            return None
        dest.write_bytes(data)
        img.local_path = str(dest)
        return str(dest)

    if img.path and os.path.exists(img.path):
        try:
            data = Path(img.path).read_bytes()
        except Exception:  # noqa: BLE001
            return None
        if 0 < len(data) <= max_bytes and supported_format(img.path):
            dest.write_bytes(data)
            img.local_path = str(dest)
            return str(dest)
        return None

    if img.url.startswith("http://") or img.url.startswith("https://"):
        return _download_to_cache(img.url, dest, token=token, timeout_seconds=timeout_seconds, max_bytes=max_bytes)

    return None


def _download_to_cache(
    url: str,
    dest: Path,
    token: str,
    timeout_seconds: int,
    max_bytes: int,
) -> str | None:
    headers = {"User-Agent": "QQMessageManager/1.0", "Accept": "image/*"}
    if token and "token=" not in url and "access_token=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}token={token}"
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            data = response.read(max_bytes + 1)
    except Exception:  # noqa: BLE001
        return None
    if len(data) > max_bytes:
        return None
    dest.write_bytes(data)
    return str(dest)


def to_data_uri(local_path: str) -> str | None:
    """把本地缓存图片读取为 base64 data URI（仅在请求前临时读取）。不支持的格式返回 None。"""
    if not local_path or not os.path.exists(local_path):
        return None
    if not supported_format(local_path):
        return None
    try:
        data = Path(local_path).read_bytes()
    except Exception:  # noqa: BLE001
        return None
    return f"data:{_guess_mime(local_path)};base64,{base64.b64encode(data).decode('ascii')}"


def _guess_mime(path: str) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(os.path.splitext(path)[1].lower(), "image/png")
