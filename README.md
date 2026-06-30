# Mark's Health Hub — Home Assistant add-on repository

A Home Assistant add-on: evening check-in + cursor-based workout queue (MTI / Attia-Galpin
aligned), Oura/Apple Health ingest, AI coaching, and a Notion food-log macro summary. Runs
24/7 on the HA Green — no Mac required.

This is a **private add-on repository**. The add-on itself lives in [`health_hub/`](health_hub/).

## Install on Home Assistant (one time)

1. **Add this repository** — Settings → Add-ons → **Add-on Store** → ⋮ (top-right) →
   **Repositories** → paste the repo URL.
   - Private repo: use a token-embedded URL so HA can pull it:
     `https://<GITHUB_TOKEN>@github.com/markreesnz/health-hub`
     (create a fine-grained PAT with **read-only** access to this repo at
     github.com/settings/tokens). If you'd rather skip the token, make the repo **public** —
     there are no secrets in it.
2. **Install** — the store now lists **Health Hub** under your repository → open → **Install**.
3. **Configuration** tab → paste your two secrets → **Save**:
   - `anthropic_api_key` — your Anthropic API key (console.anthropic.com). Powers food
     parsing + coaching. Billed to your own Anthropic account.
   - `notion_token` — your Notion internal integration token (notion.so/my-integrations).
     The food-log + workout-note pages must be shared with that integration.
4. **Info** tab → **Start**, then enable **Start on boot** + **Watchdog**.
5. Open it: **Health** in the HA sidebar (works remotely via Nabu Casa), or
   `http://192.168.0.209:8768/` on home WiFi.

## Updating (from anywhere)

1. Edit code in `health_hub/`, **bump `version`** in `health_hub/config.yaml`, `git push`.
2. In HA: Add-on Store → ⋮ → **Check for updates** → Health Hub → **Update**.

Because edits + pushes happen from any machine with `git`, and updates are pulled by HA over
the internet, **the app is fully maintainable without a LAN connection or any one person.**

## Data & persistence

State, the generated `program.json`, and backups live on the HA Green's `/share/health/`
(mapped in, seeded once) — so **updates never reset your training/food progress.** Nothing
personal is stored in this repo (see `.gitignore`).

## Secrets

No keys are committed. They're entered once in the add-on **Configuration** tab and injected
as env vars by `run.sh`. If you ever reinstall, re-enter your own Anthropic + Notion keys —
you control both, so you never depend on anyone else to restore access.
