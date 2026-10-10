# CLAUDE.md – Projektanweisungen

## Projekt

**Roomstat** (Domain `roomstat`, Repo `igiannakas/ha-roomstat`) – Home Assistant Custom
Component (HACS), Proxy-Thermostat für Tado X TRVs.
Feedforward + PI-Regelung mit externem Raumsensor.

## Sprache

- Kommunikation mit dem Nutzer: **Deutsch**
- Code, Kommentare, Commits, Doku: **Englisch**

## Architektur

```
HA-frei (direkt testbar):          HA-Bridge:
parameters.py → regulation.py      climate.py (Entity, Mixin-Komposition)
parameters.py → autotune.py
climate_controllers.py             ├─ climate_regulation.py (RegulationMixin)
                                   ├─ climate_presets.py (PresetMixin)
                                   ├─ climate_schedule.py (ScheduleMixin)
                                   ├─ climate_summer.py (SummerMixin)
                                   └─ __init__.py (Coordinator), config_flow.py
```

Neue Features immer erst in den HA-freien Modulen (`parameters.py`, `regulation.py`, `climate_controllers.py` – testbar ohne HA), dann in den HA-Modulen verdrahten.

### Schlüsseldateien

- `parameters.py` – Defaults (RegulationConfig, PresetConfig, CorrectionTuning, BehaviourConfig)
- `regulation.py` – Feedforward + PI Engine + einseitige D-Bremse (HA-unabhängig)
- `autotune.py` – Hintergrund-Selbstoptimierung von Kp/Ki/Td (HA-unabhängig), Design: `docs/pi-autotune.md`
- `climate_controllers.py` – Window/Presence/Summer/Schedule-Zustandsmaschinen, Config-Entry-Migration (HA-unabhängig)
- `climate.py` – HA ClimateEntity: Properties, Lifecycle, Config
- `climate_regulation.py` – RegulationMixin: Regelzyklus, Rate-Limiting, TRV-Kommandos
- `climate_presets.py` – PresetMixin: Preset-Wechsel, Boost-Timer, Window/Presence-Aktionen
- `climate_schedule.py` – ScheduleMixin: Zeitplan folgen (input_select), manuelle Übersteuerung mit Ablauf
- `climate_summer.py` – SummerMixin: Sommermodus-Sperre (5 °C, Service-Calls abgelehnt, TRV-Durchsetzung)
- `number.py` – NumberEntity für Preset-Temperaturen
- `sensor.py` – Boost-Restzeit-, Zeitplan-Übersteuerungs- und Tuning-Sensoren (Kp/Ki/Td in Verwendung, Selbstoptimierungs-Status und -Modell)
- `button.py` – „Zeitplan fortsetzen“- und „Selbstoptimierung zurücksetzen“-Button
- `binary_sensor.py` – Sensor-Degraded-Diagnose
- `config_flow.py` – Setup + Options Flow
- `diagnostics.py` – HA-Diagnostics-Export
- `card.py` + `www/roomstat-card.js` – Dashboard-Karte (`custom:roomstat-card`), per `async_setup` ausgeliefert und als Dashboard-Ressource eingetragen (YAML-Modus: Frontend-Modul); registriert sich erst nach `home-assistant` (Scoped-Registry); liest `boost_duration_min` und `source_entity_id` aus den Climate-Attributen
- `const.py` – DOMAIN, Config-Keys, Custom Preset Names, `safe_float()`
- `strings.json` + `translations/` – UI-Texte (EN + DE)
- `manifest.json` – Version, Metadata

## Tests

```bash
python -m pytest tests/ -v
```

- `tests/plant_sim.py` simuliert Raum + Heizkörper + Tado-TRV (mit eigenem PI) + Proxy im
  geschlossenen Regelkreis; Änderungen an Regelung oder Selbstoptimierung damit prüfen.
- `tests/ha_harness.py` stellt einen minimalen HA-Stub bereit, damit E2E-Tests den echten
  `RoomstatClimate`-Code ausführen (`test_frost_preset_persistence.py`, `test_summer_mode.py`,
  `test_schedule.py`).
  Benötigt Python ≥ 3.11 (`asyncio.timeout`), CI nutzt 3.12.

- Tests umgehen `__init__.py` via `importlib.util.spec_from_file_location` (HA-Abhängigkeit).
- **Vor jedem Commit müssen alle Tests grün sein.**

## Commit-Konventionen

```
feat: …     # Neues Feature
fix: …      # Bugfix
docs: …     # Nur Dokumentation
refactor: … # Code-Umbau ohne Funktionsänderung
```

## Versionierung

Vor jedem Release synchron aktualisieren:
- `manifest.json` → `"version": "x.y.z"`
- `README.md` → Version-Badge URL

## Dokumentation

Geschichtet – je tiefer, desto technischer. Bei Feature-Änderungen die passende
Schicht aktualisieren, nicht pauschal alle.

| Schicht | Datei | Zielgruppe | Tonfall |
|---------|-------|------------|---------|
| 0 | `README.md` | Interessent, oft Laie | Alltagssprache, **keine** Fachbegriffe |
| 1 | `docs/setup.md` | Neuer Nutzer | Klick für Klick, kein Vorwissen |
| 2 | `docs/settings.md` | Nutzer mit Sonderwunsch | Referenz, Begriffe erklärt |
| 3 | `TUNING.md` | Optimierer | Symptom → Maßnahme |
| 4 | `docs/how-it-works.md` | Neugierige, Contributor | Regelungstechnik, Begriffe eingeführt |

**Schicht 0 und 1: kurze Sätze, kein Jargon.** Diese Schichten werden vom Nutzer
maschinell übersetzt (Übersetzer-Links im README-Kopf). Einfaches Englisch ist die
Voraussetzung dafür, dass das brauchbar funktioniert. Es gibt bewusst **keine**
gepflegten Übersetzungen – der Pflegeaufwand steht in keinem Verhältnis.

Neuer Regelungsparameter → Tabelle in Schicht 2, bei Bedarf Symptom in Schicht 3.
Kein Parameter-Kram in die README zurückwandern lassen.

## Git-Branching

| Branch | Zweck |
|--------|-------|
| `main` | Einziger Langzeit-Branch. **Nie direkt pushen** – nur via PR. |
| `claude/*` | Kurzlebige Feature-Branches von Claude Code, basierend auf `main`. |

Nach dem Merge den Feature-Branch löschen. Es gibt keinen `dev`-Branch –
PRs immer gegen `main`.

## Workflow

1. Feature-Branch von `main` erstellen: `claude/<beschreibung>`
2. Implementieren, Tests grün
3. Commit & Push
4. PR gegen `main` erstellen
5. Merge-Anleitung und Release-Notes ausgeben (siehe `/deliver`)

Für Details zu PR-Merging, Releases und Hotfix-Workflows: siehe `CONTRIBUTING.md`.

## Regelungs-Engine

Lies `.claude/docs/regulation-concepts.md` bevor du an `regulation.py` oder `parameters.py` arbeitest.

## Brand-Assets

Lies `.claude/docs/brand-assets.md` bevor du an Logos/Icons arbeitest.

## Bekannte Eigenheiten

- **iOS Companion App**: `ha-entity-picker` crasht im iOS WebView (HA-Bug, nicht unser Code). Workaround: Browser nutzen.
- **Rate Limiting** (180s): Batterieschonung für Tado X TRVs – nicht verkürzen.
