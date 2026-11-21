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

### 3. Hidden foods
MacroFactor's recent-foods list can keep reporting a food as eaten today after it was deleted or swapped
in the app ("Salmon (Aoraki)", 2026-11-19). Rows in `food_log_hidden (date, lower-cased name)` are deleted
from `food_log` after every `/foods-seen` sync, so they stay out of `get_food_log` and the Hub feed.
Migration `migrations/0004_food_log_hidden.sql`. Add one with
`npx wrangler d1 execute macrofactor --remote --command "INSERT INTO food_log_hidden VALUES ('YYYY-MM-DD','food name')"`.

### 4. Staples, food memory, read-back, missed-log alerts, feedback (2026-11-21)
All in the same Worker and D1 database (`src/extras.ts`, migrations `0005_staples_history_body.sql`,
`0006_seed_staples.sql`).

- **Staples** (`staples` table): name, aliases, brand, basis (`per_100g` or `per_unit` + unit label/grams),
  full nutrients, source (`label` / `usda` / `estimate`), verified, suspect, notes, updated. Tools
  `list_staples`, `upsert_staple` (Claude saves a label Mark reads out; `source:"label"` marks it verified),
  `log_staple(name, amount, unit)` — computes the TOTAL and queues ONE entry with serving `one`, the weight in
  the name and the math in notes. Unverified staples are logged with a `verify_prompt`; suspect ones or ones
  without figures are refused (`needs_label`). Seeded unverified 2026-11-21: Eggs, Regal cold smoked salmon
  (suspect: 9.6 g protein / 14 g fat), Thoroughbread olive & rosemary and Sirena tuna (no figures yet),
  Hass avocado, Kūmara, Chicken thigh (USDA per 100 g), Rose's Chilli Crisp, AG1 (MacroFactor saved entries).
- **Food memory** (`food_history`): every food the phone confirms (`/sync-ack`) is copied with name, brand,
  serving, nutrients, date; each MF Nightly recent-foods day is mirrored too. Never pruned.
- **Fuzzy search**: `search_my_foods` and `search_food` search staples → history → saved → USDA/OFF, matching
  case, accents (kūmara), plurals, typos and prefixes ("thoroughbred" → Thoroughbread); every hit has `score`.
- **Read-back**: MacroFactor's Shortcuts actions cannot read individual food-log entries (its official spec,
  github.com/MacroFactor/apple-shortcuts, exposes only Find Recent Food and the Today Summary). So
  `get_food_log` labels each row's `entry_source`: `export`, `recent_foods` (one row per food per day), or
  `connector` (dispatched by Claude and confirmed landed — exact). MF Nightly sends Apple Health's latest weight
  and waist to `POST /body?token=<INGEST_SECRET>&weight=&weight_unit=&weight_at=&waist=&waist_unit=&waist_at=`;
  weight fills `days.scale_weight` (never over an export row), `get_weight_history` returns it
  (`source:"apple_health"`) plus `waist`. MacroFactor does not write waist to Apple Health, so waist comes from the
  `record_waist` tool (Claude saves it when Mark gives a measurement).
- **Missed-log alerts**: crons `30 7,8 * * *` and `40 10,11 * * *` (UTC) — the handler only acts when it is
  20:30 or 23:40 in `USER_TZ`, so DST needs no change. Anything queued or claimed but unconfirmed for 30+ min
  re-sends the "MacroFactor" Pushcut notification with a title/text listing the items (Pushcut may show its
  saved text instead on the free plan). `GET /missed-check?token=<INGEST_SECRET>[&min_age_min=][&dry_run=1]`
  runs it on demand.
- **Feedback**: every logging tool returns `day` (eaten incl. this entry, left vs targets, protein pace,
  12-hour eating window, notes); the server instructions ask Claude for one or two lines after each log.

Tests: `npm test` (39) — `tests/extras.test.ts` runs real SQL against `schema.sql` via Node's built-in SQLite.

#### iPhone: add weight to MF Nightly
At the end of **MF Nightly**:
1. **Find Health Samples** → Type **Weight** (Body Mass) → Sort by **Start Date**, **Latest First** → Limit **1**.
2. **Text**: `https://macrofactor-mcp.autumn-term-c180.workers.dev/body?token=<INGEST_SECRET>&weight=` then
   insert step 1's **Health Samples** → property **Value**; type `&weight_unit=` → step 1 → **Unit**;
   `&weight_at=` → step 1 → **Start Date** (Date Format **ISO 8601**, include time).
3. **Get Contents of URL** ← that Text, Method **POST**.
`refresh_from_phone` runs the same shortcut, so it refreshes weight too.

### 5. ZOE rules in the feedback (2026-11-22)
Health Hub (1.25.0+) scores the ZOE rules and, after each 10-minute sync, posts today's `zoe_day` result to
`POST /zoe-status` (header `x-hub-token`; the Hub token can read the feed and write only this). Stored in
`zoe_status` (migration `0007_zoe_status.sql`). Every logging tool's `day.zoe` carries plants this week vs pace,
fermented portions, fibre, processing score, risky items and nudges; foods logged after `as_of` aren't counted yet.

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
