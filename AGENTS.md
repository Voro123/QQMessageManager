# AGENTS.md

## Project rules for coding agents

- Any user-facing configuration, connection setting, AI provider setting, API key, prompt, automation rule, per-session state, pinned state, or similar preference must be persisted with `QSettings` so it is restored on the next launch.
- Do not commit real API keys, QQ tokens, NapCat tokens, user IDs, generated images, user sticker-memory JSON files, preview caches, or other local cache files to the repository.
- Keep NapCat connection defaults pointed at the OneBot forward WebSocket server, not the NapCat WebUI/debug endpoint.
- Prefer small, focused modules for non-UI logic. UI code should orchestrate widgets and signals; API/provider-specific logic should live outside `ui.py` when practical.
- New AI-provider integrations must be optional at runtime. Missing or failing AI settings should log a readable error and must not break normal manual messaging or real-time message receiving.
- When adding session-specific behavior such as AI delegation, speech cooldown, or pinning, keep it isolated per session and avoid affecting unselected/unmanaged sessions.
- User manual messages must never be blocked by AI-only rules such as simulated typing delay, AI minimum speech interval, autonomous-reply guards, or Skill availability.
- Every AI-controlled outgoing path must respect the centralized per-session minimum speech interval when it is enabled. This includes normal text replies, sticker replies, image-generation results, chat-summary output, and AI-generated error/status replies.
- Recheck send guards immediately before the actual NapCat send. A request that started before a cooldown began must not bypass the cooldown when its background task finishes.
- New reusable AI capabilities should be registered in the Skill library instead of adding isolated one-off selectors. Functional Skills should control runtime capabilities; persona/extension Skills may be injected into the normal chat prompt.
- Preserve compatibility with existing `QSettings` keys when replacing an older control with a Skill-library entry. Migrate previous selections instead of silently disabling configured features.
- Skill-trigger messages must be consumed exactly once. Image generation, chat summary, mention replies, and ordinary delayed replies must not all respond to the same incoming message.
- Sticker-memory metadata edits must be persisted to the existing sticker-memory JSON and immediately reflected in AI options. Keep `summary` concise and use `usage_hint` for suitable/unsuitable usage situations.
- Locked stickers must never be selected for automatic eviction. Deleting a sticker record may remove its lock metadata, but must not delete the original QQ sticker or remote asset.
- Runtime patch modules are installed in `qq_message_manager/app.py`; install order is part of the behavior. Cross-cutting send guards must be installed after the features whose send functions they wrap.
- Background work must not directly mutate Qt widgets. Use Qt signals to return image, summary, or provider results to the UI thread.
- Keep user-visible errors readable and avoid exposing API keys, tokens, full provider responses, private file paths, or system prompts.
