# -*- coding: utf-8 -*-
"""
AT-Extender als Home-Assistant-Add-on.

Browser-Logik übernommen aus https://github.com/G-Chalupa/AT-Extender (v1.2.2),
ergänzt um Add-on-Optionen, MQTT-Discovery, einen Prüf-Button und ein Telegram-Menü.
Kein Auto-Update: Das Upstream-Skript ist derzeit leer.
"""
import json
import logging
import os
import random
import re
import threading
import time
from datetime import datetime, timezone

import requests
from playwright.sync_api import sync_playwright

# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #
DATA_DIR = os.environ.get("AT_DATA_DIR", "/data")
OPTIONS_FILE = os.path.join(DATA_DIR, "options.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
COOKIE_FILE = os.path.join(DATA_DIR, "cookies.json")
DEBUG_DIR = os.environ.get("AT_DEBUG_DIR", "/share/at_extender")

LOGIN_URL = "https://login.alditalk-kundenbetreuung.de/signin/XUI/#login/"
DASHBOARD_URL = "https://www.alditalk-kundenportal.de/portal/auth/uebersicht/"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]

with open(OPTIONS_FILE, encoding="utf-8") as f:
    OPT = json.load(f)

logging.basicConfig(
    level=getattr(logging, OPT.get("log_level", "info").upper(), logging.INFO),
    format="%(asctime)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("at_extender")
logging.getLogger("paho").setLevel(logging.WARNING)

RUFNUMMER = str(OPT["rufnummer"]).strip()
PASSWORT = str(OPT["passwort"])
SCHWELLE = float(OPT.get("schwelle_gb", 1.0))
SUFFIX = RUFNUMMER[-4:] if len(RUFNUMMER) >= 4 else RUFNUMMER  # für Namen, nie die volle Nummer

# --------------------------------------------------------------------------- #
# Zustand (überlebt Neustarts)
# --------------------------------------------------------------------------- #
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(STATE, f)
    except Exception as e:
        log.warning(f"Zustand konnte nicht gespeichert werden: {e}")


STATE = load_state()
STATE.setdefault("last_gb", None)
STATE.setdefault("last_check", None)
STATE.setdefault("last_topup", None)
STATE.setdefault("paused", False)
# Option ist Startwert; danach gilt, was in HA am Schalter eingestellt wurde.
if STATE.get("auto_option_seen") != OPT.get("automatisch_nachbuchen", True):
    STATE["auto_topup"] = bool(OPT.get("automatisch_nachbuchen", True))
    STATE["auto_option_seen"] = STATE["auto_topup"]
save_state()

WAKE = threading.Event()    # weckt die Hauptschleife (Prüfen, Start, Stopp)
MANUAL = threading.Event()  # Prüfung auch dann ausführen, wenn die Überwachung pausiert ist


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def local_hm(iso):
    """ISO-Zeitstempel -> 'HH:MM' in lokaler Zeit (oder '-')."""
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%H:%M")
    except Exception:
        return "-"


def request_check():
    MANUAL.set()
    WAKE.set()


def set_paused(paused):
    STATE["paused"] = bool(paused)
    save_state()
    log.info(f"Überwachung {'gestoppt' if paused else 'gestartet'}.")
    WAKE.set()


# --------------------------------------------------------------------------- #
# Telegram (optional)
# --------------------------------------------------------------------------- #
TG_TOKEN = OPT.get("telegram_bot_token") or ""
TG_CHAT = str(OPT.get("telegram_chat_id") or "").strip()
TG_ON = bool(OPT.get("telegram_aktiv")) and bool(TG_TOKEN) and bool(TG_CHAT)


def tg_api(method, http_timeout=15, **params):
    """Ruft die Bot-API auf. Gibt 'result' zurück oder None."""
    for key in ("reply_markup", "allowed_updates"):
        if key in params and not isinstance(params[key], str):
            params[key] = json.dumps(params[key])
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=params, timeout=http_timeout)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"{r.status_code} {data.get('description', '')}")
    return data.get("result")


