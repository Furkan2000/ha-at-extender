# AT-Extender (Home-Assistant-Add-on)

Prüft regelmäßig das Restvolumen deines ALDI-TALK-Tarifs im Kundenportal und bucht unter einer
Schwelle automatisch 1 GB nach. Die Werte erscheinen per MQTT als Gerät **„ALDI TALK …1234“** in
Home Assistant.

> ⚠️ Laut ALDI-TALK-Leistungsbeschreibung (Punkt 10.3 g) ist automatisiertes Nachbuchen untersagt
> und kann zur Kündigung führen. Nutzung auf eigenes Risiko.

## Voraussetzungen

- Home Assistant OS (z. B. als VM unter Proxmox), amd64 oder aarch64
- Add-on **Mosquitto broker** installiert und Integration **MQTT** eingerichtet
- Der VM mindestens 3–4 GB RAM geben; Chromium braucht beim Lauf ein paar hundert MB
- Etwa 1,5 GB freier Speicher für das Image (Chromium + Abhängigkeiten)

## Installation

1. Zugriff auf den Ordner `/addons` einrichten, z. B. mit dem Add-on **Samba share**
   (in Windows: `\\homeassistant\addons`) oder **Advanced SSH & Web Terminal**.
2. Den Ordner `at_extender` dorthin kopieren, sodass `/addons/at_extender/config.yaml` existiert.
3. **Einstellungen → Add-ons → Add-on-Store** → oben rechts ⋮ → **Nach Updates suchen**.
4. Unter **Lokale Add-ons** erscheint **AT-Extender** → **Installieren**.
   Das Image wird dabei lokal gebaut, das dauert ein paar Minuten.
5. Im Reiter **Konfiguration** Rufnummer und Passwort eintragen → Speichern → **Starten**.
6. Im Reiter **Protokoll** sollte nach ca. 20–30 Sekunden `Aktuelles Datenvolumen: … GB` stehen.

## Entitäten

| Entität | Bedeutung |
|---|---|
| `sensor.aldi_talk_datenvolumen` | Restvolumen in GB |
| `sensor.aldi_talk_status` | `ok`, `niedrig`, `nachgebucht`, `nachgebucht_unbestaetigt`, `fehler`, `gestoppt` (Attribut `meldung` mit Details) |
| `sensor.aldi_talk_letzte_pruefung` | Zeitpunkt der letzten erfolgreichen Prüfung |
| `sensor.aldi_talk_letzte_nachbuchung` | Zeitpunkt der letzten Nachbuchung |
| `sensor.aldi_talk_naechste_pruefung` | Wann das Add-on das nächste Mal prüft |
| `button.aldi_talk_jetzt_pruefen` | Sofort prüfen (statt auf das Intervall zu warten) |
| `switch.aldi_talk_automatisch_nachbuchen` | Nachbuchen an/aus; ist es aus, wird nur gemeldet (`niedrig`) |
| `switch.aldi_talk_ueberwachung` | Regelmäßige Prüfung an/aus (gleich wie Start/Stopp im Telegram-Menü). „Jetzt prüfen" geht auch, wenn sie aus ist. |

## Optionen

| Option | Standard | Beschreibung |
|---|---|---|
| `schwelle_gb` | 1.0 | Unter diesem Wert wird nachgebucht. ALDI zeigt den Plus-Button meist erst unter 1 GB. |
| `automatisch_nachbuchen` | an | Startwert für den Schalter |
| `intervall_modus` | smart | `smart`: viel Volumen → selten prüfen (bis 90 min), knapp über der Schwelle → alle 1–1,5 min. `fest`: immer `intervall_fest`. `zufall`: zwischen `intervall_min` und `intervall_max`. |
| `telegram_aktiv`, `telegram_bot_token`, `telegram_chat_id` | aus | Telegram-Meldungen und Menü (siehe unten) |
| `telegram_jede_pruefung` | an | Nach jeder Prüfung das Restvolumen schicken. Aus = nur bei Fehler, wenig Volumen oder Nachbuchung. |

## Telegram

Mit `telegram_aktiv` schickt der Bot nach jeder Prüfung eine Nachricht, z. B.
`…7366: 📶 Noch 11.90 GB übrig. Nächste Prüfung 12:17.`

**Menü:** Unter dem Eingabefeld im Bot-Chat sind immer die Knöpfe
**⏸ Überwachung stoppen** / **▶️ Überwachung starten**, **🔄 Jetzt prüfen** und **ℹ️ Status** zu sehen
(erscheinen mit der ersten Nachricht des Bots). Sonstiger Text zeigt ebenfalls den Status.
Gestoppt bleibt gestoppt, auch nach einem Neustart des Add-ons. Der Bot reagiert nur auf den Chat aus `telegram_chat_id`.

Der Bot darf nirgends sonst abgefragt werden (kein Webhook, nicht zusätzlich in der HA-Integration „Telegram bot"
im Polling-Modus), sonst kommen die Menü-Eingaben nicht an (Log: „Bot wird woanders abgefragt").

## Beispiel-Automation (Push auf das Handy)

```yaml
alias: ALDI TALK Meldung
triggers:
  - trigger: state
    entity_id: sensor.aldi_talk_status
    to:
      - nachgebucht
      - nachgebucht_unbestaetigt
      - fehler
      - niedrig
actions:
  - action: notify.mobile_app_DEIN_HANDY
    data:
      title: ALDI TALK
      message: "{{ state_attr('sensor.aldi_talk_status', 'meldung') }}"
mode: single
```

## Wenn etwas nicht klappt

- **`Login fehlgeschlagen`**: Zugangsdaten im Browser auf
  `alditalk-kundenportal.de` prüfen. Gespeicherte Session löschen: Add-on stoppen und neu starten
  (ungültige Cookies werden automatisch verworfen).
- **`Datenvolumen nicht gefunden`** / **`Nachbuch-Button nicht gefunden`**: ALDI hat das Portal-Layout
  geändert. Die Selektoren stehen in `run.py` (`get_datenvolumen`, `check_once`); nach einer Änderung
  im Add-on auf **Neu bauen** klicken.
- **Keine Entitäten in HA**: Mosquitto-Add-on läuft? MQTT-Integration eingerichtet? Im Protokoll muss
  `Mit MQTT verbunden` stehen.
- Mehr Details: Option `log_level` auf `debug` stellen.

Browser-Logik basiert auf [G-Chalupa/AT-Extender](https://github.com/G-Chalupa/AT-Extender).
Ein Auto-Update gibt es bewusst nicht.
