# Dashboard card

The integration ships a card for each room: `custom:tadox-room-card`. It shows
what the room is doing and gives you one-tap mode buttons, with no templates,
card-mod or extra resources.

It is loaded automatically when the integration starts. After installing or
updating, reload the browser tab (or restart the app) once.

## Add it

In a dashboard, choose **Add card → Tado X room**, pick the thermostat, done.

Or in YAML:

```yaml
type: custom:tadox-room-card
entity: climate.living_room_thermostat
```

| Option | Required | What it does |
|---|---|---|
| `entity` | yes | The Tado X Proxy thermostat. |
| `name` | no | Text at the top. Defaults to the room (area) name. |
| `icon` | no | Room icon. Defaults to the area icon. |
| `heating_entity` | no | Sensor with the valve's heating %. Found automatically for Tado X; set it only if the % stays empty. |

In a sections dashboard the card takes half a section by default, so two rooms
sit side by side.

## What it shows

- **Icon colour** — red while the radiator is heating, teal while the room
  follows its schedule, grey otherwise (and when the room is Off or locked by
  summer mode).
- **Red badge** — an open window, or a manual override of the schedule.
- **First line** — room temperature → target, and the valve's heating %.
- **Second line** — what the room is doing: `Schedule · Day`,
  `Override · Night · 42 min`, `Window open`, `Away · nobody home`,
  `Summer lock`, …

## Buttons

Off · Night · Day · Boost · Schedule. The active one is filled with its colour.

- **Boost** asks first, in a dialog that says what will happen
  (for example "Heats to 22 °C for 30 minutes, then goes back to its schedule").
- **Schedule** ends a manual override and follows the schedule again.
- While summer mode is on, the buttons are refused and a message says why.

Long-press the room name or icon to open the thermostat's details.
