# Battery Coordinator

Coordinates a **Zendure 2400 AC** battery alongside one or more **HomeWizard Plug-in Batteries** (PIBs) connected to a **HomeWizard P1 meter**, so they don't fight each other for the same surplus or load.

## What it does

- Picks one battery to "lead" P1 tracking at a time (no two controllers chasing zero).
- Steps the Zendure setpoint up/down based on PIB saturation, with hysteresis to minimize relay clicks.
- Switches the Zendure to its own NOM mode when the PIBs run out of room (taper / full).
- Uses the HomeWizard PIB **permissions API** (`charge_allowed` / `discharge_allowed`) to keep the Zendure and PIBs from cross-charging each other when only one should be active.
- Does NOT charge from the grid. Does NOT discharge into export.

The full coordination strategy is documented in the [project README](https://github.com/Arie/ha-battery-coordinator#how-it-coordinates-the-batteries).

## What you need

- A **Zendure 2400 AC** with HEMS disabled (so its local REST API is reachable). Find its IP in the Zendure app under *Device Information*.
- A **HomeWizard P1 meter** with a local API token. Enable via HW app → *Settings → Devices → P1 meter → Enable Local API*; press the button on the meter when prompted to issue the token.
- HA's [HomeWizard integration](https://www.home-assistant.io/integrations/homewizard/) installed — that's where per-PIB SOC and power come from (the P1's `/api/batteries` endpoint exposes neither).
- Optional: a **solar power** sensor entity in HA (used only as a sanity guard during the discharge↔charge flip at sunrise).

## Configuration

The Configuration tab has every option documented inline. Required:

- **Zendure IP** — local IP of the Zendure 2400 AC.
- **HW P1 IP** — local IP of the HomeWizard P1 meter.
- **HW P1 token** — bearer token from the HomeWizard app (see above).

Defaulted (edit only if your entity IDs differ):

- **PIB SOC entities** — `sensor.plug_in_battery_state_of_charge`, `_2`, ...
- **PIB power entities** — `sensor.plug_in_battery_power`, `_2`, ...

Up to 4 PIBs supported; each is treated as a first-class unit by the brain.

Brain tuning constants are pre-filled with production-tested defaults. Don't change them unless you know what you're doing — they're exposed for diagnostics.

## Operations

- **Dry run** — runs the brain but never sends commands. Useful for validating the setup before going live.
- **Log level** — `info` shows per-tick decisions and state transitions; `debug` adds even more detail.

The Log tab shows live decisions like:

```
12:09:00  P1: -1354W  🔌 +0/+0W (100/100%)  ☀️ 4098W  🪫 +2215W (84%)  [CHARGE] → charge 2400W (+185)
```

Read it as: P1 grid power, per-PIB power and SOC, solar production, Zendure power and SOC, current state, and the target (with the diff vs current Zen power). State transitions are logged on their own line, e.g. `State: SLEEP → CHARGE`.
