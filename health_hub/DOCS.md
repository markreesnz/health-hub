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

## Alcohol
- **Drinks live in the food log** (Food tab): the 🍷 Wine button next to Add logs a 150 ml glass
  (1.5 NZ standard drinks) at the current time, and they show as a "Drinks" group under the meals
  with the reset day, dry streak and any rule broken. "+ drink at an earlier time" back-fills one;
  "No drinks today" confirms a dry day. The drinking day rolls over at 05:00, so a 00:30 glass
  belongs to the night before.
- A day only counts as **dry** once confirmed ("No drinks today", or the evening check-in saved with no
  drink). Blank days are "not logged", never dry.
- Rules (config.json `alcohol`): 30-day reset from `reset_start` (change it with `POST /alcohol/settings`; the
  override lives in `/share/health/data/alcohol.json`), then dry Mon–Thu, max 2 standard drinks,
  nothing after 20:00.
- Drinks are stored per day in `behaviours.json` (`drinks: [{id, time, kind, std}]`); the 0/1
  `drink` flag and `input_boolean.had_a_drink` stay in sync. The pre-change file is kept once as
  `backups/behaviours-before-drink-log.json`.
- The weekly review (rules kept/broken, veg cups, weight, HRV/sleep after drinking vs dry nights)
  has no card in the app; it is served at `/review/weekly` for the Health Connector.
- API: `GET /alcohol?date=`, `GET /review/weekly?end=`, `POST /alcohol/drink {kind|std, time?, date?}`,
  `POST /alcohol/remove {date, id}`, `POST /alcohol/none {date?}`, `POST /alcohol/settings {reset_start}`.
- Tests: `python3 -m unittest test_alcohol` (from `health_hub/`).

## Food & drink → that night (Insights tab)
- Each Oura sync also keeps, per night: when heart rate bottomed out (clock time and hours after
  bed), HRV recovery (2nd half of the night minus the 1st), time to fall asleep, time awake, and
  the readiness breakdown (HRV balance, recovery index, resting HR, body temperature, sleep
  balance, previous-day activity). Oura's existing permissions already cover these.
- The Insights tab compares each evening (drinks, late meal, carbs over target, calories 10%+ over,
  off-plan food, all 9 veg cups) with the next night's numbers over the last 90 days. A comparison
  shows once there are 3+ nights each way, and is only coloured when the difference is clear with
  5+ nights each way.
- API: `GET /insights/food-sleep?days=90` (also used by the Health Connector's `health_food_sleep`).

## Photo logging and meal timing (Food tab)
- **📷 next to the food box** logs a meal from a photo: the app shrinks it, Claude identifies each
  food with a portion estimate, macros, Wahls cups and off-plan flags, and nothing is saved until
  you tap "Add to <meal>" (✕ removes a wrong item). Text typed in the box is sent as a note
  ("olive oil dressing"). `POST /food/photo {image, media_type, note}` parses only.
- **Time eaten** (the time box, default now) is stored on every item as `at`.
- Under the macro tiles: last thing eaten vs bedtime (that night's Oura bedtime, or the 14-night
  usual), amber under 3 h, and the eating window.
- In "Food & drink → that night", "Ate within 3 h of bed" now comes from logged times, falling
  back to the check-in toggle on days without them.

## Training this week (Today tab)
- Weekly targets (Mon–Sun), default 3 strength sessions, 150 Zone 2 minutes and 1 HIIT; change them
  with "Change targets" (saved in `/share/health/data/training.json`). Strength is counted, not planned.
- Sessions come from Health Auto Export workouts, from Oura (its detections plus the Apple Watch
  workouts it imports from Apple Health; pulled on every Oura sync, first 8 weeks backfilled once) and
  from the + Strength / + Zone 2 / + HIIT buttons. Apple and Oura copies of one workout are merged.
- Zone 2 minutes are minutes with heart rate in range (default 120–135 bpm) from Oura's heart-rate
  samples or the export's per-minute heart rate; without samples, an endurance workout counts when
  its average heart rate is in range (±5), or in full when there's no heart rate at all (marked).
- ✕ deletes a hand-logged session, or hides a device session that wasn't real training.
- API: `GET /training?date=`, `POST /training/log {kind, minutes?, date?}`, `POST /training/remove {id}`,
  `POST /training/settings {strength, zone2_minutes, hiit, zone2_hr}`.
- The old session queue (Plan tab, morning workout push) is unchanged and no longer shown on the Today tab.

## Food tab, Oura-style
- Top: the day's rating (Nutritious / Good / Fair / Limited, from the ZOE day score), a headline and a
  short note written by Claude (`GET /food/summary`, cached until the log changes), and the week on
  the four levels.
- Bottom: a 24-hour meal-timing dial — meals, last night's sleep, and "kitchen closes" (3 h before
  your usual Oura bedtime) — with last meal, kitchen-closes time and eating window.

