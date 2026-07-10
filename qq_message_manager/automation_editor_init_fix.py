from __future__ import annotations

from typing import Any


def install_automation_editor_init_fix(automation_module: Any) -> None:
    """Guard runtime-patched editor controls during the base dialog constructor.

    ``AutomationTaskEditDialog.__init__`` calls ``self._sync_controls()`` before
    the target-selector patch has created ``target_manual_input``.  Because the
    class method is already patched at that point, the extended sync method must
    tolerate this partially constructed state.
    """

    dialog_cls = automation_module.AutomationTaskEditDialog
    if getattr(dialog_cls, "_automation_editor_init_fix_installed", False):
        return

    patched_sync = dialog_cls._sync_controls

    def sync_controls_safe(self: Any) -> None:
        try:
            patched_sync(self)
        except AttributeError as exc:
            # During the original constructor the extended target widgets do
            # not exist yet.  The wrapped method has already completed the base
            # control synchronization, so it is safe to defer only the target
            # selector portion until the explicit sync at the end of the patch
            # constructor.
            missing = getattr(exc, "name", "") or ""
            if missing not in {
                "target_manual_input",
                "target_refresh_button",
                "_automation_target_form",
                "_automation_schedule_form",
            }:
                raise

    dialog_cls._sync_controls = sync_controls_safe
    dialog_cls._automation_editor_init_fix_installed = True
