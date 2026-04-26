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

1. Open the **HomeWizard app** → **Settings** → **Devices** → your **P1 meter** → **Enable Local API**.
2. On v2 firmware (≥6.x) the app will prompt you to **press the button on the P1 meter** within 30 seconds. After the press the app issues a bearer token — copy it and treat it like a password.
3. Note the IP from the same screen; reserve it in DHCP so it stays put.

Reference: [HomeWizard API docs](https://api-documentation.homewizard.com/docs/v2/authorization).

### HomeWizard PIB SOC entities (Home Assistant)

The HW P1 `/api/batteries` endpoint doesn't expose per-PIB state of charge — that data only flows through HA's [HomeWizard integration](https://www.home-assistant.io/integrations/homewizard/). **Add the integration first** (Settings → Devices & services → Add Integration → HomeWizard). The integration auto-discovers HW devices on your LAN; if discovery doesn't find them you can enter the P1's IP manually.

For each PIB the integration creates a `Plug-In Battery` device with sensors. With default device names you'll get:

- `sensor.plug_in_battery_state_of_charge` (PIB 1)
- `sensor.plug_in_battery_state_of_charge_2` (PIB 2)
- ... and so on for `_3`, `_4`

If you renamed a PIB device, the entity ID will reflect the new name. Check **Settings → Devices & services → HomeWizard** to confirm the actual IDs and put them in the add-on's Configuration tab.

## Install as a Home Assistant Add-on

If you run **HA OS** or **HA Supervised**:

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories** (older HA UIs) or **Settings → Apps → App Store → ⋮ → Repositories** (newer UIs after the rename).
2. Add `https://github.com/Arie/ha-battery-coordinator` and close the dialog.
3. Refresh the store, find **Battery Coordinator**, and **Install**.
4. Configuration tab — fill in the values you collected above:
   - `zendure_ip` — from Zendure app → Device Information
   - `hw_p1_ip` — from HomeWizard app → Settings → Devices → P1 meter
   - `hw_p1_token` — the token issued after the button-press step
   - `solar_entity` (optional) — your HA solar power sensor. Purely informational; logged for diagnostics. Safe to leave empty.
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

## How it coordinates the batteries

The PIBs and the Zendure both want to balance the grid meter to zero ("NOM" — net-on-meter). If both react at the same time they fight: the Zendure overshoots, the PIBs correct, the Zendure backs off, repeat. The brain solves this by giving exactly one device the role of "fast P1 tracker" at any moment, and using the other as a slower predictable bias.

### Charging from solar surplus

The Zendure runs at one of a few **fixed power steps**: `0 / 200 / 400 / 800 / 1200 / 1600 / 2000 / 2400 W`. While it sits on a step it acts as a constant load — quiet, predictable. The PIBs do the actual NOM tracking against P1 on top of that baseline. The HW PIBs are the right unit for this job: they react in milliseconds, rarely click their relays, and don't mind hunting around zero.

The brain watches how hard the PIBs are working and shifts the Zendure step accordingly:

- **PIBs near their hardware max** (combined > ~1200W, or saturating relative to their current charge cap when one is in taper) → step the Zendure **up** so the PIBs have headroom again.
- **PIBs nearly idle** (combined < ~200W) → step the Zendure **down** so the PIBs aren't getting starved by an oversized Zendure setpoint.

Step changes have hysteresis (~15s sustained signal) so a passing cloud or appliance cycle doesn't trigger relay clicks.

### When the PIBs run out of room

PIBs lose charge capacity above ~93% SOC ("taper zone": 720 W → 600 → 480 → 240 → 180 → 120 W as SOC climbs to 100%). At that point the stepped scheme stops working — the PIBs can't absorb whatever the Zendure isn't taking. The brain detects this and **switches the Zendure into its own NOM mode**, where it tracks P1 directly: `Zen target = current_zen_power − P1`. The PIBs hold zero. The Zendure is now the fast tracker until either solar drops or the PIBs come out of taper.

### Discharging at night

Mirror image. The Zendure is the fast NOM tracker; PIBs sit in standby. When the Zendure hits its max discharge AND P1 is still importing, the brain wakes the PIBs to **help discharge** (Zen pinned at max, PIBs absorb the residual). When the Zendure runs empty, PIBs take over solo.

### Cross-charge prevention

The HW PIB **permissions API** (`charge_allowed` / `discharge_allowed`) is the lock: when the Zendure is leading discharge, PIBs are set `discharge_allowed` only — they physically cannot charge from the Zendure even if the firmware would otherwise want to.

### Hard rules

- **Never charge from the grid.** Wake-to-charge requires sustained P1 export — if P1 is importing the brain stays out of CHARGE.
- **Never discharge into export.** When P1 swings to export the brain flips out of DISCHARGE within `FLIP_S` (default 30s) and stops draining the battery into the meter.

Both rules hold whether or not a solar sensor is configured — the brain uses P1 as the sole source of truth.

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