BTN_START = "▶️ Überwachung starten"
BTN_STOP = "⏸ Überwachung stoppen"
BTN_CHECK = "🔄 Jetzt prüfen"
BTN_STATUS = "ℹ️ Status"


def keyboard():
    """Feste Tastatur unter dem Eingabefeld; der Start/Stopp-Knopf folgt dem aktuellen Zustand."""
    toggle = BTN_START if STATE.get("paused") else BTN_STOP
    return {"keyboard": [[toggle], [BTN_CHECK, BTN_STATUS]],
            "resize_keyboard": True, "is_persistent": True}


def telegram(msg):
    """Sendet an den eingestellten Chat; jede Nachricht frischt die Tastatur auf."""
    if not OPT.get("telegram_aktiv"):
        return
    if not TG_ON:
        log.warning("Telegram aktiv, aber Bot-Token oder Chat-ID fehlt.")
        return
    for _ in range(3):
        try:
            tg_api("sendMessage", chat_id=TG_CHAT, text=msg, reply_markup=keyboard())
            return
        except Exception as e:
            log.warning(f"Telegram-Fehler: {e}")
        time.sleep(3)


def menu_text():
    gb = STATE.get("last_gb")
    if STATE.get("paused"):
        lauf = "⏸ gestoppt"
        naechste = "-"
    else:
        lauf = "▶️ läuft"
        naechste = local_hm(STATE.get("next_check"))
    vol = f"{gb:.2f} GB (Stand {local_hm(STATE.get('last_check'))})" if gb is not None else "noch unbekannt"
    return (f"ALDI TALK …{SUFFIX}\n"
            f"Überwachung: {lauf}\n"
            f"Datenvolumen: {vol}\n"
            f"Nächste Prüfung: {naechste}")


class TelegramBot(threading.Thread):
    """Long-Polling: wertet die Knöpfe der festen Tastatur aus, sonstiger Text zeigt den Status."""

    def __init__(self):
        super().__init__(daemon=True, name="telegram")
        self.offset = STATE.get("tg_offset")
        self.warned = set()

    def run(self):
        log.info("Telegram-Menü aktiv.")
        while True:
            try:
                updates = tg_api("getUpdates", http_timeout=60, timeout=50, offset=self.offset or "",
                                 allowed_updates=["message"])
            except Exception as e:
                if "409" in str(e):
                    log.warning("Telegram: Bot wird woanders abgefragt (Webhook oder zweites Programm). "
                                "Menü funktioniert erst, wenn das aufhört.")
                    time.sleep(120)
                else:
                    log.debug(f"Telegram-Abfrage fehlgeschlagen: {e}")
                    time.sleep(10)
                continue
            for upd in updates or []:
                self.offset = upd["update_id"] + 1
                try:
                    self.handle(upd)
                except Exception as e:
                    log.warning(f"Telegram-Update konnte nicht verarbeitet werden: {e}")
            if updates:
                STATE["tg_offset"] = self.offset
                save_state()

    def allowed(self, chat_id):
        if str(chat_id) == TG_CHAT:
            return True
        if chat_id not in self.warned:
            self.warned.add(chat_id)
            log.warning(f"Telegram: Nachricht aus fremdem Chat {chat_id} ignoriert.")
        return False

    def handle(self, upd):
        msg = upd.get("message")
        if not msg or not self.allowed(msg["chat"]["id"]):
            return
        text = (msg.get("text") or "").strip()
        if text == BTN_START:
            set_paused(False)
            telegram("▶️ Überwachung gestartet - prüfe jetzt.")
        elif text == BTN_STOP:
            set_paused(True)
            telegram("⏸ Überwachung gestoppt. „Jetzt prüfen“ geht trotzdem.")
        elif text == BTN_CHECK:
            request_check()
            telegram("🔄 Prüfung läuft, Ergebnis kommt gleich.")
        else:
            telegram(menu_text())


