# AT-Extender Home Assistant add-on — project notes

Handoff from a previous Claude session (2026-10-18/19). The user (Furkan) writes German; answer in German.

## Goal
Run the ALDI TALK "AT-Extender" bot (auto top-up of 1 GB data when < 1 GB is left) as a
**local Home Assistant add-on**, with HA entities via MQTT.

Origin: https://github.com/G-Chalupa/AT-Extender (v1.2.2, Python + Playwright). Its upstream
https://github.com/Dinobeiser/AT-Extender currently has an EMPTY `at-extender.py` (v1.2.3), so the
original auto-update would overwrite the script with nothing → auto-update was removed entirely.

Note: ALDI TALK's terms (Leistungsbeschreibung 10.3 g) forbid automation; the user knows this.

## Environment
- Mini-PC with Proxmox → Home Assistant OS VM, local IP **192.168.50.116** (domain 270700.xyz)
- Add-ons in use: Advanced SSH & Web Terminal, Samba share, Mosquitto broker (+ MQTT integration)
- Windows PC maps Samba shares: H: config, I: addons, J: share, K: backup
- Repo layout: `repository.yaml` + `at_extender/` (the add-on).
- Since 2026-10-19 the add-on is installed **from this GitHub repo** (added as add-on repository in HA),
  slug **`72298cf0_at_extender`**. The old local copy (`local_at_extender`, /addons/at_extender) is uninstalled.
- Working clone for Claude Code: **/share/at_extender_src** (the Claude Code add-on cannot see /addons).
  `gh` is logged in as Furkan2000 (token may be lost on Claude Code add-on restart → `gh auth login` again).

## Files
- `config.yaml` — add-on manifest (version **1.0.2**, arch amd64/aarch64, `services: mqtt:want`, `map: share:rw`).
  Options: rufnummer, passwort, schwelle_gb (1.0), automatisch_nachbuchen, intervall_modus
  (smart|fest|zufall), intervall_fest/min/max, telegram_aktiv/bot_token/chat_id, log_level.
- `Dockerfile` — `python:3.12-slim-bookworm` (NOT Alpine; Playwright needs glibc),
  playwright==1.64.0 + chromium, paho-mqtt==2.1.0, requests.
- `run.py` — everything: options from /data/options.json, state in /data/state.json,
  cookies in /data/cookies.json, MQTT discovery, Playwright logic, main loop.
- `translations/de.yaml`, `en.yaml`, `DOCS.md` (user docs incl. example notify automation).

