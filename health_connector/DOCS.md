# Health Connector

Runs the Health MCP tools on the Home Assistant Green, so Claude can read the
Health Hub and log a drink or a meal from any device with the Mac asleep.

## Tools

| Tool | What it does |
|---|---|
| `health_today` | Today's food, Wahls cups by group, drinks and rule flags, 30-day reset |
| `health_range` | Day-by-day summary for a date range (max 92 days) with averages |
| `health_targets` | Food targets, Wahls rules, alcohol rules, reset status, weight goal |
| `health_weekly_review` | The Sunday review: rules kept/broken, drinks, veg cups, weight, sleep/HRV after drinking vs dry nights |
| `health_preview_log_drink` → `health_apply` | Log a drink (preview shows the day it lands on and any rules it breaks) |
| `health_preview_log_meal` → `health_apply` | Log a meal (preview shows the parsed items, macros, cups and off-plan flags) |

## How it is protected

- The add-on publishes **no ports**. It talks to the Health Hub over the
  Supervisor's private network; it holds no Home Assistant token and no API keys
  (food parsing runs in the Health Hub with its own key).
- Claude reaches it only through the bundled `health_mcp` integration at
  `https://<your-id>.ui.nabu.casa/api/health_mcp`, which requires a Home Assistant
  sign-in (OAuth) and an **admin** user.
- The integration forwards requests with a random shared secret generated on first
  start (`/data/proxy_secret`); the add-on refuses anything without it.
- Each user gets 60 requests a minute. Every request is logged in this add-on's log
  as one JSON line: user, MCP method/tool, status, time.

## Setup

1. Update **Health Hub** to 1.11.0 or later.
2. Install and start **Health Connector**. The log should say `Health Hub reachable`
   and `integration files installed`.
3. Restart Home Assistant (Settings → System → Restart).
4. Settings → Devices & services → Add integration → **Health connector (MCP)** → Submit.
5. In Claude: Settings → Connectors → Add custom connector
   - Name: `Health Hub (cloud)`
   - URL: `https://<your-id>.ui.nabu.casa/api/health_mcp`
   - Advanced → OAuth Client ID: `https://claude.ai` (secret blank)
   - Connect, and sign in to Home Assistant with an admin account.

## Options

- `health_url`: the Health Hub add-on on the internal network. Default
  `http://44616b96-health-hub:8768` (the same repository prefix as the Finance add-on).
  If the log says it cannot reach the hub, copy the Health Hub's hostname from
  Settings → Add-ons → Health Hub (the "Hostname" shown on its Info tab).