def notify_result(status, meldung):
    """Telegram-Meldung nach jeder Prüfung (bzw. nur bei Fehler/Nachbuchung)."""
    jede = OPT.get("telegram_jede_pruefung", True)
    if STATE.get("paused"):
        weiter = "Überwachung ist gestoppt."
    else:
        weiter = f"Nächste Prüfung {local_hm(STATE.get('next_check'))}."
    if status == "fehler":
        telegram(f"…{SUFFIX}: ❌ Fehler: {meldung}\n{weiter}")
    elif status.startswith("nachgebucht"):
        telegram(f"…{SUFFIX}: 📲 {meldung}\n{weiter}")
    elif status == "niedrig":
        telegram(f"…{SUFFIX}: ⚠️ {meldung}\n{weiter}")
    elif jede:
        telegram(f"…{SUFFIX}: 📶 {meldung}\n{weiter}")


# --------------------------------------------------------------------------- #
# MQTT mit Home-Assistant-Discovery
# --------------------------------------------------------------------------- #
class HaMqtt:
    def __init__(self):
        self.client = None
        self.base = f"at_extender/{SUFFIX}"
        self.uid = f"at_extender_{SUFFIX}"

    def connect(self):
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            log.warning("Kein SUPERVISOR_TOKEN - MQTT deaktiviert.")
            return
        try:
            r = requests.get("http://supervisor/services/mqtt",
                             headers={"Authorization": f"Bearer {token}"}, timeout=10)
            r.raise_for_status()
            cfg = r.json()["data"]
        except Exception as e:
            log.warning(f"Kein MQTT-Broker gefunden ({e}). Läuft ohne HA-Entitäten weiter.")
            return

        import paho.mqtt.client as mqtt
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.uid)
        if cfg.get("username"):
            c.username_pw_set(cfg["username"], cfg.get("password"))
        if cfg.get("ssl"):
            c.tls_set()
        c.will_set(f"{self.base}/availability", "offline", retain=True)
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        c.reconnect_delay_set(min_delay=5, max_delay=120)
        try:
            c.connect(cfg["host"], int(cfg.get("port", 1883)), keepalive=60)
        except Exception as e:
            log.warning(f"MQTT-Verbindung fehlgeschlagen: {e}")
            return
        c.loop_start()
        self.client = c

    # -- Callbacks ---------------------------------------------------------- #
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.warning(f"MQTT-Login abgelehnt: {reason_code}")
            return
        log.info("Mit MQTT verbunden, sende Discovery.")
        self._discovery()
        client.subscribe(f"{self.base}/check/set")
        client.subscribe(f"{self.base}/auto/set")
        client.subscribe(f"{self.base}/monitor/set")
        client.publish(f"{self.base}/availability", "online", retain=True)
        self.publish_state()

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode(errors="ignore").strip().upper()
        if msg.topic.endswith("/check/set"):
            log.info("Prüfung über Home Assistant angefordert.")
            request_check()
        elif msg.topic.endswith("/auto/set") and payload in ("ON", "OFF"):
            STATE["auto_topup"] = payload == "ON"
            save_state()
            log.info(f"Automatisch nachbuchen: {'an' if STATE['auto_topup'] else 'aus'}")
            self.publish_state()
        elif msg.topic.endswith("/monitor/set") and payload in ("ON", "OFF"):
            set_paused(payload == "OFF")
            self.publish_state()

    # -- Discovery ---------------------------------------------------------- #
    def _discovery(self):
        device = {
            "identifiers": [self.uid],
            "name": f"ALDI TALK …{SUFFIX}",
            "manufacturer": "AT-Extender",
            "model": "Datenvolumen-Monitor",
        }
        avail = {"availability_topic": f"{self.base}/availability"}
        st = f"{self.base}/state"
        entities = {
            ("sensor", "datenvolumen"): {
                "name": "Datenvolumen",
                "state_topic": st,
                "value_template": "{{ value_json.gb }}",
                "unit_of_measurement": "GB",
                "device_class": "data_size",
                "state_class": "measurement",
                "suggested_display_precision": 2,
                "icon": "mdi:signal-cellular-3",
            },
            ("sensor", "status"): {
                "name": "Status",
                "state_topic": st,
                "value_template": "{{ value_json.status }}",
                "json_attributes_topic": st,
                "json_attributes_template": "{{ {'meldung': value_json.meldung} | tojson }}",
                "icon": "mdi:information-outline",
            },
            ("sensor", "letzte_pruefung"): {
                "name": "Letzte Prüfung",
                "state_topic": st,
                "value_template": "{{ value_json.last_check }}",
                "device_class": "timestamp",
                "entity_category": "diagnostic",
            },
            ("sensor", "letzte_nachbuchung"): {
                "name": "Letzte Nachbuchung",
                "state_topic": st,
                "value_template": "{{ value_json.last_topup }}",
                "device_class": "timestamp",
                "icon": "mdi:plus-circle-outline",
            },
            ("sensor", "naechste_pruefung"): {
                "name": "Nächste Prüfung",
                "state_topic": st,
                "value_template": "{{ value_json.next_check }}",
                "device_class": "timestamp",
                "entity_category": "diagnostic",
            },
            ("button", "jetzt_pruefen"): {
                "name": "Jetzt prüfen",
                "command_topic": f"{self.base}/check/set",
                "payload_press": "PRESS",
                "icon": "mdi:refresh",
            },
            ("switch", "automatisch_nachbuchen"): {
                "name": "Automatisch nachbuchen",
                "state_topic": st,
                "value_template": "{{ 'ON' if value_json.auto_topup else 'OFF' }}",
                "command_topic": f"{self.base}/auto/set",
                "icon": "mdi:autorenew",
            },
            ("switch", "ueberwachung"): {
                "name": "Überwachung",
                "state_topic": st,
                "value_template": "{{ 'OFF' if value_json.paused else 'ON' }}",
                "command_topic": f"{self.base}/monitor/set",
                "icon": "mdi:radar",
            },
        }
        for (component, key), cfg in entities.items():
            cfg.update(avail)
            cfg["unique_id"] = f"{self.uid}_{key}"
            cfg["default_entity_id"] = f"{component}.aldi_talk_{key}"
            cfg["device"] = device
            self.client.publish(f"homeassistant/{component}/{self.uid}/{key}/config",
                                json.dumps(cfg), retain=True)

    # -- Zustand ------------------------------------------------------------ #
    def publish_state(self, status=None, meldung=None, next_check=None):
        if status is not None:
            STATE["status"] = status
        if meldung is not None:
            STATE["meldung"] = meldung
        if next_check is not None:
            STATE["next_check"] = next_check
        save_state()
        if not self.client:
            return
        gb = STATE.get("last_gb")
        payload = {
            "gb": round(gb, 3) if gb is not None else None,
            "status": STATE.get("status", "gestartet"),
            "meldung": STATE.get("meldung", ""),
            "last_check": STATE.get("last_check"),
            "last_topup": STATE.get("last_topup"),
            "next_check": STATE.get("next_check"),
            "auto_topup": STATE.get("auto_topup", True),
            "paused": STATE.get("paused", False),
        }
        self.client.publish(f"{self.base}/state", json.dumps(payload), retain=True)


