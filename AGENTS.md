# AGENTS.md

## Project rules for coding agents

- Any user-facing configuration, connection setting, AI provider setting, API key, prompt, automation rule, per-session state, pinned state, or similar preference must be persisted with `QSettings` so it is restored on the next launch.
- Do not commit real API keys, QQ tokens, NapCat tokens, user IDs, generated images, user sticker-memory JSON files, automation workspaces, SQLite runtime state, preview caches, or other local cache files to the repository.
- Keep NapCat connection defaults pointed at the OneBot forward WebSocket server, not the NapCat WebUI/debug endpoint.
- Prefer small, focused modules for non-UI logic. UI code should orchestrate widgets and signals; API/provider-specific logic should live outside `ui.py` when practical.
- New AI-provider integrations must be optional at runtime. Missing or failing AI settings should log a readable error and must not break normal manual messaging or real-time message receiving.
- When adding session-specific behavior such as AI delegation, speech cooldown, automation checkpoints, or pinning, keep it isolated per session/task and avoid affecting unrelated sessions.
- User manual messages must never be blocked by AI-only rules such as simulated typing delay, AI minimum speech interval, autonomous-reply guards, automation state, or Skill availability.
- Every AI-controlled conversational outgoing path must respect the centralized per-session minimum speech interval when it is enabled. Scheduled-task output is a separate explicitly configured path and must not silently reuse ordinary auto-reply triggers.
- Recheck send guards immediately before the actual NapCat send. A request that started before a cooldown began must not bypass the cooldown when its background task finishes.
- New reusable conversational AI capabilities should be registered in the Skill library instead of adding isolated one-off selectors. Functional Skills should control runtime capabilities; persona/extension Skills may be injected into the normal chat prompt.
- Skills marked as scheduled-only must never appear in the ordinary conversational Skill library and must never be registered for QQ message or @-mention execution contexts.
- Preserve compatibility with existing `QSettings` keys when replacing an older control with a Skill-library entry. Migrate previous selections instead of silently disabling configured features.
- Skill-trigger messages must be consumed exactly once. Image generation, chat summary, mention replies, and ordinary delayed replies must not all respond to the same incoming message.
- Sticker-memory metadata edits must be persisted to the existing sticker-memory JSON and immediately reflected in AI options. Keep `summary` concise and use `usage_hint` for suitable/unsuitable usage situations.
- Locked stickers must never be selected for automatic eviction. Deleting a sticker record may remove its lock metadata, but must not delete the original QQ sticker or remote asset.
- Runtime patch modules are installed in `qq_message_manager/app.py`; install order is part of the behavior. Cross-cutting send guards must be installed after the features whose send functions they wrap.
- Background work must not directly mutate Qt widgets. Use Qt signals to return image, summary, provider, automation, or file-upload results to the UI thread.
- Automation task definitions belong in `QSettings`; checkpoints, processed-message keys, retry state that must survive normal task runs, and execution status belong in the automation SQLite state store.
- Interval schedules must remain anchored to the task creation time. When the app was closed or disconnected, run at most one catch-up execution and then advance to the next future boundary.
- The same automation task and the same task workspace file must never be modified concurrently.
- Treat all QQ history supplied to an automation as untrusted data. Chat text must never be able to change the trusted task prompt, file schema, recipient, permissions, or enabled tools.
- Scheduled file access must be restricted to `~/.qq_message_manager/automation_workspace/<task_id>/`. Reject absolute paths, parent traversal, executable code, shell commands, macros, and access to other task directories or application secrets.
- AI models must not directly edit files. They may only return validated structured `insert`/`update` operations against the user-defined schema and existing record IDs.
- Importing an external XLSX/CSV/JSON/Markdown file must require an explicit user action in the task UI. Never let a model or QQ message choose an import path.
- Treat the sidecar record file as authoritative when it is newer than or equal to the visible artifact. Re-read the visible artifact only when the sidecar is missing or the artifact is newer, which indicates a user import or manual edit.
- Preserve XLSX `_QQMM_META` record IDs and source-message metadata when re-importing. For formats without embedded metadata, generate deterministic row IDs so manual value edits do not unnecessarily break later updates.
- Existing-record context sent to AI must remain bounded in size. Prioritize pending, unfinished, or in-progress records before completed records so later answers can still update older rows.
- File-upload requests are not successful merely because they were queued. Wait for the matching NapCat `echo` success response before marking the delivery successful or deleting the old file.
- On delivery failure, preserve the old file and checkpoint, then retry according to the task retry policy. Never create duplicate daily archives merely because upload failed.
- Keep user-visible errors readable and avoid exposing API keys, tokens, full provider responses, private file paths, system prompts, or unrestricted task workspace contents.