## How run.py works
1. Open dashboard https://www.alditalk-kundenportal.de/portal/auth/uebersicht/ with saved cookies.
2. If redirected to login (https://login.alditalk-kundenbetreuung.de/signin/XUI/#login/):
   fill `#input-5` (number) / `#input-6` (password), `click_login_button()` tries role/text/
   one-button/old class/submit, finally Enter in password field. Login now WORKS.
3. `get_datenvolumen()` iterates all `one-usage-meter`, parses `one-heading` ("16,5 GB" / "812 MB"),
   picks the meter that contains the +1 GB button (`one-button[slot="action"]:has(one-icon[name="plus"])`),
   else label `Inland`/`Inland & EU`, else first. Returns (gb, meter_locator).
   Portal DOM (Oct 2026): `one-usage-meter > one-stack > [one-text "Noch", one-heading "16,5 GB",
   one-text "von 25 GB"]`, `one-button[slot=footer]` = label ("Inland", "Roaming EU", "Inland & EU"),
   `one-button[slot=action][circle]` = +1 GB. The +1 GB button is ALWAYS visible (greyed when ≥1 GB),
   so only the threshold check prevents clicking.
4. If gb < schwelle and auto top-up on: click plus in that meter, click a confirmation button if one
   appears ("Jetzt nachbuchen"/"Nachbuchen"/"Bestätigen"/"Ja" — unverified, never seen yet),
   reload and re-read volume → status `nachgebucht` or `nachgebucht_unbestaetigt`.
5. On failure: `save_debug()` writes screenshot + HTML to **/share/at_extender/** (keeps last 10 pairs).
6. 3 attempts per run; interval by mode (smart: ≥10 GB 60–90 min … just above threshold 1–1.5 min).
   MQTT button can wake the loop early.

## MQTT / HA entities (device "ALDI TALK …<last 4 digits>")
Topics under `at_extender/<last4>/`: `state` (JSON: gb, status, meldung, last_check, last_topup,
next_check, auto_topup), `availability`, `check/set`, `auto/set`.
Entities: sensor.aldi_talk_datenvolumen, _status (attr meldung), _letzte_pruefung,
_letzte_nachbuchung, _naechste_pruefung, button.aldi_talk_jetzt_pruefen,
switch.aldi_talk_automatisch_nachbuchen. Broker credentials come from Supervisor
(`http://supervisor/services/mqtt` with SUPERVISOR_TOKEN).

## History / status
- 1.0.0: initial add-on. Error "Login-Button nicht gefunden" (upstream used get_by_text("Anmelden").first,
  which hit a hidden element).
- 1.0.1: robust login + debug dumps → login OK; then "Datenvolumen nicht gefunden"
  (portal replaced `one-group` with `one-stack`).
- 1.0.2: new meter parsing, tested locally against the user's real portal HTML (reads 16.5 GB,
  simulated 812 MB → clicks plus). **Confirmed working on HA** (2026-10-19: login OK, reads 11.90 GB).
- 1.1.0: Telegram message after every check (option `telegram_jede_pruefung`), Telegram menu (any text →
  inline buttons Start/Stopp/Jetzt prüfen, only from `telegram_chat_id`, long-polling thread `TelegramBot`,
  offset in state.json `tg_offset`), pause state `STATE["paused"]` (persists), HA switch
  `switch.aldi_talk_ueberwachung` (topic `monitor/set`), status `gestoppt`. Add-on bot is a different bot than
  HA's own telegram_bot integration (polling) — don't share tokens or getUpdates will 409.
- 1.2.0: inline menu replaced by a persistent reply keyboard (`keyboard()`, `is_persistent`), attached to
  every bot message; buttons arrive as plain text (BTN_START/BTN_STOP/BTN_CHECK/BTN_STATUS). No callback_query anymore.
- 1.3.0: early top-up. Between schwelle_gb and `nachbuchen_versuch_gb` (default 2.0) `plus_state()` reads the
  +1 GB button; greyed = style rgb(212,212,212) / `inverse` attr / child color="muted" / disabled (seen Oct 2026 at
  14.9 GB). Active → save_debug("frueh-nachbuchen") + click; no effect → status ok, 15 min cooldown
  (`STATE["frueh_klick_ts"]`), last_topup restored. Below schwelle: clicks regardless (old behavior).
  **Unverified:** what the ENABLED button looks like — check the first frueh-nachbuchen dump / log line
  "+1-GB-Knopf ..." when volume drops under 2 GB and adjust plus_state() if it is misdetected.

## Open points / next steps
- First real top-up **confirmed 2026-10-20**: 0.00 GB → click → 1.00 GB, status `nachgebucht`,
  no confirmation dialog appeared. Before that it sat at 1.01 GB for a long time without booking
  (user says ALDI allows top-up slightly above 1 GB) → reason for 1.3.0 early top-up.
- Unrelated noise in HA core log: flightradar24 HTTP 429, Bresser template missing `battery_ok`.

## Workflow tips
- Release loop: edit in /share/at_extender_src → bump `version` in config.yaml (required, otherwise
  HA sees no update) → commit + `git push` → `ha store reload` → `ha apps update 72298cf0_at_extender`.
  Logs: `ha apps logs 72298cf0_at_extender`. Debug dumps: /share/at_extender/. Set option log_level=debug for per-meter details.
- Never print the user's phone number/password in logs; only the last 4 digits are used.
