# Battery Coordinator

Coordinates a **Zendure 2400 AC** battery alongside one or more **HomeWizard Plug-in Batteries** (PIBs) connected to a **HomeWizard P1 meter**, so they don't fight each other for the same surplus or load.

It's a finite-state-machine controller that runs once per second, reads grid + battery state, and sends one Zendure command at a time. Production-tested with unit, property-based, and invariant tests covering grid-charge prevention, oscillation, taper-zone behaviour, and fleet-wide coordination.

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

Install HA's [HomeWizard integration](https://www.home-assistant.io/integrations/homewizard/) — the add-on reads per-PIB SOC and power from the entities it creates. With default device names you'll get:

- `sensor.plug_in_battery_state_of_charge` (PIB 1)
- `sensor.plug_in_battery_state_of_charge_2` (PIB 2)
- ... and so on for `_3`, `_4`

If you renamed a PIB, check **Settings → Devices & services → HomeWizard** for the actual entity IDs and put them in the add-on's Configuration tab.

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

- **Never charge from the grid.** Wake-to-charge requires sustained P1 export — if P1 is importing the brain stays out of CHARGE. (Opt-out: `allow_grid_charge`, see below.)
- **Never discharge into export.** When P1 swings to export the brain flips out of DISCHARGE within `FLIP_S` (default 30s) and stops draining the battery into the meter. (Opt-out: `allow_grid_export`, see below.)

Both rules hold whether or not a solar sensor is configured — the brain uses P1 as the sole source of truth.

## Dynamic tariffs

The brain has no price model. It balances the meter, which is the right goal on a fixed tariff and the wrong one on a dynamic contract: an EV charging through a cheap hour looks like any other load, so the batteries empty themselves into it and have nothing left for the expensive evening peak.

Rather than build a price model into a 1 Hz control loop, the add-on takes three optional on/off entities and lets Home Assistant decide. Any entity works — `input_boolean`, `binary_sensor`, or a template sensor comparing your price sensor to a threshold.

| Option | While it is on |
|--------|----------------|
| `inhibit_discharge_entity` | Batteries stop discharging and hold their SOC |
| `inhibit_charge_entity` | Batteries stop charging |
| `force_charge_entity` | Batteries charge from the grid at full rate (needs `allow_grid_charge`) |
| `force_discharge_entity` | Zendure discharges at full rate, exporting the surplus (needs `allow_grid_export`) |
| `setpoint_entity` | Zendure follows a wattage from an external optimiser (needs both opt-ins, see below) |

All default to empty, which is exactly the previous behaviour. Gates are evaluated before the state machine, so they override any state — including `DISCHARGE`, which has no "stop covering load" transition of its own. A gate that is on shows in the log line as `[🔒D]`, `[🔒C]`, `[⚡F]`, `[⚡D]`, or `[🎯+1500]` for a setpoint.

If Home Assistant becomes unreachable, a gate holds its last value for 60 seconds and then reverts to off. A grid charge never starts on a reading the add-on could not confirm.

### Holding SOC while the EV charges

```yaml
# configuration.yaml — on when power is cheap enough that you would rather
# import than spend stored charge.
template:
  - binary_sensor:
      - name: Hold battery SOC
        state: >
          {{ states('sensor.zonneplan_current_quarter_hourly_electricity_tariff')
             | float(9) < 0.10 }}
```

Point `inhibit_discharge_entity` at `binary_sensor.hold_battery_soc`. Add `or is_state('sensor.ev_charger_status', 'charging')` if you never want the house battery paying for the car.

Note the `float(9)` default: if the price sensor is ever unavailable the template falls back to an implausibly high price, so the gate reads *off* and the batteries keep behaving normally. Defaulting to `0` would do the opposite — an unavailable sensor would silently pin the batteries in hold. Check whether your tariff sensor reports the all-in price or the market price before picking a threshold; Zonneplan's quarter-hourly tariff updates every 15 minutes, so the gate re-evaluates on that cadence.

### Charging from the grid when power is cheap

Set `allow_grid_charge: true` **and** point `force_charge_entity` at an entity that is on during the hours you want to buy. This deliberately breaks the "never charge from the grid" rule, so it is off by default and the add-on refuses to start if you enable the flag without a trigger entity.

```yaml
template:
  - binary_sensor:
      - name: Grid charge now
        state: >
          {{ states('sensor.zonneplan_current_quarter_hourly_electricity_tariff')
             | float(9) < 0.02
             and states('sensor.zendure_soc') | float(100) < 80 }}
```

Two different trades hide behind "grid charging", and they do not have the same answer:

- **Import cheap, self-consume at the peak.** Gain is the price spread minus round-trip losses (~10–15%). Net metering is irrelevant here — nothing is exported — so this works both before and after the scheme ends.
- **Import cheap, export later.** Under net metering the exported kWh is credited at the full retail price for that hour — commodity plus the tax and VAT the netting refunds — so this pays the same as self-consumption. It gets *worse* after net metering ends, when export is credited at roughly the market price with no tax refund.

The gate above targets the first. Work out your own numbers before enabling it. `inhibit_charge_entity` always wins over `force_charge_entity`, so a stop request can never be overridden by a stuck automation.

### Forcing an export

`force_discharge_entity` is the mirror of `force_charge_entity`, and it needs its own opt-in — `allow_grid_export`, not `allow_grid_charge`. Those are two different rules: one governs importing to charge, the other exporting to discharge. Enabling one must never imply the other.