HA = HaMqtt()

# --------------------------------------------------------------------------- #
# Browser-Logik (aus G-Chalupa/AT-Extender)
# --------------------------------------------------------------------------- #
def click_by_text(page, text, timeout=5000, retries=5):
    for attempt in range(retries):
        try:
            btn = page.get_by_text(text, exact=True).first
            btn.wait_for(timeout=timeout)
            btn.click()
            return True
        except Exception:
            log.debug(f"Button '{text}' nicht gefunden (Versuch {attempt + 1}/{retries})")
            time.sleep(1)
    return False


def handle_cookie_banner(page):
    try:
        btn = page.query_selector('button[data-testid="uc-deny-all-button"]')
        if btn and btn.is_visible():
            btn.click()
            time.sleep(1)
            log.debug("Cookie-Banner abgelehnt.")
            return
        for btn in page.query_selector_all("button"):
            try:
                text = (btn.text_content() or "").strip().lower()
                if any(k in text for k in ("verweigern", "ablehnen", "decline")) and btn.is_visible():
                    btn.click()
                    time.sleep(1)
                    log.debug(f"Cookie-Banner per Text '{text}' abgelehnt.")
                    return
            except Exception:
                continue
    except Exception as e:
        log.debug(f"Cookie-Banner: {e}")


