# Battery Coordinator

Coordinates a **Zendure 2400 AC** battery alongside one or more **HomeWizard Plug-in Batteries** (PIBs) connected to a **HomeWizard P1 meter**, so they don't fight each other for the same surplus or load.

It's a finite-state-machine controller that runs once per second, reads grid + battery state, and sends one Zendure command at a time. Production-tested with property-based invariant tests (260+) covering grid-charge prevention, oscillation, taper-zone behaviour, and fleet-wide coordination.

## Install as a Home Assistant Add-on

If you run **HA OS** or **HA Supervised**:

1. Settings → Add-ons → Add-on Store → ⋮ → **Repositories**
2. Add `https://github.com/Arie/ha-battery-coordinator`
3. Install **Battery Coordinator** from the new section
4. Configure (Zendure IP, HW P1 IP + token) → **Start**

The add-on auto-generates a configuration form from [`battery-coordinator/config.yaml`](battery-coordinator/config.yaml). All brain tuning constants are exposed as options with production-tested defaults.

## Run as standalone Docker

For **HA Core** / **HA Container** users (no Supervisor available), or running on a separate host:

```bash
cp .env.example .env  # fill in your IPs / tokens
docker compose up -d
```

The container talks directly to the Zendure (local REST) and HomeWizard P1 (local HTTPS) — no Home Assistant required. Optional HA REST API for a solar sensor only.

## What it does

- **One controller on the meter at a time.** Picks Zendure or PIBs to track P1, never both.
- **Stepped Zendure setpoints** in CHARGE mode (0 / 200 / 400 / 800 / 1200 / 1600 / 2000 / 2400 W) with hysteresis to minimize relay clicks.
- **NOM mode** when PIBs hit taper or saturation — Zendure absorbs the residual.
- **PIB permissions API** (`charge_allowed` / `discharge_allowed`) to keep the Zendure and PIBs from cross-charging each other when only one should be active.
- **Never charges from grid.** Never discharges into export.

## Repository layout

```
battery-coordinator/      The HA add-on (config.yaml, Dockerfile, app/)
  app/
    main.py               Add-on entry point (direct device APIs)
    config.py             Reads /data/options.json or env vars
    device_io.py          Zendure + HW P1 + (optional) HA solar
    brains/permission_fsm.py   The FSM brain
coordinator_cli.py        Legacy systemd CLI (HA REST, kept for handover scenarios)
tests/                    260+ unit + property-based + invariant tests
docs/                     Background notes
```

## Development

```bash
uv sync
uv run pytest tests/ -q
```

Run the standalone CLI in dry-run against your live setup:

```bash
HA_URL=http://your-ha:8123 HA_TOKEN=... \
  uv run python coordinator_cli.py
```

## Caveats

- **Per-PIB SOC isn't in the HW P1 API at all.** The add-on reads it from HA entities (`sensor.plug_in_battery_state_of_charge` / `_2` by default — configurable, up to 4 batteries). The HomeWizard integration in HA exposes these for free.
- **HEMS must be disabled** in the Zendure app for the local REST API to respond.
- LAN-only — the add-on talks to your devices on the local network, never to Zendure or HomeWizard cloud.
