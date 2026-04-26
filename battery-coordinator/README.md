# Battery Coordinator

Coordinates a **Zendure 2400 AC** battery alongside one or more **HomeWizard Plug-in Batteries** (PIBs) connected to a **HomeWizard P1 meter**, so they don't fight each other for the same surplus / load.

## What it does

- Picks one battery to "lead" P1 tracking at a time (no two controllers chasing zero).
- Steps the Zendure setpoint up/down based on PIB saturation, with hysteresis to minimize relay clicks.
- Switches to the PIBs alone when the Zendure is empty / full.
- Uses the HomeWizard PIB **permissions API** (`charge_allowed` / `discharge_allowed`) to keep the Zendure and PIBs from cross-charging each other when only one should be active.
- Does NOT charge from the grid. Does NOT discharge into export.

## What you need

- A **Zendure 2400 AC** with HEMS disabled (so its local REST API is reachable).
- A **HomeWizard P1 meter** with a local API token.
- Optional: a **solar power** sensor entity in HA (used only as a sanity guard during the discharge↔charge flip at sunrise).

## Configuration

The add-on Configuration tab has every option documented inline. Required:

- **Zendure IP** — the local IP of the Zendure 2400 AC.
- **HW P1 IP** — the HomeWizard P1 meter's IP.
- **HW P1 token** — created in the HomeWizard app (Settings → Local API).

Brain tuning constants are pre-filled with production-tested defaults. Don't change them unless you know what you're doing — they're exposed for diagnostics.

## Operations

- **Dry run** — runs the brain but never sends commands. Useful for validating the setup before going live.
- **Log level** — `info` is fine for day-to-day; bump to `debug` when investigating.

The add-on logs its decisions per second to the add-on log. If the Zendure is responding strangely, check those logs first.
