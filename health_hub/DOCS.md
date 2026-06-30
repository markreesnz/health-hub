# 🩺 Health app

Evening check-in + AI coaching, fed by **Apple Health** and the **Oura ring**, scheduled
against the MTI "Living Program". Single-file browser app (`index.html`) backed by an
always-on hub (`server.py`) that lives on the **HA Green**.

```
iPhone: Health Auto Export ──POST /ingest──▶  HA Green (server.py)  ◀──fetch── Browser app (index.html)
   (Oura sleep/HRV/RHR + Apple steps/etc)         │  computes today's workout        │
                                                   │  stores history + AI summary     └─▶ api.anthropic.com (coaching)
Morning cron: server.py notify ──▶ Home Assistant ──push──▶ iPhone (today's workout)
```

## Components
| File | Role |
|---|---|
| `index.html` | The app: evening check-in, live metrics + workout panels, AI coaching, history. |
| `server.py` | Stdlib hub: serves the app, ingests Health Auto Export, computes workouts, stores state, pushes notifications. |
| `config.json` | Port, block-schedule, Home Assistant notify credentials. |
| `data/` | `state.json` (entries + rolling summary), `metrics.json` (ingested health data). |
| `backups/` | Daily localStorage snapshots POSTed by the app on load. |

## Run
```bash
cd ~/health
python3 server.py            # http://localhost:8768  (serves app + API)
python3 server.py notify     # send today's workout to the iPhone (for cron)
python3 server.py workout    # print today's workout (debug)
```
Open **http://localhost:8768/** (or the HA Green host). Model defaults to `claude-sonnet-4-6`;
switch to `claude-opus-4-8` in ⚙ for deeper analysis.

## Secrets (read at runtime, never written to files)
- **Anthropic key** — read from the **`ANTHROPIC_API_KEY`** env var, server-side. Coaching is
  proxied through the hub (`POST /coach`), so the key never reaches the browser. It's already in
  `~/.zshrc`, so launching from a normal shell works. For a systemd service on the HA Green, add
  it to the unit (`Environment=ANTHROPIC_API_KEY=...` or an `EnvironmentFile`). The ⚙ key field
  in the app is now an *optional per-device fallback* only.
- **HA token** — read from **`~/scripts/tuya/ha_token`** (the same file the energy proxy uses),
  via `ha_token_file` in `config.json`. Set `ha_token` inline only to override.

## Data in: Oura + Apple Health via Health Auto Export
The Oura ring syncs sleep, HRV and resting HR into Apple Health; everything else (steps,
active energy, weight, etc.) is already there. One bridge covers both:

1. Install **Health Auto Export** (iOS).
2. **Oura app → Settings → enable Apple Health** so the ring writes sleep/HRV/RHR into Health.
3. In Health Auto Export create an **Automation → REST API**:
   - URL: see below — at home use the add-on directly; **off-network use the Nabu Casa relay**.
   - Format: **JSON**, aggregate **daily**, run e.g. each morning + evening.
   - Metrics: heart rate variability, resting heart rate, sleep analysis, step count,
     active energy, exercise time, respiratory rate, blood oxygen, body mass.
4. The hub tags each reading with its source and flags Oura-sourced ones (shown in the metrics panel).

The parser is tolerant of Health Auto Export's shape; new metrics are easy to map in
`METRIC_MAP` in `server.py`.

### Ingest from anywhere — Health Auto Export's native HA export (preferred)
Health Auto Export has a built-in **Home Assistant** integration that posts directly into HA
(over Nabu Casa, with a token), so it works on cellular/any network and needs no add-on
reachability and no relay. Data lands as HA sensors; the hub reads them from HA.

1. Health Auto Export → Automation → choose the **Home Assistant** export.
   - URL: your Nabu Casa URL `https://xudiffmlhtk3kpgfp8ess0algkjikw8x.ui.nabu.casa`
   - Token: a HA Long-Lived Access Token (reuse `~/scripts/tuya/ha_token` or make a new one).
   - Select the metrics; aggregate daily; run a manual sync once to create the entities.
2. The hub reads those entities via `metrics_from_ha()` (matched by entity-id substring in
   `HA_METRIC_PATTERNS`). Confirm/adjust the patterns once the real entity names exist.

This is independent of the add-on: metrics flow into HA immediately, and are usable on HA
dashboards too. The add-on is still needed for the workout card/queue, coaching, and the app UI.

## Workout to your phone (Home Assistant)
Fully wired and tested. The hub tries the remote **Nabu Casa** URL first
(`ha_url`, works off-LAN) and falls back to the LAN URL (`ha_url_local`) when home. Token comes
from `~/scripts/tuya/ha_token`. Notify service is `notify/mobile_app_marks_iphone` (confirmed
against HA; a second device `_2` also exists).

1. Test: `python3 server.py notify` → buzzes the iPhone with today's session.
4. Schedule a morning push (HA Green crontab):
   ```
   30 6 * * *  cd /path/to/health && /usr/bin/python3 server.py notify
   ```
   (Rest days send a "Rest day" nudge.) The full session detail lives in the Notion program,
   linked from the push and the app.

## Workout to an HA dashboard card (primary delivery — no Mac)
The workout is a **queue, not a calendar**: a cursor points at the current session and only
advances when you mark it **done**. No date logic. The block flattens to an ordered list of the
training sessions (Mon Strength A · Tue Z2 · Thu Strength B · Fri Work Cap · Sat long Z2 × 4
weeks = 20 sessions; rest days excluded — set `INCLUDE_REST=True` to include them).