## Loading
- Each dashboard request fails on its own; failures show in the banner at the top. Only when nothing
  answers does the page say it can't reach the hub, with the reason (expired Home Assistant sign-in,
  add-on restarting) and a Reload button.

## Steps and active calories: Oura only
- No Apple Watch: steps and active calories come only from Oura's daily activity (synced with the
  rest of Oura). Apple Health / Home Assistant values for these two are ignored; everything else
  from Apple Health (weight etc.) still flows. The first start after updating re-pulls 90 days so
  the history is all Oura.

## Today: habits that drive action (1.14.0)
- **Today tab** answers "what should I do today?": readiness, one headline sentence naming the one
  thing that matters, habit pills (only the slipping one in orange; tap for the week), Zone 2 pace
  (minutes vs target, days left, minutes/day needed, 4-week adherence), up to three actions, and the
  evening check-in (from 5pm) asking only what the logs can't answer.
- **Habits** (`/share/health/data/habits.json`): Zone 2 (weekly minutes), Eat clean (Wahls score
  70+, or one tap on days with no food log), No alcohol (your alcohol rules). Library: Kitchen 3h,
  Strength, HIIT, Steps; plus your own one-tap habits. "Choose habits" on Today switches them.
- **Slipping** = furthest behind this week: weekly targets against even pace through the week,
  daily habits by the share of known days missed; 0.2+ counts. Only the worst gets a recovery
  action ("38 min easy Zone 2 today — 150 min to go with 4 days left"). Zone 2 asks cap at 90 min/day.
- Evening check-in: "Any drinks today?" only if none were logged, "Did you eat clean?" only if no
  food was logged, plus your own one-tap habits. Stress, eczema and late meal are no longer asked.
- Zone 2 default target is 180 min/week at 120–135 bpm.
- The MTI session queue is retired: no Plan tab, no morning workout push, no HA card. Set
  `"workout_queue": true` in config.json to bring it back.
- API: `GET /today/plan`, `GET /habits`, `POST /habits/mark {id, value, date?}`,
  `POST /habits/config {active? | add_custom? | remove_custom?}`, `POST /actions/done {key}`.
- Tests: `python3 -m unittest test_habits test_training test_alcohol`.

## Trends and insights (1.15.0)
- Tabs are now **Today · Log · Trends**. Trends holds this week's insight, weight, the sleep/heart/
  activity charts and last night's numbers; the old correlation lists and "significant changes"
  are gone.
- **Behaviour signals** per evening: drinks, late meal (within 3 h of bed), heavy day (calories 20%+
  over your median), high carbs (over target), off-plan food, low veg (under 6 cups), Zone 2 day
  (30+ min), HIIT day, strength day, late bedtime (45+ min after your 28-day median).
- **Outcomes** the next night: HRV, lowest heart rate, deep sleep, sleep efficiency, sleep score,
  hours after bed that heart rate bottomed out. Last 120 days.
- **Bar**: 8+ nights with and without, |Cohen's d| 0.3+, 90%+ confidence (Welch), and an action for
  that direction — otherwise it isn't shown. Ranked by effect size × confidence.
- **One headline a week** (kept for the ISO week). A surfaced insight returns only when its effect
  moves 30%+ or flips (`/share/health/data/insights_state.json`). The rest sit under "more insights".
- API: `GET /insights/ranked`. Tests: `test_insights.py`.

