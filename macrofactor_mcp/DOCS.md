# MacroFactor Connector (cloud)

Lets Claude search food macros, read MacroFactor data and queue food logs into MacroFactor.
Unlike the Finance and WSET connectors it does **not** run on the HA Green: it is
[jamesj64/macrofactor-mcp](https://github.com/jamesj64/macrofactor-mcp) deployed unchanged to
Cloudflare Workers, so it runs with the Mac and the Green both off.

- Upstream commit deployed: `7f6d19ecbe0e634ba920593ff8c548e9ba30ff87`
- Worker: `macrofactor-mcp` → `https://macrofactor-mcp.autumn-term-c180.workers.dev`
- D1 database: `macrofactor` (`b8854de3-7d76-430f-a544-b2c3dd52c62d`)
- `wrangler.jsonc` here = upstream's with `database_id`, `USER_TZ` (`Pacific/Auckland`) and `keep_vars: true` changed.
- `PUSHCUT_WEBHOOK_URL` is a plain-text var set in the Cloudflare dashboard (by choice); `keep_vars` stops `wrangler deploy` removing it.

## Local patch: `local.patch`

### 1. Weighed foods sent as totals

MacroFactor's *Log by JSON* multiplies the nutrients of a measured serving (`{amount, unit}`)
by `amount`: a 72 kcal egg sent as `{amount: 50, unit: "grams"}` logged as 3,600 kcal
(confirmed 2026-11-19). Upstream's `get_food_nutrients` returns exactly that shape, so the
patch rewrites measured servings to `"one"` (nutrients are always the total eaten) and puts
the amount in the name.

### 2. `/hub-foods` read-only feed for the Health Hub
`GET /hub-foods?date=YYYY-MM-DD` with header `x-hub-token: <HUB_READ_TOKEN>` returns the day's foods:
those logged by hand in MacroFactor (MF Nightly's recent-foods rows, `source: macrofactor`) and those
Claude logged that landed (`source: claude`, macros from the stored payload, time = when it landed).
A recent-foods row with the same name as a Claude food that day is dropped; repeated names get a
counter ("Egg (2)"). It reads only; `INGEST_SECRET` is not accepted. Health Hub 1.24.0 polls it every
30 min (`macrofactor_token` option). The dispatch log keeps each payload for this — database
migration `migrations/0003_dispatch_payload.sql` (applied 2026-11-19; fresh installs get it from `schema.sql`).
Limitation: a Claude food deleted in the MacroFactor app still shows in the Hub; delete it there too.

Apply after checkout: `git apply ../health-hub/macrofactor_mcp/local.patch`.

## How it is protected

- MCP is served only at `/mcp/<MCP_TOKEN>`; bare `/mcp` returns 401. The connector URL is the
  credential (Claude → Authentication: none). There is no Home Assistant / OAuth sign-in.
- Phone endpoints (`/pending-all`, `/sync-ack`, `/today`, `/foods-seen`, `/upload-export`) need `INGEST_SECRET`.
- Writes never reach MacroFactor without a tap on the iPhone: the server only queues; the
  "MF Sync" Shortcut runs MacroFactor's official *Log by JSON* action.
- Secrets live only as Worker secrets (never in this repo): `INGEST_SECRET`, `MCP_TOKEN`,
  `PUSHCUT_WEBHOOK_URL`, `HUB_READ_TOKEN` (Health Hub's read-only feed), optional `PUSHCUT_REFRESH_WEBHOOK_URL`, optional `USDA_API_KEY`.

## Redeploy / update

```bash
git clone https://github.com/jamesj64/macrofactor-mcp && cd macrofactor-mcp
git checkout <commit>            # review the upstream diff first
cp ../health-hub/macrofactor_mcp/wrangler.jsonc .
git apply ../health-hub/macrofactor_mcp/local.patch
npm ci && npx tsc --noEmit && npx vitest run
npx wrangler deploy              # existing secrets are kept
```
Schema changes upstream ship as `migrations/*.sql`; apply with
`npx wrangler d1 execute macrofactor --file=./migrations/<file> --remote`.

Rotate a secret: `npx wrangler secret put MCP_TOKEN` (then update the Claude connector URL) or
`INGEST_SECRET` (then update the URLs/headers in the MF Sync / MF Nightly / Update MF Shortcuts).

## Phone setup

See upstream `ios-setup.md`: **MF Sync** (writes, run by the Pushcut "MacroFactor" notification),
**MF Nightly** (reads, 11:50 pm + MacroFactor-closed automations), optional **Update MF** (export upload).