The hub writes the *current* session into Home Assistant as `sensor.todays_workout` (with a
ready-to-render `markdown` attribute); a Lovelace card shows it and a button marks it done.
Runs entirely on the HA Green, reachable anywhere via Nabu Casa.

**Mark done on the card — one-time HA setup:**
1. Create the toggle: Settings → Devices & Services → **Helpers** → **+ Create Helper** →
   **Toggle** → name `Workout done` (→ `input_boolean.workout_done`).
2. Add the card (dashboard → Edit → Add Card → Manual):
   ```yaml
   type: vertical-stack
   cards:
     - type: markdown
       content: "{{ state_attr('sensor.todays_workout','markdown') }}"
     - type: button
       name: Mark done → next session
       icon: mdi:check-bold
       tap_action:
         action: call-service
         service: input_boolean.turn_on
         target:
           entity_id: input_boolean.workout_done
   ```

**How advancing works:** the hub polls `input_boolean.workout_done` every 20s
(`_start_done_watch`); when it's on, it logs the session done, moves the cursor forward, resets
the toggle, and re-pushes the card (which auto-refreshes in HA). Cursor + completion log live in
`data/state.json`.

**Endpoints / CLI:** `GET /workout/current` (+`.txt`), `POST /done`, `POST /cursor {cursor:N}`,
`GET /sequence`. CLI: `python3 server.py card | done | reset | workout`.

**Notes:** states set via the REST API don't survive an HA restart; the hub re-pushes the
current session on its next startup, so the card self-heals. The cursor itself is durable (on disk).

> Marking done on the card requires the hub running on the HA Green so the poll is always live
> without the Mac — see **Deploy to the HA Green** below.

## (Optional) Apple Notes page
A single note **"🏋️ Today's Workout"** is overwritten each morning, so there's one stable
place that always shows today's session (and it syncs to the iPhone via iCloud).

**Constraint:** Apple Notes can only be written from an Apple device — not the HA Green. So the
write step runs on the Mac or the phone (the hub just provides the content via `/workout/today.txt`).

**Mac (installed & running):** `notes_workout.py` writes the note; scheduled by launchd at
`~/Library/LaunchAgents/nz.reesmoore.health-workout-note.plist` (6:30am daily, RunAtLoad catches
up after sleep). Manual run: `python3 notes_workout.py`. Remove: `launchctl unload <plist>`.
Trade-off vs the "always-on lives on the HA Green" rule: the Mac must be on around training time
— acceptable here because only an Apple device can write Notes, and the Mac is the most capable one.

**iPhone Shortcut (works anywhere, no Mac needed):**
1. New Shortcut "Update Workout Note":
   - **Get Contents of URL** → `http://192.168.0.209:8768/workout/today.txt` (home WiFi). For
     off-home use, deploy the hub on the HA Green writing the text into HA's `config/www/health/`
     and fetch `https://<your>.ui.nabu.casa/local/health/today_workout.txt` (reachable anywhere).
   - **Find Notes** where Name is `🏋️ Today's Workout` → **Delete Notes** (clears the old one).
   - **Create Note** with the fetched text (its first line becomes the title).
2. **Automation** → Personal → Time of Day 6:30am, daily → Run "Update Workout Note", Run Immediately.

(The HA push notification from the previous step still works as an optional nudge.)

## Training program
The schedule mirrors the Notion **"Training — Living Program"** (Block 1): Mon Strength A ·
Tue Z2 · Wed REST · Thu Strength B · Fri Work Capacity · Sat long Z2 · Sun REST, with
week-in-block driving reps/durations. It's deterministic, computed from `block_start` in
`config.json`.

**When you re-plan a block (~22nd monthly):** update `block_start`, `block_name`, and the
`PROGRAM` / `WEEK_PARAMS` tables in `server.py` to match the new Notion page.

## Deploy to the HA Green (as a local add-on)
This folder doubles as a Home Assistant local add-on (`config.yaml`, `Dockerfile`, `run.sh`).
The add-on runs the hub always-on, boots on start, auto-restarts (watchdog), persists state on
`/share/health`, and uses the Supervisor token for HA access (no Nabu Casa / long-lived token).

1. **Get the folder onto the Green** at `/addons/health_hub` (use whichever works):
   - Mac on the home WiFi: `scp -r ~/health root@192.168.0.209:/addons/health_hub`
     (or the SSH add-on's port). *Note: if the Mac is on a different network than the Green,
     this won't route — get on home WiFi, or use the Samba/Studio Code Server add-on, or
     `git clone` from a private repo in the Green terminal.*
2. **Install:** Settings → Add-ons → Add-on Store → ⋮ (top right) → *Check for updates* →
   open **Health Hub** → **Install**.
3. **Configure:** in the add-on's *Configuration* tab set `anthropic_api_key`, confirm
   `notify_service` and `done_helper`. *Start* the add-on and enable *Start on boot* + *Watchdog*.
4. **Point clients at it:** Health Auto Export → `http://192.168.0.209:8768/ingest`; open the app
   at `http://192.168.0.209:8768/`. The done-watch, card refresh, and coaching now run on the
   Green with no Mac involved.

(While the hub runs only on the Mac, the HA card still works but the done-button advance and
Apple-data ingest only work when the Mac is on and reachable from the phone/HA.)
