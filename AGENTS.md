# AGENTS.md

## Project rules for coding agents

- Any user-facing configuration, connection setting, AI provider setting, API key, prompt, automation rule, per-session state, pinned state, or similar preference must be persisted with `QSettings` so it is restored on the next launch.
- Do not commit real API keys, QQ tokens, NapCat tokens, user IDs, or generated local cache files to the repository.
- Keep NapCat connection defaults pointed at the OneBot forward WebSocket server, not the NapCat WebUI/debug endpoint.
- Prefer small, focused modules for non-UI logic. UI code should orchestrate widgets and signals; API/provider-specific logic should live outside `ui.py` when practical.
- New AI-provider integrations must be optional at runtime. Missing or failing AI settings should log a readable error and must not break normal manual messaging or real-time message receiving.
- When adding session-specific behavior such as AI delegation or pinning, keep it isolated per session and avoid affecting unselected/unmanaged sessions.
