## 1.5.0

- Food tools report ZOE rules (plants this week, ferments, fibre, processed-food risk) instead of Wahls cups. Meal previews list medium/high-risk processed items under `processed` (was `off_plan`). Needs Health Hub 1.21.0.

## 1.4.2

- `health_weekly_review` defaults to the week just ending on Sunday, otherwise the last complete week, and now includes the best/worst night and body numbers vs the last 4 weeks. Needs Health Hub 1.18.0.

## 1.4.1

- `health_weekly_review` tells the hub it's Claude reading, so it doesn't clear the Sunday "review is ready" item on Mark's Today screen.

## 1.4.0

- `health_weekly_review` now returns the new Sunday review (habit week grid, Zone 2, smoothed weight, HRV/sleep trend, experiment, insight, next week's proposals). New `health_experiments`. Needs Health Hub 1.16.0.

## 1.3.0

- New `health_insights` (ranked, action-only insights with a weekly headline) and `health_today_plan` (habits, slipping habit and recovery action, Zone 2 pace). Needs Health Hub 1.15.0.

## 1.2.0

- New `health_training_week` tool: weekly strength sessions, Zone 2 minutes and HIIT against targets, with 8 weeks of history. Needs Health Hub 1.13.0.

## 1.1.0

- New `health_food_sleep` tool: each evening's food and drinks against the next night's Oura numbers (sleep score, HRV, lowest heart rate and when it bottomed out, HRV recovery, time to fall asleep, deep sleep). Needs Health Hub 1.12.0.
- `health_preview_log_meal` takes an optional `time_eaten` (HH:MM); meal timing feeds the "ate within 3 h of bed" comparison.
- `health_range` days now include sleep score, heart-rate low after bed, HRV recovery and time to fall asleep.

## 1.0.0

- First release: Health MCP tools on the Green, exposed to Claude through the `health_mcp` integration behind Home Assistant sign-in (admin only).
- Reads: `health_today`, `health_range`, `health_targets`, `health_weekly_review` — all served by the Health Hub add-on, which owns the data and the rules.
- Writes: `health_preview_log_drink` / `health_preview_log_meal` → `health_apply`, preview-then-apply like the Finance connector. Retries with the same preview_id and request_id land once; a drink preview is refused if that day's drinks changed.
- Per-user rate limit (60 requests a minute) and one JSON log line per request (user, MCP method/tool, status, time).
