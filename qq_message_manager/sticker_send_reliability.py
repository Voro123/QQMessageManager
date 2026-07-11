from __future__ import annotations

import base64
import shutil
import threading
from pathlib import Path
from typing import Any

from PySide6.QtGui import QImageReader

from .image_cache import MAX_IMAGE_BYTES, SUPPORTED_EXTENSIONS, ensure_cached
from .models import ChatImage

# Keep sticker payloads comfortably below the general image-read limit.  The
# resulting base64 text is roughly one third larger than the original bytes.
MAX_STICKER_SEND_BYTES = min(MAX_IMAGE_BYTES, 4 * 1024 * 1024)
_CACHE_LOCK = threading.RLock()
_CACHE_INFLIGHT: set[str] = set()
_ACTIVE_TOKEN = ""


def install_sticker_send_reliability(ui_module: Any, sticker_module: Any) -> None:
    """Send remembered image stickers from a durable local copy.

    Received QQ image URLs and file identifiers are not durable send handles.
    The library preview may still work because ``image_cache`` already contains
    a local copy, while NapCat later fails when it tries to download the stale
    original URL.  Complete mface records keep using native mface parameters;
    other records are cached locally and sent as ``base64://`` image data.
    """

    _install_window_token_bridge(ui_module)
    _install_background_sticker_cache(sticker_module)
    _install_cached_cq_resolver(sticker_module)


def _install_window_token_bridge(ui_module: Any) -> None:
    main_window_cls = ui_module.MainWindow
    if getattr(main_window_cls, "_sticker_send_token_bridge_installed", False):
        return

    original_init = main_window_cls.__init__
    original_ready = main_window_cls._handle_ai_reply_ready

    def init_with_sticker_cache(self: Any, *args: Any, **kwargs: Any) -> None:
        global _ACTIVE_TOKEN
        original_init(self, *args, **kwargs)
        _ACTIVE_TOKEN = str(getattr(self, "token", "") or "")
        memory = getattr(self, "sticker_memory", None)
        if memory is not None:
            _schedule_cache_records(memory, _ACTIVE_TOKEN)

    def ready_with_current_token(self: Any, session_id: str, reply: str) -> None:
        global _ACTIVE_TOKEN
        _ACTIVE_TOKEN = str(getattr(self, "token", "") or "")
        original_ready(self, session_id, reply)

    main_window_cls.__init__ = init_with_sticker_cache
    main_window_cls._handle_ai_reply_ready = ready_with_current_token
    main_window_cls._sticker_send_token_bridge_installed = True


def _install_background_sticker_cache(sticker_module: Any) -> None:
    memory_cls = sticker_module.StickerMemory
    if getattr(memory_cls, "_durable_sticker_cache_installed", False):
        return

    original_remember = memory_cls.remember_from_event
    original_load = memory_cls.load

    def remember_and_cache(self: Any, event: dict[str, Any]) -> int:
        before = set(getattr(self, "records", {}))
        count = original_remember(self, event)
        added_ids = set(getattr(self, "records", {})) - before
        _schedule_cache_records(self, _ACTIVE_TOKEN, discard_on_failure=added_ids)
        return count

    def load_and_cache(self: Any) -> None:
        original_load(self)
        _discard_unusable_persisted_records(self)
        _schedule_cache_records(self, _ACTIVE_TOKEN)

    memory_cls.remember_from_event = remember_and_cache
    memory_cls.load = load_and_cache
    memory_cls._durable_sticker_cache_installed = True


def _install_cached_cq_resolver(sticker_module: Any) -> None:
    record_cls = sticker_module.StickerRecord
    if getattr(record_cls, "_cached_sticker_send_installed", False):
        return

    original_to_cq = record_cls.to_cq_code

    def to_cached_cq_code(self: Any) -> str:
        if _has_complete_mface(self):
            return original_to_cq(self)

        local_path = _resolve_local_image(self, _ACTIVE_TOKEN)
        if not local_path:
            # file_id/url/path values received from QQ are not guaranteed to be
            # valid send handles.  Returning an empty value makes the existing
            # UI skip this broken record instead of asking NapCat to download a
            # stale source and producing "Bad Request".
            return ""
        try:
            data = Path(local_path).read_bytes()
        except OSError:
            return ""
        if not data or len(data) > MAX_STICKER_SEND_BYTES:
            return ""
        encoded = base64.b64encode(data).decode("ascii")
        return sticker_module._cq(  # noqa: SLF001 - runtime patch uses module encoder
            "image",
            {
                "file": "base64://" + encoded,
                "summary": str(getattr(self, "summary", "") or ""),
            },
        )

    record_cls.to_cq_code = to_cached_cq_code
    record_cls._cached_sticker_send_installed = True


