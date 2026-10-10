# Roomstat

[![Tests](https://github.com/igiannakas/ha-roomstat/actions/workflows/tests.yml/badge.svg)](https://github.com/igiannakas/ha-roomstat/actions/workflows/tests.yml)
![Version](https://img.shields.io/badge/version-2.0.0-blue)
![HA](https://img.shields.io/badge/Home%20Assistant-2026.3%2B-41BDF5)

Roomstat heats each room to the temperature measured by a sensor in the room,
not the temperature at the radiator. It works with Tado V3+ and Tado X radiator
thermostats in Home Assistant.

Roomstat is based on
**[Tado X Proxy Thermostat](https://github.com/kinimodb/ha-tadox-proxy)** by
kinimodb. Read the upstream README for what the integration does, how it
regulates and what its settings mean. This page lists only what Roomstat
changes.

## What's different

**Tado V3+**
- **Works with Tado V3+ radiator thermostats**, not just Tado X. Connect them
  through Home Assistant's **HomeKit Device** integration, paired with the Tado
  Internet Bridge. That connection is local and fast, with no daily limit.
- **Commands stay at 25 °C or below**, the highest value V3+ accepts. The
  original sent up to 30 °C. V3+ refused those commands, so the room didn't
  heat.

**Control**
- **Auto-tune.** Learns Kp, Ki and the braking time of each room in the
  background, from everyday heat-ups. Off by default. See [Auto-tune](#auto-tune).
- **Heat-up braking.** Stops heating a little before the room reaches its
  target, so the heat still in the radiator doesn't push it past. It can only
  ever reduce heating.

**Modes**
- **Schedule.** The room follows a helper that your scheduler sets. Manual
  changes win for a while, then the schedule takes over again. See
  [Schedule](#schedule).
- **Summer mode.** One switch locks every room at 5 °C. See
  [Summer mode](#summer-mode).
- **Away and Off stay** until you change them, also when the schedule moves on.
- **Eco is called Night.**
- **Presence** can be an `input_boolean` as well as a binary sensor.
- **Fixes:**
  - the chosen mode survives restarts, reloads and window open/close;
  - a running Boost carries on after a restart;
  - a Boost saved by the window or presence logic comes back as Comfort.

**Dashboard**
- A room card comes with the integration. See [Dashboard card](#dashboard-card).

**Renamed**
- New name and internal ID (`roomstat`). Rooms set up in Tado X Proxy aren't
  taken over; add them again in Roomstat.

**Removed**
- The "Follow physical thermostat" switch and its two settings. If you turn
  the dial on the radiator, the next command from Roomstat overrides it.

## Install

1. In HACS, open **⋮ → Custom repositories**. Add
   `https://github.com/igiannakas/ha-roomstat` as an **Integration**.
2. Download **Roomstat** and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Roomstat**.
4. Pick the radiator thermostat (for V3+, the one from HomeKit Device), a
   temperature sensor in the room, and a name. Add one entry per radiator
   thermostat.

Everything else is under **Configure** on each entry.

## Auto-tune

Turn it on per room: **Configure → Auto-tune → Enable auto-tune**.

- It learns from heat-ups: a target that rises by 0.5 °C or more while the
  room is cooler, for example a schedule going from Night to Comfort.
- The first values appear after the first heat-up, and they settle in about a
  week.
- It changes values a little at a time, stays within safe limits around your
  own values, and undoes any change that makes things worse.
- Sensors per room:
  - Kp, Ki and braking time in use;
  - auto-tune status;
  - what it measured (dead time, coast time, heat-up rate, last overshoot).
- **Reset auto-tune** forgets everything it learned. Changing Kp, Ki or the
  braking time yourself also starts learning again.

You can set the braking time by hand instead, under **Configure → PI
Controller → Heat-up braking time** (0 = off).

## Schedule

**Configure → Schedule → Schedule helper:** an `input_select` that your
scheduler sets to `comfort`, `night`, `away` or `frost_protection`.

- The room switches to whatever the helper says.
- A manual change (preset, temperature or Boost) wins until the next schedule
  change, or until the **Override duration** has passed. 0 means until the
  next schedule change.
- **Resume schedule** (a button, and a preset) ends a manual change straight
  away.

## Summer mode

**Configure → Summer mode switch:** an `input_boolean`. Use the same one for
every room.

- **On:** every room is held at 5 °C. Changes from Home Assistant are refused,
  and a turn of the dial on the radiator is put back.
- **Off:** every room goes back to its schedule, or to Comfort.

## Dashboard card

Add a card, choose **Roomstat room** and pick the thermostat. Or use YAML:

```yaml
type: custom:roomstat-card
entity: climate.living_room_thermostat
```

| Option | What it does |
|---|---|
| `entity` | The Roomstat thermostat (required). |
| `name` | The title. Defaults to the area name. |
| `icon` | The room icon. Defaults to the area icon. |
| `heating_entity` | The valve's heating % sensor, if it isn't found automatically. |

It shows the room and target temperature, the heating %, and what the room is
doing. Buttons: Off, Night, Day, Boost and Schedule.

## License

MIT. The original work is © kinimodb, and the Roomstat changes are
© igiannakas. See [LICENSE](LICENSE).
