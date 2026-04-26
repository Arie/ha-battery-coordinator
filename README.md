# Battery Coordinator

Coordinates a **Zendure 2400 AC** battery alongside one or more **HomeWizard Plug-in Batteries** (PIBs) connected to a **HomeWizard P1 meter**, so they don't fight each other for the same surplus or load.

It's a finite-state-machine controller that runs once per second, reads grid + battery state, and sends one Zendure command at a time. Production-tested with property-based invariant tests (260+) covering grid-charge prevention, oscillation, taper-zone behaviour, and fleet-wide coordination.

## Before you install — prepare your hardware

The add-on talks to your devices over the LAN. You'll need three things from each side:

### Zendure (one-time)

1. **Disable HEMS** in the Zendure app. The local REST API only responds when HEMS is off — leaving HEMS on means the cloud-side controller stays in charge and the add-on can't reach it. (See the [Zendure-HA-zenSDK README](https://github.com/Gielz1986/Zendure-HA-zenSDK) for screenshots.)
2. **Find the device IP.** Zendure app → your device → **Device Information** → IP address. Pin it to a DHCP reservation in your router so it doesn't move.
3. **Confirm it's reachable.** From any host on the same LAN: `curl http://<zendure_ip>/properties/report` should return JSON with a `sn` field. If it 404s or hangs, HEMS is still on or the firmware is too old.

### HomeWizard P1 meter

1. Open the **HomeWizard app** → your P1 meter → **Settings** → **API**.
2. **Enable Local API** (HTTPS). On v2 firmware (≥6.x) this requires a one-time confirmation tap on the device.
3. **Create a token** from the same screen and copy it. Treat it like a password.
4. **Find the IP** in the same Settings page; reserve it in DHCP.

Reference: [HomeWizard API docs](https://api-documentation.homewizard.com/docs/v2/getting-started).

### HomeWizard PIB SOC entities (Home Assistant)

The HW P1 `/api/batteries` endpoint doesn't expose per-PIB state of charge — that data only flows through HA's HomeWizard integration. **Add the integration first** (Settings → Devices & Services → Add Integration → HomeWizard → enter the P1 IP). The default entity names the integration creates are:

- `sensor.plug_in_battery_state_of_charge` (PIB 1)
- `sensor.plug_in_battery_state_of_charge_2` (PIB 2)
- ... up to `_4`

If your entity IDs differ (renamed devices, multiple P1 meters), update them in the add-on's Configuration tab.

## Install as a Home Assistant Add-on

If you run **HA OS** or **HA Supervised**:

1. Settings → Add-ons → Add-on Store → ⋮ → **Repositories**
2. Add `https://github.com/Arie/ha-battery-coordinator`
3. Install **Battery Coordinator** from the new section
4. Configuration tab — fill in the values you collected above:
   - `zendure_ip` — from Zendure app → Device Information
   - `hw_p1_ip` — from HomeWizard app → Settings → API
   - `hw_p1_token` — from HomeWizard app → Settings → API
   - `solar_entity` (optional) — your HA solar power sensor, used only as a sunrise sanity guard
   - `pib_soc_entities` / `pib_power_entities` — defaults match HA's HomeWizard integration; edit only if your entity IDs differ
5. **Start** the add-on and watch the **Log** tab. The first useful line is `Zendure SN: ...` followed by per-second decisions (`P1 / PIB / Solar / Zen / Zone / Target`).

The add-on auto-generates the form from [`battery-coordinator/config.yaml`](battery-coordinator/config.yaml). All brain tuning constants are exposed with production-tested defaults — leave them alone unless you have a specific reason to change them.

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

- **HEMS must stay disabled** in the Zendure app — re-enabling it locks out the local REST API.
- **Per-PIB SOC** comes from HA's HomeWizard integration (the HW P1 `/api/batteries` endpoint doesn't expose it).
- **LAN-only.** The add-on never talks to Zendure or HomeWizard cloud — your devices and tokens stay local.