def goto(page, url, sleep_after=0):
    page.goto(url, wait_until="domcontentloaded")
    if sleep_after:
        time.sleep(sleep_after)
    handle_cookie_banner(page)


def login_erfolgreich(page):
    try:
        page.wait_for_selector('one-heading[level="h1"]', timeout=8000)
        heading = page.text_content('one-heading[level="h1"]')
        return bool(heading and "Übersicht" in heading)
    except Exception:
        return False


def click_login_button(page):
    """Probiert mehrere Wege, den Anmelden-Knopf zu drücken. Gibt den genutzten Weg zurück."""
    name = re.compile(r"^\s*(anmelden|einloggen|login)\s*$", re.I)
    candidates = [
        ("Rolle button 'Anmelden'", lambda: page.get_by_role("button", name=name)),
        ("Text 'Anmelden'", lambda: page.get_by_text(name)),
        ("one-button mit Text", lambda: page.locator("one-button", has_text=name)),
        ("Klasse button--solid", lambda: page.locator(
            '[class="button button--solid button--medium button--color-default button--has-label"]')),
        ("submit-Button", lambda: page.locator('button[type="submit"], input[type="submit"]')),
    ]
    for label, make in candidates:
        try:
            loc = make()
            for i in range(loc.count()):
                el = loc.nth(i)
                if el.is_visible():
                    el.click(timeout=5000)
                    return label
        except Exception as e:
            log.debug(f"Login-Knopf über {label}: {e}")
    # Letzter Ausweg: Enter im Passwortfeld
    try:
        page.press("#input-6", "Enter")
        return "Enter im Passwortfeld"
    except Exception as e:
        log.debug(f"Enter im Passwortfeld: {e}")
    return None


def save_debug(page, tag):
    """Screenshot + HTML nach /share/at_extender, damit man sieht, was das Portal anzeigt."""
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = os.path.join(DEBUG_DIR, f"{ts}-{tag}")
        page.screenshot(path=base + ".png", full_page=True)
        with open(base + ".html", "w", encoding="utf-8") as f:
            f.write(page.content())
        # nur die letzten 10 Paare behalten
        files = sorted(os.listdir(DEBUG_DIR))
        for old in files[:-20]:
            os.remove(os.path.join(DEBUG_DIR, old))
        log.warning(f"Debug gespeichert: {base}.png / .html (Samba: share\\at_extender)")
    except Exception as e:
        log.debug(f"Debug-Speichern fehlgeschlagen: {e}")


def do_login(page, context):
    log.info("Login wird durchgeführt...")
    goto(page, LOGIN_URL, sleep_after=2)
    try:
        page.wait_for_selector("#input-5", timeout=15000)
    except Exception:
        save_debug(page, "login-formular")
        raise Exception("Login-Formular nicht gefunden.")
    page.fill("#input-5", RUFNUMMER)
    page.fill("#input-6", PASSWORT)
    time.sleep(1)
    way = click_login_button(page)
    if not way:
        save_debug(page, "login-knopf")
        raise Exception("Login-Button nicht gefunden.")
    log.info(f"Login abgeschickt ({way}).")
    try:
        page.wait_for_url(lambda u: "login" not in u, timeout=20000)
    except Exception:
        pass
    time.sleep(4)
    if "uebersicht" not in page.url:
        goto(page, DASHBOARD_URL, sleep_after=3)
    if not login_erfolgreich(page):
        save_debug(page, "nach-login")
        raise Exception("Login fehlgeschlagen - Übersicht nicht sichtbar. Zugangsdaten prüfen.")
    context.storage_state(path=COOKIE_FILE)
    log.info("Login erfolgreich.")


PLUS_BTN = 'one-button[slot="action"]:has(one-icon[name="plus"])'
VOL_RE = re.compile(r"([\d\.,]+)\s*(GB|MB)", re.I)