def _schedule_cache_records(
    memory: Any,
    token: str,
    discard_on_failure: set[str] | None = None,
) -> None:
    discard_ids = discard_on_failure or set()
    records = list(getattr(memory, "records", {}).values())
    for record in records:
        sticker_id = str(getattr(record, "id", "") or "")
        if not sticker_id or _has_complete_mface(record) or _durable_path(record):
            continue
        with _CACHE_LOCK:
            if sticker_id in _CACHE_INFLIGHT:
                continue
            _CACHE_INFLIGHT.add(sticker_id)
        threading.Thread(
            target=_cache_record_worker,
            args=(memory, sticker_id, token, sticker_id in discard_ids),
            daemon=True,
        ).start()


def _cache_record_worker(
    memory: Any,
    sticker_id: str,
    token: str,
    discard_on_failure: bool = False,
) -> None:
    try:
        record = memory.get(sticker_id)
        if record is None or _has_complete_mface(record):
            return
        source = _resolve_local_image(record, token)
        if not source:
            if discard_on_failure:
                _discard_invalid_record(memory, sticker_id)
            return
        destination = _durable_destination(memory, record, source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_path = Path(source).resolve()
        if source_path != destination.resolve():
            shutil.copy2(source_path, destination)
        with _CACHE_LOCK:
            current = memory.get(sticker_id)
            if current is None:
                return
            current.path = str(destination)
            memory.save()
    except Exception:  # noqa: BLE001 - cache failure must not break message intake
        return
    finally:
        with _CACHE_LOCK:
            _CACHE_INFLIGHT.discard(sticker_id)


def _resolve_local_image(record: Any, token: str) -> str:
    durable = _durable_path(record)
    if durable:
        return durable

    image = ChatImage(
        url=str(getattr(record, "url", "") or ""),
        path=str(getattr(record, "path", "") or ""),
        file=str(getattr(record, "file", "") or ""),
        file_id=str(getattr(record, "file_id", "") or ""),
        file_unique=str(getattr(record, "file_unique", "") or ""),
    )
    local_path = str(ensure_cached(image, token=token, max_bytes=MAX_STICKER_SEND_BYTES) or "")
    return local_path if _is_readable_image(local_path) else ""


def _durable_path(record: Any) -> str:
    raw = str(getattr(record, "path", "") or "").strip()
    if not raw:
        return ""
    path = Path(raw).expanduser()
    try:
        valid = (
            path.is_file()
            and 0 < path.stat().st_size <= MAX_STICKER_SEND_BYTES
            and _is_readable_image(str(path))
        )
    except OSError:
        return ""
    return str(path) if valid else ""


def _durable_destination(memory: Any, record: Any, source: str) -> Path:
    memory_path = Path(getattr(memory, "path", Path.home() / ".qq_message_manager" / "sticker_memory.json"))
    cache_dir = memory_path.parent / "sticker_cache"
    suffix = Path(source).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        suffix = ".jpg"
    safe_id = "".join(char for char in str(getattr(record, "id", "") or "sticker") if char.isalnum() or char in "_-.")
    return cache_dir / f"{safe_id[:100]}{suffix}"


def _has_complete_mface(record: Any) -> bool:
    return bool(
        str(getattr(record, "source_type", "") or "") == "mface"
        and str(getattr(record, "emoji_id", "") or "")
        and str(getattr(record, "emoji_package_id", "") or "")
        and str(getattr(record, "key", "") or "")
    )


def _is_readable_image(path: str) -> bool:
    if not path:
        return False
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    return not reader.read().isNull()


def _discard_invalid_record(memory: Any, sticker_id: str) -> None:
    """Undo a newly remembered record when no real image can be obtained."""
    try:
        with _CACHE_LOCK:
            if getattr(memory, "is_locked", lambda _id: False)(sticker_id):
                return
            delete_record = getattr(memory, "delete_record", None)
            if callable(delete_record):
                delete_record(sticker_id)
                return
            records = getattr(memory, "records", {})
            if records.pop(sticker_id, None) is not None:
                memory.save()
    except Exception:  # noqa: BLE001 - validation must not break message intake
        return


def _discard_unusable_persisted_records(memory: Any) -> int:
    """Drop legacy blank entries immediately instead of waiting for URL timeouts.

    New image stickers are copied to the durable sticker cache after they are
    received.  Therefore an unlocked record that is still missing a decodable
    local image on a later load is an incomplete/failed record, not a usable
    library item.  Locked records retain their explicit no-eviction guarantee.
    """
    removed = 0
    try:
        with _CACHE_LOCK:
            records = getattr(memory, "records", {})
            invalid_ids = [
                sticker_id
                for sticker_id, record in list(records.items())
                if not getattr(memory, "is_locked", lambda _id: False)(sticker_id)
                and not _durable_path(record)
            ]
            for sticker_id in invalid_ids:
                if records.pop(sticker_id, None) is not None:
                    removed += 1
            if removed:
                memory.save()
    except Exception:  # noqa: BLE001 - bad legacy data must not block startup
        return 0
    return removed
