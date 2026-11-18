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

## How it is protected

- MCP is served only at `/mcp/<MCP_TOKEN>`; bare `/mcp` returns 401. The connector URL is the
  credential (Claude → Authentication: none). There is no Home Assistant / OAuth sign-in.
- Phone endpoints (`/pending-all`, `/sync-ack`, `/today`, `/foods-seen`, `/upload-export`) need `INGEST_SECRET`.
- Writes never reach MacroFactor without a tap on the iPhone: the server only queues; the
  "MF Sync" Shortcut runs MacroFactor's official *Log by JSON* action.
- Secrets live only as Worker secrets (never in this repo): `INGEST_SECRET`, `MCP_TOKEN`,
  `PUSHCUT_WEBHOOK_URL`, optional `PUSHCUT_REFRESH_WEBHOOK_URL`, optional `USDA_API_KEY`.

## Redeploy / update

```bash
git clone https://github.com/jamesj64/macrofactor-mcp && cd macrofactor-mcp
git checkout <commit>            # review the upstream diff first
cp ../health-hub/macrofactor_mcp/wrangler.jsonc .
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