def parse_volume(text):
    m = VOL_RE.search(text or "")
    if not m:
        return None
    value, unit = m.groups()
    value = float(value.replace(".", "").replace(",", ".")) if "," in value else float(value)
    return value / 1024 if unit.upper() == "MB" else value


def get_datenvolumen(page):
    """Sucht den Datenvolumen-Kreis und gibt (GB, Kreis-Locator) zurück.

    Aufbau (Stand Okt. 2026):
      one-usage-meter > one-stack > [one-text 'Noch', one-heading '16,5 GB', one-text 'von 25 GB']
                      > one-button[slot=footer] 'Inland'
                      > one-button[slot=action] (+1 GB)
    """
    try:
        page.wait_for_selector("one-usage-meter one-heading", timeout=15000)
    except Exception:
        pass

    meters = page.locator("one-usage-meter")
    found = []
    for i in range(meters.count()):
        m = meters.nth(i)
        try:
            heading = m.locator("one-heading").first.text_content(timeout=2000) or ""
        except Exception:
            continue
        gb = parse_volume(heading)
        if gb is None:
            continue  # z. B. 'Unbegrenzt' Minuten/SMS
        try:
            label = (m.locator('one-button[slot="footer"]').first.text_content(timeout=1000) or "").strip()
        except Exception:
            label = ""
        has_plus = m.locator(PLUS_BTN).count() > 0
        found.append({"gb": gb, "label": label, "plus": has_plus, "loc": m, "raw": heading.strip()})
        log.debug(f"Kreis {i}: {heading.strip()!r} Label={label!r} Plus={has_plus}")

    if not found:
        raise Exception("Datenvolumen nicht gefunden - Portal-Layout evtl. geändert.")

    # 1. Kreis mit +1-GB-Knopf, 2. Label Inland / Inland & EU, 3. erster Kreis mit GB
    pick = (next((f for f in found if f["plus"]), None)
            or next((f for f in found if f["label"] in ("Inland", "Inland & EU")), None)
            or found[0])
    log.debug(f"Verwende Kreis {pick['label'] or '?'}: {pick['raw']}")
    return pick["gb"], pick["loc"]