## Experiments and the Sunday review (1.16.0)
- Tabs: **Today · Log · Review · Trends**. The Sunday "weekly review is ready" item on Today clears once you open Review.
- **Experiments** (`/share/health/data/experiments.json`): one at a time, 14 days by default
  (7–42). Presets are detected automatically — No alcohol at all, Kitchen closes 3 h before bed,
  30 min Zone 2 every day, Lights out by 22:30, 9 cups of veg — or name your own and tick it off
  each evening. Today shows the day and adherence.
- **Result** at the end (or "End now"): adherence, then HRV, resting HR, deep sleep and sleep
  efficiency on nights after adherent days vs the 28 nights before the start (needs 5+ nights each;
  "changed" = |d| 0.3+ at 80%+ confidence), and the weight trend (kg/week slope, ±0.25 counts).
  Under 60% adherence the verdict is "not enough adherence to tell". A result that helped can be
  made a habit in one tap. Suggestions skip anything that already helped or showed no effect in the
  last 90 days, and put the experiment your top insight points at first.
- **Review** (any day; complete on Sunday, when Today shows "Your weekly review is ready"): habit
  grid, Zone 2 total, 7-day smoothed weight, weekly HRV and sleep score vs the previous five weeks,
  experiment progress/result, the week's insight, and next week's proposals — the two habits
  furthest behind, an experiment if none is running, the insight's action — each Accept / Edit /
  Skip (`/share/health/data/weekly_plans.json`). Accepted actions show on Today as "This week's
  plan" through the next week; an accepted experiment starts the next day.
- API: `GET /experiments`, `POST /experiments/start {preset | name, days?}`, `/experiments/mark
  {value}`, `/experiments/end`, `/experiments/promote {id}`; `GET /review?date=`,
  `POST /review/decide {week, id, action: accept|edit|skip, text?}`, `/review/tick {week, id}`.
- Tests: `test_experiments.py`.
- **Usual foods**: the Log tab shows chips for foods logged 2+ times in the last 30 days; one tap adds
  that item to the selected meal at the time in the box (`GET /food/usual`).

## Withings (1.17.0)
- Weight and body fat come straight from Withings' cloud (OAuth, like Oura) — no Apple Health hop.
- Setup, once: create a free app at developer.withings.com (Public API) with an https callback URL
  (e.g. `https://localhost/withings`, or your Nabu Casa URL if that's refused). In the app: ⚙ →
  Withings scale → paste client id, secret and the callback URL → Authorise → paste the URL the
  browser lands on. The first connect pulls a year of weigh-ins; after that it syncs every 30 minutes.
- One reading per day: the first weigh-in of the (NZ) day, so days compare like with like.
- Once connected, Withings owns `weight` and `body_fat`; Apple Health / Home Assistant values for them
  no longer overwrite it. Tokens and app credentials live in `/share/health/data/withings.json`.
- API: `GET /withings/status`, `POST /withings/creds {client_id, client_secret, redirect_uri}`,
  `POST /withings/code {code}`, `POST /withings/sync {days?}`. Tests: `test_withings.py`.

## Background sync (1.17.1)
- An always-on loop syncs Oura every hour (last 7 days) and, every 20 seconds, takes Home Assistant's
  metric snapshot, picks up workouts sent via HA, refreshes the check-in sensors and watches the
  check-in button. Opening the app also syncs Oura (last 2 days, at most every 10 minutes); ⚙ →
  Sync now forces one. Withings syncs every 30 minutes on its own loop.
- 1.14.0–1.17.0 accidentally stopped this loop along with the retired session queue, so in those
  versions Oura only synced while the app was open and HA-sent workouts weren't picked up.

## Review page, reworked (1.18.0)
- No graphs. The Review shows: one sentence on the week, the habit-by-day grid, the **best and worst
  night** (sleep score, else HRV) with what the evening before held (dry, drinks, late meal, Zone 2,
  heavy day…), **your body this week** — weight, HRV, sleep score and resting heart rate as this
  week's average vs the previous 4 weeks, in words ("3 ms lower than usual"; "about the same" inside
  ±0.2 kg / ±2 ms / ±2 / ±1 bpm; only "worse" in the attention colour) — then next week's actions and
  the experiment.
