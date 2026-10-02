# Finance Connector

Runs the Finance MCP tools (overview, fortnight, transactions, spending, records,
refresh, guarded edits, planning files) on the Home Assistant Green, so Claude can use
the Finance app from the cloud and from any device. Nothing runs on the Mac.

## How it is protected

- The add-on publishes **no ports**. It talks to the Finance add-on and to Home
  Assistant over the Supervisor's private network.
- Claude reaches it only through the bundled `finance_mcp` integration at
  `https://<your-id>.ui.nabu.casa/api/finance_mcp`, which requires a Home Assistant
  sign-in (OAuth) and an **admin** user.
- The integration forwards requests with a random shared secret generated on first
  start (`/data/proxy_secret`); the add-on refuses anything without it.
- No Home Assistant token or bank credential is stored by this add-on. Refresh uses
  the Finance app's own sync in a headless browser, exactly as before.

## Setup

1. Install and start **Finance Connector**. The log should say
   `Finance app reachable` and `integration files installed`.
2. Restart Home Assistant (Settings → System → Restart).
3. Settings → Devices & services → Add integration → **Finance connector (MCP)** → Submit.
4. In Claude: Settings → Connectors → Add custom connector
   - URL: `https://<your-id>.ui.nabu.casa/api/finance_mcp`
   - Advanced → OAuth Client ID: `https://claude.ai` (secret blank)
   - Connect, and sign in to Home Assistant with an admin account.

## Options

- `finance_url`: the Finance add-on on the internal network. Default
  `http://44616b96-financial-planner:8765`.

## Planning files

`finance_planning_files` / `finance_save_planning_file` read and write JSON/Markdown planning
artifacts (e.g. `planned-payments.json`) in `/share/finance_connector/planning`.
