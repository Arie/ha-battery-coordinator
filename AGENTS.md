# Battery Coordinator

Coordinates a Zendure 2400 AC battery with N HomeWizard Plug-in Batteries (PIBs) for zero-grid metering. Ships as a Home Assistant add-on; can also run as a standalone container.

## Goal

Minimize grid import/export by coordinating multiple battery systems with solar panels. The core constraint: PIBs are autonomous (can't be directly setpoint-controlled tick-by-tick), so the coordinator controls only the Zendure and gates PIB participation via the HomeWizard P1 meter's permissions API.

## Hardware

| Device | Power | Capacity | Control |
|--------|-------|----------|---------|
| Zendure 2400 AC | 2400W charge/discharge | 8.16 kWh | Local REST API (RAM-only writes for charge/discharge, flash for standby) |
| HomeWizard PIB × N | 800W each | 2.7 kWh each | Autonomous zero-metering. Mode (`zero` / `standby` / `to_full`) and permissions (`charge_allowed` / `discharge_allowed`) via P1 meter API v2 |
| HomeWizard P1 meter | – | – | Reads grid power. Hosts the PIB control API |
| Solar (optional) | – | – | Read-only via HA sensor |

### Key hardware behaviors

- **Zendure relay**: Switching between charge and discharge causes a physical relay click. Minimize these.
- **Zendure response lag**: Commands take ~5 seconds to take effect.
- **Zendure standby**: `smartMode: 0` persists to flash (deep-sleep across reboots). `smartMode: 1` with both limits at 0 is RAM-only — used for the brain's heartbeat re-assertion to avoid flash wear.
- **PIB taper (charge)**: 93%: 720W → 100%: 0W (steps).
- **PIB taper (discharge)**: Linear ~60W per SOC% from 11% down.
- **PIB zero-metering**: PIBs independently try to zero P1. They react in 1-2s but can overshoot.

## Architecture

The brain is `PermissionFSM` (`battery-coordinator/app/brains/permission_fsm.py`) — a finite-state machine over seven states.

### States

```
SLEEP ─── all standby
CHARGE ─── PIBs zero-mode (charge-only), Zen at fixed step OR NOM
GRID_CHARGE ─── deliberate grid import (opt-in), Zen at max, PIBs to_full
FORCE_DISCHARGE ─── deliberate export (opt-in), Zen at max discharge
DISCHARGE ─── Zen NOM-tracks P1, PIBs in standby
DISCHARGE_HELP ─── Zen pinned at max, PIBs help discharge
PIB_DISCHARGE ─── Zen off, PIBs discharge alone
```

Transitions are data, defined in a transition table on the brain (state → list of `Transition` objects with guard, holdoff, target state, and PIB action). The FSM checks guards in order and takes the first match whose holdoff has elapsed.

### Stepped vs NOM in CHARGE

The Zendure runs at one of `[0, 200, 400, 800, 1200, 1600, 2000, 2400]` watts in stepped mode while PIBs are the fast P1 tracker. When all PIBs enter taper (or hit 100%), the Zendure switches to its own NOM tracking: `target = zen_power - p1`.

The boundary tick is clamped to `current_step + 400` to avoid a ~1.4kW jump that would overshoot.

### Tariff gates

The brain has no price model — on a dynamic tariff, "zero the meter" is the
wrong objective during a cheap hour. Three booleans on `Reading`
(`inhibit_discharge`, `inhibit_charge`, `force_charge`) carry external
policy in from HA entities; the add-on supplies mechanism, HA automations
supply policy.

`_apply_gates()` runs in `decide()` **before** `_check_transitions()`, because
a gate must be able to override a state whose own table would keep it there
(`DISCHARGE` has no "stop covering load" guard — that is its job).
`_check_transitions()` additionally vetoes any transition whose destination is
inhibited, resetting its holdoff timer, so a transition cannot sit armed
through the gated window and fire the instant it lifts.

Two subtleties worth preserving:

- An inhibit-forced `target=0` bypasses both `_should_send` carve-outs. Those
  suppress `target=0` on readings indistinguishable from the ones gates fire
  on — "Zen discharging while P1 imports" is exactly the EV case — so without
  the bypass the gate would flip state and change nothing.
- The bypass is naturally one-tick: once the gate forces `SLEEP`, `SLEEP` is
  not itself inhibited, so `_apply_gates()` returns `None` on the next tick and
  normal send throttling resumes. This matters because `main.py` maps
  `target=0` in `SLEEP` to a **flash** write; a permanently armed bypass would
  burn flash at 1 Hz for as long as the gate was held. Pinned by
  `TestGateDoesNotSpamTheDevice`.

`GRID_CHARGE` is entered and left only by the gate, so its transition table is
deliberately empty — two mechanisms owning one state would disagree.
`allow_grid_charge` defaults off and `inhibit_charge` outranks `force_charge`.

`FORCE_DISCHARGE` is the same shape, and its empty table is load-bearing for a
second reason: it is what exempts the state from `_discharge_held_off` and the
`DISCHARGE->CHARGE` flip, both of which exist to prevent exactly the export it
performs. `allow_grid_export` is a **separate** flag from `allow_grid_charge` —
one gates import, the other export, and conflating them would let an opt-out of
one rule silently disable the other.

Only the Zendure can export: the PIBs have no power setpoint (`target_power_w`
is read-only) and no discharge-regardless mode, so the ceiling is ~2400W rather
than the fleet's 4000W. During a forced export the PIBs are held to
`discharge_allowed` only — with `charge_allowed` they would read the negative P1
the Zendure creates as surplus and charge off our own output.

### Key safety mechanisms

- **PIB permissions are the cross-charge lock.** When Zen leads discharge, PIBs are set `discharge_allowed` only — they physically cannot charge from Zen surplus.
- **Last-known caching on Zendure read.** A failed HTTP read returns the previous status, not zeros — a network blip won't trigger spurious "Zen drained" transitions.
- **Heartbeat re-assertion.** Brain re-emits `pib_mode/permissions` every 5 min and Zen `target=0` every 30s. Silent drift (failed PUT, external app toggle, firmware reboot) self-heals within a bounded window.
- **Flash vs RAM standby.** In the idle states (`SLEEP`, `PIB_DISCHARGE`, `SETPOINT` at 0) `target=0` goes through `ensure_standby()`: flash standby (`smartMode: 0`, both limits 0), written only when the tick's read shows the device is not already in it. The 5-min idle heartbeat therefore heals drift without a flash write per beat. A RAM zero (`smartMode: 1` + both limits 0) is used for `target=0` in the active states, where the next command is seconds away.
- **PIB permission failures retry next tick.** A failed `set_mode` PUT calls `brain.mark_pib_send_failed()`, which rewinds the heartbeat timer so the next tick re-emits — not after 5 min.
- **Startup detection has a 2s holdoff.** A single noisy first reading can't lock the brain into a wrong state.
- **p1_contradicts requires 3s sustained.** Brief P1 spikes during charge/discharge no longer accelerate step-down.
- **Solar limit fails open: startup, gate off, stale gates, stale P1 (> `P1_STALE_S` = 15s, via `HWP1Meter.last_ok_t`) and SIGTERM all restore 100%; only 0xF001 is written.**
- **DISCHARGE stops rather than holding the pilot floor into an export.** `nom = zen_power - p1` equals `-(house load)` exactly — the `zen_power` term cancels the current setpoint — so it is safe to threshold on. When demand sits below `PILOT_W`, holding the -50W floor pushed the difference to the grid and parked P1 at ~-50W, inside the dead zone above the `DISCHARGE->CHARGE` guard (`p1 < -100W`). No transition could see it, so the brain stayed there indefinitely and bled the pack into the meter. `_discharge_held_off` latches the stop, with `EXPORT_HYST_W` (25W) of hysteresis so a load hovering at the boundary doesn't flip the target every tick. Pinned by `TestNeverFeedsBackToGrid`.

## Network

| Device | API |
|--------|-----|
| Zendure 2400 AC | `http://<zendure_ip>/properties/report` (read), `/properties/write` (write) |
| HomeWizard P1 meter | `https://<p1_ip>/api/measurement` (grid), `/api/batteries` (PIB read/write) |
| Home Assistant (optional) | `http://supervisor/core/api/states/<entity>` (solar + per-PIB SOC/power) |
| SolarEdge inverter (optional) | Writes go through HA, `POST /api/services/number/set_value` on `solar_limit_entity` (0xF001 via `solaredge_modbus_multi`). Never Modbus: the inverter accepts one Modbus TCP client and HA's integration holds it |

PIB SOC is not exposed by the P1 API — it must come from HA's HomeWizard integration.

## Deployment

Primary deployment is as an **HA add-on**. The Supervisor builds the container from `battery-coordinator/Dockerfile`, writes user config to `/data/options.json`, and runs `/run.sh` → `python3 /app/main.py`.

Alternative: standalone Docker via the root `Dockerfile` (uses env vars instead of `/data/options.json`).

## File structure

```
battery-coordinator/
  config.yaml             HA add-on manifest (options schema)
  build.yaml              Per-arch base images
  Dockerfile              Add-on container (Alpine + py3-aiohttp)
  run.sh                  Add-on entry point (bashio → python3 main.py)
  CHANGELOG.md            Per-version notes
  README.md               Add-on user docs
  translations/en.yaml    Add-on form labels/descriptions
  app/
    main.py               Async tick loop, command dispatch
    config.py             Loads /data/options.json or env vars; validates
    device_io.py          Zendure local REST + HW P1 + OptionalHASensor
    coordinator_logic.py  Reading, Decision, PIB taper functions
    curtailer.py          SolarCurtailer: the inverter limit (pure, no I/O)
    curtail_io.py         Limit writes with in-flight tracking; startup/shutdown restore
    brains/
      permission_fsm.py   The brain (transition table, decide(), step logic)

Dockerfile                Standalone Docker (Python slim + pip aiohttp)
docker-compose.yml        Single-service compose for docker-only path
pyproject.toml            uv project: pytest, ruff, dev deps
tests/
  test_brain_overrides.py Tunable kwargs propagate
  test_config.py          Config loader / validator
  test_curtail_io.py      Restore on cancel mid-write, unknown limit, dry run
  test_curtailer.py       SolarCurtailer unit tests (gates, trim, throttle, heartbeat)
  test_curtailment.py     Closed loop: soak first, no sustained export, bounded writes
  test_device_io.py       Stale-read caching, SN retry, OptionalHA staleness + HTTP status
  test_invariants.py      Property-based + scenario tests on the brain
  test_n_pibs.py          Brain handles 1/2/3/4 PIBs
  test_packaging.py       pyproject / config.yaml / CHANGELOG version pin
  test_tariff_gates.py    Inhibit gates, grid charge, flash-wear on held gates
```

## Pre-commit gate

`.github/workflows/ci.yml` runs these four checks on every push to
`master` and every PR. **Run all four locally before each commit** —
the CI will reject the same things, and a red build is more disruptive
than a five-second local check.

```bash
uv run ruff check battery-coordinator/app/ tests/           # Lint
uv run ruff format --check battery-coordinator/app/ tests/  # Format
uv run mypy                                                 # Types
uv run python -m pytest tests/ -q                           # Tests
```

If `ruff format --check` fails, fix it with `uv run ruff format
battery-coordinator/app/ tests/`. The other three need real changes —
don't bypass them with `--no-verify` or by disabling rules in
`pyproject.toml` without justification (rule ignores there carry an
explanatory comment for a reason).

mypy is strict on `battery-coordinator/app/` (every public def
annotated, no `Any` leaks, no bare generic `dict`/`list`). Tests are
intentionally looser — bare `def test_foo():` is fine, but bodies are
still type-checked, so a real bug like `None + int` will surface.

## Development workflow

**Always write a failing test first, then fix it.**

1. Add a test that captures the broken behavior; verify it fails.
2. Fix the code.
3. Run the four pre-commit gate checks above.
4. Commit (one focused change per commit, imperative-mood subject).

## Constants reference

Brain defaults live in `PermissionFSM.DEFAULTS` (canonical), with class constants reading from it. `Config.brain` reads the same dict so the bare `PermissionFSM()` constructor and the addon-options path can't drift.

| Constant | Default | Purpose |
|----------|---------|---------|
| PILOT_W | 50W | Minimum power for "Zen relay engaged" classification |
| ZEN_STEPS | [0, 200, 400, 800, 1200, 1600, 2000, 2400] | Stepped charge levels |
| PIB_HIGH | 1200W | Combined PIB above this → step Zen up |
| PIB_MAXED | 1400W | Combined PIB above this → fast step-up |
| PIB_LOW | 200W | Combined PIB below this → step Zen down |
| PIB_TAPER_CAP | 300W | Per-PIB charge cap below this = in taper zone |
| PIB_CHARGE_DETECT | 100W | Startup: combined PIB > this → adopt CHARGE |
| PIB_DISCHARGE_DETECT | -100W | Startup: combined PIB < this → adopt DISCHARGE |
| STEP_HOLDOFF | 15s | Sustained PIB saturation before stepping |
| STEP_HOLDOFF_FAST | 5s | Faster holdoff when PIBs maxed |
| STEP_DOWN_COOLDOWN | 30s | After step-up, before any step-down |
| WAKE_CHARGE_S | 10s | SLEEP → CHARGE holdoff |
| WAKE_DISCHARGE_S | 30s | SLEEP → DISCHARGE holdoff |
| FLIP_S | 30s | Charge ↔ discharge flip holdoff |
| HELP_ENTER_S | 15s | Zen-maxed → wake PIBs to help |
| HELP_EXIT_S | 15s | Low load → return PIBs to standby |
| PIB_HEARTBEAT_S | 300s | Re-assert pib_mode/permissions every 5 min |
| P1_EXPORT | -100W | P1 below this = exporting (surplus) |
| P1_IMPORT | 200W | P1 above this = importing (deficit) |
| P1_OVER_DISCHARGE | -100W | DISCHARGE_HELP exit signal |
| NOM_DEADBAND | 10W | Min change before sending NOM target |
| ZEN_MAXED_FRAC | 0.95 | Zen at this fraction of max → considered maxed |
| ZEN_HELP_EXIT_FRAC | 0.8 | Total discharge below this → PIBs redundant |
| EXPORT_HYST_W | 25W | Hysteresis on the DISCHARGE hold-off (stop at 50W demand, resume at 75W) |

## Tariff-gate options

| Option | Default | Purpose |
|--------|---------|---------|
| inhibit_discharge_entity | "" | HA on/off entity; on → hold SOC |
| inhibit_charge_entity | "" | HA on/off entity; on → stop charging |
| force_charge_entity | "" | HA on/off entity; on → grid-charge at max |
| allow_grid_charge | false | Opt out of the never-charge-from-grid rule |
| force_discharge_entity | "" | HA on/off entity; on → discharge at max, exporting |
| allow_grid_export | false | Opt out of the never-discharge-into-export rule |

Gate entities are read through `OptionalHABinarySensor`, which wraps the same
retry/staleness contract as `OptionalHASensor` and fails closed to `False`
after 60s — an unreachable HA reverts to pre-feature behaviour and can never
start a grid charge.

## Curtailment options

| Option | Default | Purpose |
|--------|---------|---------|
| curtail_export_entity | "" | HA on/off entity; on → don't export (trim the inverter after the batteries soak) |
| import_pays_entity | "" | HA on/off entity; on → inverter 0%, PIBs `to_full` charge-only |
| solar_limit_entity | "" | HA `number` (0–100%) the add-on writes; alone it only restores 100% |
| solar_rated_w | 6000 | Inverter rated AC power, to turn watts into percent |
| curtail_target_w | -30W | P1 the trim aims at (slight export the PIBs soak) |
| curtail_deadband_w | 30W | ± band around the target with no action |
| curtail_step_pct | 1 | Smallest limit change written |
| curtail_min_write_s | 3s | Minimum time between writes |
| curtail_settle_down_s | 20s | Sustained export + saturation before trimming down (PIB ramp ~20s) |
| curtail_settle_up_s | 5s | Sustained import before trimming up |

The curtailer (`curtailer.py`) is a separate pure controller; `main.py` feeds
it `brain.batteries_saturated(reading)` and performs the writes through
`CurtailIO` (`curtail_io.py`), which also restores 100% at startup and on
shutdown — including after a write cut off mid-call. Saturation =
no battery can take more: the Zendure follows a setpoint, is maxed or at its
SOC ceiling, or `inhibit_charge` is on; and the PIBs are all ≥ 99 %, charging
at ≥ 90 % of their combined cap, or not permitted to charge. Trim-up happens
only while the inverter is held back (output within `HELD_BACK_MARGIN_W` =
150W of the cap), solar reads 0, or the import has lasted `UNHELD_IMPORT_S`
(60s); a step taken only on that fallback restarts its timer, so it is one
step per `UNHELD_IMPORT_S`, not one per settle. Give-back: charge-only PIBs park
P1 at ~0 by soaking, so a low limit never reads as import — while held back,
not saturated and `p1 >= target_w` for `settle_up_s`, the limit rises
`GIVE_BACK_STEP_PCT` (5). Failed writes back off from `min_write_s` doubling to
`MAX_RETRY_S` (60s). In `main.py`, P1 older than `P1_STALE_S` (15s,
`curtail_io.p1_stale`) runs the curtailer as if no gate were on, so a frozen
reading cannot ratchet the limit to 0. While a gate is on the limit is re-asserted every `HEARTBEAT_S` (300s).
In the brain, `inhibit_charge` blocks the import-pays PIB `to_full`, and
without a setpoint the discharge states are blocked while either price gate is
on.