- It opens on the week just ending on Sunday, otherwise the last complete week; "‹ Earlier / Later ›"
  moves between weeks (`GET /review?date=`).

## Correlations restored (1.18.2)
- The full patterns section is back on Trends, under this week's insight: food & drink → that night,
  all correlations (each behaviour/metric → next-day recovery, HRV, resting HR, sleep, with n and
  significance), significant trends over the last 3 months, and recent check-ins. 1.15.0 had removed
  it in favour of the single weekly insight; both now show.

## Correcting a session (1.18.4)
- Each Apple/Oura session on the Training card has ✎: say what it really was (Zone 2, strength, HIIT,
  not training) and, for Zone 2, how many minutes counted. Corrections are kept when the device
  resyncs the same workout (`user_category`, `user_zone2` in workouts.json; `POST /training/edit
  {id, kind?, zone2?}`).
- A session that gets 0 Zone 2 minutes now says why, e.g. "average HR 142 outside 120–135". If your
  rowing regularly sits above the range, consider widening it under Change targets.

## Faster Oura workouts (1.18.5)
- The background loop syncs Oura every 15 minutes (was hourly), and after an on-open sync brings new
  data the Training card and Today redraw straight away.
- The Training card shows when Oura workouts were last checked and how many were found, or the exact
  error if the check failed, with **Sync now** (`oura_workout_sync.json`).
- A workout only reaches the hub after the Oura app has synced it to Oura's cloud.

## Processed-food risk (1.20.0)
- Each logged food gets a ZOE-style processing level: Unprocessed / No risk / Low / Medium / High
  (`processing_risk.py`). Medium and High show a small tag on the item; tap it for what drove it.
- The Food tab shows a **Processing** bar: share of today's calories at each level, amber once
  25%+ of calories are medium/high risk. `GET /food` carries it as `processing`.
- Inputs come from the food parser (grams, sugar, sodium, NOVA class, estimated eating speed in
  kcal/min, typical additives). Scoring is deterministic: hyperpalatable (Fazzino 2019 cut-offs)
  +1, eaten faster than 72 kcal/min +1, one high-risk additive +2 (two or more +3) or any
  medium-risk additive +1. 0 = No risk (Low if NOVA 4), 1 = Low, 2 = Medium, 3+ = High.
  NOVA 1 with no additives = Unprocessed.
- ZOE hasn't published its algorithm; this is a reconstruction from what ZOE has said publicly
  (the three drivers, the 72 kcal/min threshold, its additive examples), calibrated so ZOE's
  published examples (butter, dark chocolate, Weetabix, sourdough, milk chocolate, white bread,
  nuggets) land where ZOE puts them — see `test_processing_risk.py`.
- Food logged before 1.20.0 has no inputs and stays unscored. The prompt change refreshes the
  parse cache once.

## ZOE rules replace Wahls (1.21.0)
- The Wahls tracker, veg cups, seaweed and on/off-plan flags are gone. Food is scored against
  ZOE's rules (`zoe_rules.py`): **30 different plants a week** (veg, fruit, wholegrains, legumes,
  nuts, seeds 1 point; herbs and spices ¼; each plant once per Mon–Sun week), **3 fermented
  portions a day**, **30 g fibre a day**, **minimal processed food** (the processed-food risk
  scale, calorie-weighted) and a **12-hour-or-less eating window**.
- The **ZOE day score** (0–100) is the mean of five parts: plants vs the pace for 30 by Sunday,
  ferments/3, fibre/30, processing score, eating window. It drives the day rating, the week
  chart, the Today "ZOE" card, the history grid and the Eat clean habit (70+).
- The parser now returns `plants`, `fibre` and `fermented` per item. Food logged before 1.21.0
  has none of these and isn't scored.
- Insights and food → sleep use "processed food" and "low fibre" / "30 g+ fibre" in place of
  off-plan food, low veg and all 9 cups. The "9 cups of veg" experiment is now "30 g fibre every
  day" (a running veg experiment is scored on fibre).
- Still gluten-, dairy- and egg-free, no liver or oysters; legumes and wholegrains are back in.