def check_once():
    """Ein kompletter Durchlauf. Gibt (status, meldung) zurück."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=LAUNCH_ARGS)
        try:
            if os.path.exists(COOKIE_FILE):
                context = browser.new_context(user_agent=USER_AGENT, storage_state=COOKIE_FILE)
            else:
                context = browser.new_context(user_agent=USER_AGENT)
            page = context.new_page()

            goto(page, DASHBOARD_URL, sleep_after=3)
            if "login" in page.url or not login_erfolgreich(page):
                if os.path.exists(COOKIE_FILE):
                    os.remove(COOKIE_FILE)
                do_login(page, context)
            else:
                log.info("Session noch gültig.")
                context.storage_state(path=COOKIE_FILE)

            try:
                gb, meter = get_datenvolumen(page)
            except Exception:
                save_debug(page, "volumen")
                raise
            STATE["last_gb"] = gb
            STATE["last_check"] = now_iso()
            log.info(f"Aktuelles Datenvolumen: {gb:.2f} GB")

            if gb >= SCHWELLE:
                return "ok", f"Noch {gb:.2f} GB übrig."

            if not STATE.get("auto_topup", True):
                msg = f"Nur noch {gb:.2f} GB - automatisches Nachbuchen ist aus."
                log.info(msg)
                return "niedrig", msg

            log.info("Versuche, 1 GB nachzubuchen...")
            btn = meter.locator(PLUS_BTN).first
            if not (btn.count() and btn.is_visible()):
                save_debug(page, "nachbuchen")
                raise Exception("Nachbuch-Button nicht gefunden.")
            btn.scroll_into_view_if_needed()
            btn.click()
            time.sleep(2)
            # Falls ALDI eine Bestätigung verlangt
            for txt in ("Jetzt nachbuchen", "Nachbuchen", "Bestätigen", "Ja"):
                try:
                    c = page.get_by_role("button", name=txt, exact=True)
                    if c.count() and c.first.is_visible():
                        c.first.click()
                        log.info(f"Bestätigung '{txt}' geklickt.")
                        break
                except Exception:
                    pass
            STATE["last_topup"] = now_iso()

            # Gegenprüfung: Seite neu laden und Volumen erneut lesen
            time.sleep(6)
            try:
                goto(page, DASHBOARD_URL, sleep_after=4)
                gb_neu, _ = get_datenvolumen(page)
                STATE["last_gb"] = gb_neu
                if gb_neu > gb + 0.5:
                    return "nachgebucht", f"1 GB nachgebucht: {gb:.2f} → {gb_neu:.2f} GB."
                return "nachgebucht_unbestaetigt", (
                    f"Button geklickt, Volumen danach {gb_neu:.2f} GB (evtl. noch nicht aktualisiert).")
            except Exception:
                return "nachgebucht_unbestaetigt", f"Button bei {gb:.2f} GB geklickt, Gegenprüfung fehlgeschlagen."
        finally:
            browser.close()


# --------------------------------------------------------------------------- #
# Intervall
# --------------------------------------------------------------------------- #
def next_interval():
    mode = OPT.get("intervall_modus", "smart")
    if mode == "fest":
        return int(OPT.get("intervall_fest", 300))
    if mode == "zufall":
        lo, hi = int(OPT.get("intervall_min", 300)), int(OPT.get("intervall_max", 500))
        return random.randint(min(lo, hi), max(lo, hi))
    gb = STATE.get("last_gb")
    if gb is None:
        return 300
    for limit, rng in ((10, (3600, 5400)), (5, (900, 1800)), (3, (600, 900)),
                       (2, (300, 450)), (SCHWELLE + 0.2, (150, 240)), (SCHWELLE, (60, 90))):
        if gb >= limit:
            return random.randint(*rng)
    return 60


# --------------------------------------------------------------------------- #
# Hauptschleife
# --------------------------------------------------------------------------- #
def main():
    if not RUFNUMMER or not PASSWORT:
        log.error("Rufnummer und Passwort im Reiter 'Konfiguration' eintragen und Add-on neu starten.")
        while True:
            time.sleep(3600)

    log.info(f"AT-Extender gestartet (Rufnummer …{SUFFIX}, Schwelle {SCHWELLE} GB).")
    HA.connect()
    if TG_ON:
        TelegramBot().start()
    elif OPT.get("telegram_aktiv"):
        log.warning("Telegram aktiv, aber Bot-Token oder Chat-ID fehlt.")

    while True:
        WAKE.clear()
        if STATE.get("paused") and not MANUAL.is_set():
            STATE["next_check"] = None
            HA.publish_state(status="gestoppt", meldung="Überwachung gestoppt.")
            log.info("Überwachung gestoppt - warte auf Start oder manuelle Prüfung.")
            WAKE.wait()
            continue
        MANUAL.clear()

        status, meldung = "fehler", ""
        for attempt in range(1, 4):
            try:
                status, meldung = check_once()
                break
            except Exception as e:
                meldung = str(e)
                log.error(f"Versuch {attempt}/3 fehlgeschlagen: {e}")
                time.sleep(5)

        if STATE.get("paused"):
            STATE["next_check"] = None
            HA.publish_state(status=status, meldung=meldung)
            notify_result(status, meldung)
            log.info(f"Status: {status}. Überwachung gestoppt.")
            WAKE.wait()  # Ergebnis stehen lassen, bis Start/Prüfen/Stopp kommt
            continue

        interval = next_interval()
        next_ts = datetime.fromtimestamp(time.time() + interval, timezone.utc).isoformat(timespec="seconds")
        HA.publish_state(status=status, meldung=meldung, next_check=next_ts)
        notify_result(status, meldung)
        log.info(f"Status: {status}. Nächste Prüfung in {interval} s.")

        if WAKE.wait(timeout=interval):
            log.info("Vorzeitig aufgeweckt (Prüfen/Start/Stopp).")


if __name__ == "__main__":
    main()