**Only the Zendure can export.** The plug-in batteries expose no power setpoint (`target_power_w` is read-only in the HomeWizard API) and have no discharge-regardless mode, so in `zero` mode they only ever cover real household load. The practical ceiling is therefore ~2400W, not the fleet's 4000W:

| House load | Zendure | PIBs | Exported |
|---|---|---|---|
| 7300 W | 2400 W | 1600 W | 0 |
| 2000 W | 2400 W | idle | 400 W |
| 300 W | 2400 W | idle | 2100 W |

For the duration the PIBs are held to `discharge_allowed` only. Granting them `charge_allowed` would let them read the negative P1 the Zendure is creating as solar-like surplus and charge from the export — the batteries funding each other in a circle while the meter runs.

Whether this pays depends on your contract, and the intuition is easy to get backwards. On a dynamic tariff **under net metering**, an exported kWh is credited at the full retail price for that hour — the hourly commodity rate plus the tax and VAT the netting refunds. Selling into a €0.38 evening peak earns €0.38, the same as avoiding an import at that price. Exporting is therefore worth *more* while net metering lasts, not less; once it ends, export is credited at roughly the market rate with no tax refund and possibly minus feed-in costs.

Two things to check against your own contract before relying on it:

- Net metering only nets export against import **up to your annual import volume**. Export beyond that is paid at the supplier's much lower redelivery rate.
- Many suppliers now levy feed-in costs (*terugleverkosten*), fixed or per-kWh, which come straight off this margin.

Note also that under net metering, exporting and self-consuming a stored kWh are worth the same. Forced export only adds value when the battery would otherwise sit idle — full pack, low household load, high price.

### Following an optimiser's setpoint

The four gates are on/off. An optimiser such as [Day Ahead Optimizer](https://github.com/corneel27/day-ahead) produces something richer: a wattage per interval, planned against day-ahead prices, solar forecast and household baseload. `setpoint_entity` is a gate with a magnitude. Point it at a numeric entity in watts — DAO's `entity set power feedin` helper is exactly this — and the add-on follows it:

| Setpoint | Zendure | PIBs |
|---|---|---|
| positive | charges at that wattage | `charge_allowed` only |
| negative | discharges at that wattage | `discharge_allowed` only |
| 0 | standby | zero mode, both permissions |
| unavailable / non-numeric | normal behaviour | normal behaviour |

The optimiser owns *how much*; the add-on owns *how*. Only the Zendure takes a watt setpoint, so it follows the number exactly up to its 2400 W limits. The plug-in batteries cannot, so they get the one permission that stops them fighting it: while the Zendure imports they may not discharge (they would read the import as load and feed the Zendure), and while it exports they may not charge (they would read the export as surplus and charge from it). At 0 they are free to zero-track the house on their own, which is what they are good at.

Precedence, from the top: `inhibit_*` beats the setpoint outright (while a stop request is up the add-on behaves as if no optimiser were speaking), the manual `force_*` toggles beat it (an explicit human toggle beats an optimiser), and it beats the state machine. Values are clamped to `zen_max_charge_w` / `zen_max_discharge_w`, anything below 50 W is a hold, and the SOC floor and ceiling still apply — a stuck negative setpoint stops at `zen_soc_min`.

It needs **both** `allow_grid_charge` and `allow_grid_export`, and the add-on refuses to start with the entity set and either missing: an optimiser buys and sells by nature, and half a job would be silent. With a setpoint entity configured the force entities become optional.

Absence is the fail-safe, and it is also the hand-over switch. Do not point the add-on straight at the optimiser's helper; put a template sensor in between that shows the value only while you want the optimiser in charge:

```yaml
template:
  - sensor:
      - name: Battery setpoint
        unit_of_measurement: W
        state: >
          {% if is_state('input_boolean.dao_battery_control', 'on') %}
            {{ states('input_number.dao_battery_setpoint') }}
          {% else %}unavailable{% endif %}
```

Switch the boolean off and the add-on drops back to plain zero-on-the-meter within a second. The same sensor is where a liveness check belongs — return `unavailable` when the optimiser has not written for a couple of hours — so a dead optimiser cannot leave the fleet parked on its last instruction.

### Never feeding back

The batteries are held to a no-export invariant, tested closed-loop (`TestNeverFeedsBackToGrid`, `TestPIBsNeverFeedBack`): the Zendure stops rather than hold its minimum output into a surplus, and the PIBs lose `discharge_allowed` whenever sustained export appears. Solar itself still exports normally — the guarantee covers battery output only.

## Repository layout

```
battery-coordinator/      The HA add-on (config.yaml, Dockerfile, app/)
  app/
    main.py               Add-on entry point (direct device APIs)
    config.py             Reads /data/options.json or env vars
    device_io.py          Zendure + HW P1 + (optional) HA solar
    brains/permission_fsm.py   The FSM brain
tests/                    Unit + property-based + invariant tests
```

## Development

```bash
uv sync
uv run pytest tests/ -q
```

Run the coordinator standalone (dry-run, no commands sent):

```bash
ZENDURE_IP=192.168.1.x HW_P1_IP=192.168.1.y HW_P1_TOKEN=... \
  DRY_RUN=true uv run python battery-coordinator/app/main.py
```

## Caveats

- **HEMS must stay disabled** in the Zendure app — re-enabling it locks out the local REST API.
- **Per-PIB SOC** comes from HA's HomeWizard integration (the HW P1 `/api/batteries` endpoint doesn't expose it).
- **LAN-only.** The add-on never talks to Zendure or HomeWizard cloud — your devices and tokens stay local.
