# CLAUDE.md – project instructions

## Project

**Roomstat** (domain `roomstat`, repo `igiannakas/ha-roomstat`): a Home Assistant
custom integration (HACS) that runs a proxy thermostat for each Tado X radiator
thermostat (TRV). Feedforward + PI control on an external room sensor, a
one-sided derivative brake, and background auto-tune.

Roomstat started as a fork of
[kinimodb/ha-tadox-proxy](https://github.com/kinimodb/ha-tadox-proxy) (MIT).
The README describes only what Roomstat changes and points to upstream for
everything else. Keep it that way.

## Language

English everywhere: talking to the user, code, comments, commits and docs.

## Working rules

- Make changes on `main`, in the working tree. **Do not commit or push.** The
  owner reviews the changes and commits them with GitHub Desktop.
- No feature branches and no pull requests.
- Before handing over, all tests and the linter must pass:
  `python -m pytest tests/ -q` and `ruff check .`
- End with a ready-to-use commit title: `feat: …`, `fix: …`, `docs: …` or
  `refactor: …`, with `!` for breaking changes (`feat!: …`).

## Architecture

```
HA-free (testable directly):        HA bridge:
parameters.py → regulation.py       climate.py (entity, mixin composition)
parameters.py → autotune.py         ├─ climate_regulation.py (RegulationMixin)
climate_controllers.py              ├─ climate_presets.py (PresetMixin)
                                    ├─ climate_schedule.py (ScheduleMixin)
                                    ├─ climate_summer.py (SummerMixin)
                                    └─ __init__.py (coordinator), config_flow.py
```

Build new logic in the HA-free modules first (`parameters.py`,
`regulation.py`, `autotune.py`, `climate_controllers.py`), then wire it into
the HA modules.

### Key files

- `parameters.py`: all defaults (RegulationConfig, PresetConfig,
  CorrectionTuning, BehaviourConfig, AutotuneConfig).
- `regulation.py`: feedforward + PI engine and the one-sided derivative brake.
- `autotune.py`: background tuning of Kp, Ki and the braking time Td.
- `climate_controllers.py`: window, presence, summer and schedule state
  machines, restart persistence helpers.
- `climate.py`: the ClimateEntity (properties, lifecycle, options).
- `climate_regulation.py`: regulation cycle, rate limiting, TRV commands,
  feeding the auto-tuner.
- `climate_presets.py`: presets, boost timer, window and presence actions.
- `climate_schedule.py`: follows an `input_select` schedule helper, with
  manual overrides that expire.
- `climate_summer.py`: summer lock (5 °C, service calls refused, TRV held).
- `number.py`: preset temperatures. `sensor.py`: boost remaining, schedule
  override remaining, auto-tune sensors. `button.py`: resume schedule, reset
  auto-tune. `binary_sensor.py`: sensor degraded.
- `config_flow.py`: setup and the options flow (sections).
- `card.py` + `www/roomstat-card.js`: the dashboard card
  (`custom:roomstat-card`). Served by `async_setup` and added as a dashboard
  resource (a frontend module when resources are in YAML). It registers only
  after `home-assistant` is defined (scoped registry), and reads
  `boost_duration_min` and `source_entity_id` from the climate attributes.
- `const.py`: DOMAIN, option keys, custom preset names, `safe_float()`.
- `strings.json` + `translations/` (en, de): UI texts. Keep `strings.json`
  and `en.json` identical, and add new keys to `de.json` too.
- `manifest.json`: version and metadata.

## Regulation concepts

Read this before changing `regulation.py` or `parameters.py`.

- **Feedforward** compensates the TRV's placement bias (radiator vs room) every
  cycle: `command = setpoint + (trv_reading − room) + P + I + brake`. It does
  the heavy lifting; PI only corrects what is left.
- **Loop gain is (1 + Kp).** The feedforward cancels the TRV's own reading, so
  the Tado controller acts on `(1 + Kp)·error + I + brake`. Kp = 0 does not
  mean "no proportional action".
- **PI is deliberately small** (defaults Kp 0.8, Ki 0.003). Larger values
  overshoot because of the radiator → room lag.
- **Integral deadband (0.3 °C):** the integral only accumulates near the
  target, and decays outside it, so a cold start never winds it up.
- **Anti-windup:** no integration while the command is saturated, plus the
  deadband decay above. The integral is clamped to ±2 °C.
- **Command clamp 5–25 °C.** Tado X via HomeKit and V3+ reject anything above
  25. Clamp in the regulator, not at send time, so anti-windup stays honest.
- **Gain scheduling** (optional) scales Kp by error size. It adds little with
  Tado, because the valve is already fully open far from the target.
- **One-sided brake:** `−(1 + Kp)·Td·max(0, slope)`, clamped to [−2, 0] °C, on
  a Theil–Sen slope of the room sensor. It only acts while the room is rising
  and can only lower the command, so a falling reading (window, glitch) can
  never add heat. Off when the sensor is degraded. **Never make it
  two-sided.**
- **Rate limit:** default 180 s between commands to save TRV batteries. Don't
  lower the default.

## Auto-tune

Read this before changing `autotune.py` or `AutotuneConfig`.

- The Tado TRV runs its own PI loop and keeps heating for 30–60 min after its
  demand turns negative (the "coast"). That causes the heat-up overshoot.
  Lowering Kp barely helps, because the demand only crosses zero when the
  error does. The brake moves that crossing earlier by Td × slope.
- The tuner learns only from everyday episodes, never from test signals:
  - **heat-ups** (setpoint step ≥ 0.5 °C with the room ≥ 0.4 °C below) give
    the dead time θ (tangent method), the heating rate, the coast and the
    overshoot;
  - **holds** (steady setpoint, valve actually heating) give offset and
    oscillation.

  Heat-ups cut short by the schedule are kept when they carry enough
  information.
- **SIMC is used only for Ti** (= 4(τc + θ), τc = 1.5 θ), as a basis for Ki,
  and as the justification for Td ≈ coast. **SIMC's Kc is not used:** the only
  gain estimate (rate / demand) is self-referential, because the demand is
  proportional to Kc. In simulation it ratcheted Kp down to its floor. Kp moves
  only on evidence: down on oscillation, up on repeated stalls short of the
  target.
- **Safety:**
  - hard bounds, plus bounds relative to the configured values;
  - small steps and dead-bands;
  - rate limits (3 h gentler, 12 h "more heat");
  - two agreeing episodes needed for "more heat";
  - a watchdog rolls back a "more heat" change if overshoot rises or an
    oscillation appears. There is no freeze after a rollback and no blocked
    moves (owner's decision);
  - emergency detune;
  - any tuner exception falls back to the configured values.
- Learned values live **only** in the restore-state extra data (key
  `autotune`). Never write them to the config entry options: that would
  reload the integration.
- All limits live in `AutotuneConfig` (`parameters.py`). `tests/plant_sim.py`
  simulates room + radiator + Tado TRV (with its own PI) + proxy in closed
  loop. Check control or auto-tune changes against it.

## Brand assets

- Home Assistant (2026.3+) loads the integration logo locally from
  `custom_components/roomstat/brand/` (`icon.png` 256×256, `icon@2x.png`
  512×512, `logo.png` 256×256). A `brand/` folder in the repo root is not read.
- The HACS store resolves icons from brands.home-assistant.io, which needs a PR
  to `home-assistant/brands` (`custom_integrations/roomstat/`).

## Tests

```bash
python -m pytest tests/ -q
ruff check .
```

- `tests/ha_harness.py` provides a minimal HA stub, so end-to-end tests run the
  real `RoomstatClimate` code (`test_frost_preset_persistence.py`,
  `test_summer_mode.py`, `test_schedule.py`, `test_autotune_climate.py`). It
  needs Python ≥ 3.11 (`asyncio.timeout`); CI uses 3.12.
- Tests bypass `__init__.py` by loading modules with
  `importlib.util.spec_from_file_location`.

## Versions and releases

- Bump together: `manifest.json` → `"version"`, and the version badge in
  `README.md`.
- Release notes (GitHub release, English, Markdown):

  ```
  ## v{VERSION} – Short title

  ### New
  - …

  ### Fixes
  - …

  ### Breaking changes (if any)
  - What changed and what users need to do

  ### Installation
  Via HACS → Roomstat → Update, then restart Home Assistant.
  ```

## Documentation

- One file: `README.md`. Plain, short English.
- It lists only what Roomstat changes compared with upstream, plus how to
  install and use those changes. Point to the upstream README for everything
  else. No parameter tables.
- When a feature changes, update its line in "What's different".
- Design rationale belongs in code docstrings and in this file, not in extra
  docs.

## Known quirks

- **iOS Companion App:** `ha-entity-picker` crashes in the iOS WebView. It's an
  HA bug, not this integration. Use a browser for the options flow.
