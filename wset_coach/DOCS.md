# WSET Coach Connector

Runs the WSET Study Coach MCP tools (coaching contract, progress, weak areas,
study plan, tasting history, session logs, textbook search, preview → apply
writes) on the Home Assistant Green, so Claude can coach from any device with
the Mac asleep.

## How it is protected

- The add-on publishes **no ports**. Home Assistant reaches it over the
  Supervisor's private network.
- Claude reaches it only through the bundled `wset_mcp` integration at
  `https://<your-id>.ui.nabu.casa/api/wset_mcp`, which requires a Home Assistant
  sign-in (OAuth) and an **admin** user.
- The Mac syncs the workspace at `https://<your-id>.ui.nabu.casa/api/wset_mcp/git/repo`
  with the existing long-lived admin token. Nothing else is exposed.
- The integration forwards requests with a random shared secret generated on first
  start (`/data/proxy_secret`); the add-on refuses anything without it.
- Each user gets 60 requests a minute per endpoint. Every request is logged in this
  add-on's log as one JSON line: user, MCP method/tool or git path, status, time.

## The workspace

`/share/wset_coach/repo` is a git repository holding `AGENTS.md` (served read-only as
the coaching contract), `state/`, `quizzes/`, `tastings/` and
`study/textbook_fulltext.txt`. Every applied write is one commit by
"WSET Study Coach". A preview is rejected at apply time if its file changed since,
including by a push from the Mac. Pushes must fast-forward; a Mac copy that is behind
must pull first, and a conflict stops on the Mac for review. History is never
rewritten by the add-on.

## Setup

1. Install and start **WSET Coach Connector**. The log should say `integration files installed`.
2. Restart Home Assistant.
3. Settings → Devices & services → Add integration → **WSET coach connector (MCP)** → Submit.
4. Push the first copy of the workspace from the Mac (`~/bin/wset-coach-sync init`).
5. In Claude: Settings → Connectors → Add custom connector
   - Name: `WSET Study Coach (cloud)`
   - URL: `https://<your-id>.ui.nabu.casa/api/wset_mcp`
   - Advanced → OAuth Client ID: `https://claude.ai` (secret blank)
   - Connect, and sign in to Home Assistant with an admin account.
