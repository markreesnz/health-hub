## 1.0.0

- First release: Health MCP tools on the Green, exposed to Claude through the `health_mcp` integration behind Home Assistant sign-in (admin only).
- Reads: `health_today`, `health_range`, `health_targets`, `health_weekly_review` — all served by the Health Hub add-on, which owns the data and the rules.
- Writes: `health_preview_log_drink` / `health_preview_log_meal` → `health_apply`, preview-then-apply like the Finance connector. Retries with the same preview_id and request_id land once; a drink preview is refused if that day's drinks changed.
- Per-user rate limit (60 requests a minute) and one JSON log line per request (user, MCP method/tool, status, time).
