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

The brain is `PermissionFSM` (`battery-coordinator/app/brains/permission_fsm.py`) — a finite-state machine over five states.

### States

```
SLEEP ─── all standby
CHARGE ─── PIBs zero-mode (charge-only), Zen at fixed step OR NOM
DISCHARGE ─── Zen NOM-tracks P1, PIBs in standby
DISCHARGE_HELP ─── Zen pinned at max, PIBs help discharge
PIB_DISCHARGE ─── Zen off, PIBs discharge alone
```

Transitions are data, defined in a transition table on the brain (state → list of `Transition` objects with guard, holdoff, target state, and PIB action). The FSM checks guards in order and takes the first match whose holdoff has elapsed.

### Stepped vs NOM in CHARGE

The Zendure runs at one of `[0, 200, 400, 800, 1200, 1600, 2000, 2400]` watts in stepped mode while PIBs are the fast P1 tracker. When all PIBs enter taper (or hit 100%), the Zendure switches to its own NOM tracking: `target = zen_power - p1`.

The boundary tick is clamped to `current_step + 400` to avoid a ~1.4kW jump that would overshoot.

### Key safety mechanisms

- **PIB permissions are the cross-charge lock.** When Zen leads discharge, PIBs are set `discharge_allowed` only — they physically cannot charge from Zen surplus.
- **Last-known caching on Zendure read.** A failed HTTP read returns the previous status, not zeros — a network blip won't trigger spurious "Zen drained" transitions.
- **Heartbeat re-assertion.** Brain re-emits `pib_mode/permissions` every 5 min and Zen `target=0` every 30s. Silent drift (failed PUT, external app toggle, firmware reboot) self-heals within a bounded window.
- **Flash vs RAM standby.** First entry to `target=0` writes flash (`smartMode: 0`). Heartbeat re-assertions write RAM (`smartMode: 1` + both limits 0). Same effective state, no flash wear.
- **PIB permission failures retry next tick.** A failed `set_mode` PUT calls `brain.mark_pib_send_failed()`, which rewinds the heartbeat timer so the next tick re-emits — not after 5 min.
- **Startup detection has a 2s holdoff.** A single noisy first reading can't lock the brain into a wrong state.
- **p1_contradicts requires 3s sustained.** Brief P1 spikes during charge/discharge no longer accelerate step-down.

## Network

| Device | API |
|--------|-----|
| Zendure 2400 AC | `http://<zendure_ip>/properties/report` (read), `/properties/write` (write) |
| HomeWizard P1 meter | `https://<p1_ip>/api/measurement` (grid), `/api/batteries` (PIB read/write) |
| Home Assistant (optional) | `http://supervisor/core/api/states/<entity>` (solar + per-PIB SOC/power) |

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
    brains/
      permission_fsm.py   The brain (transition table, decide(), step logic)

Dockerfile                Standalone Docker (Python slim + pip aiohttp)
docker-compose.yml        Single-service compose for docker-only path
pyproject.toml            uv project: pytest, ruff, dev deps
tests/
  test_brain_overrides.py Tunable kwargs propagate
  test_config.py          Config loader / validator
  test_device_io.py       Stale-read caching, SN retry, OptionalHA staleness + HTTP status
  test_invariants.py      Property-based + scenario tests on the brain
  test_n_pibs.py          Brain handles 1/2/3/4 PIBs
  test_packaging.py       pyproject / config.yaml / CHANGELOG version pin
```

## Testing

```bash
uv run python -m pytest tests/                       # All tests
uv run ruff check battery-coordinator/app/ tests/    # Lint
uv run ruff format --check battery-coordinator/app/ tests/  # Format check
uv run ruff format battery-coordinator/app/ tests/   # Apply format
```

## Development workflow

**Always write a failing test first, then fix it.**

1. Add a test that captures the broken behavior; verify it fails.
2. Fix the code.
3. Run all tests: `uv run python -m pytest tests/`
4. Run lint: `uv run ruff check battery-coordinator/app/ tests/`
5. Run format check: `uv run ruff format --check battery-coordinator/app/ tests/` (or apply with `ruff format`)
6. Commit (one focused change per commit, imperative-mood subject).

## Constants reference

Brain defaults live in `PermissionFSM.DEFAULTS` (canonical), with class constants reading from it. `Config.brain` reads the same dict so the bare `PermissionFSM()` constructor and the addon-options path can't drift.

| Constant | Default | Purpose |
|----------|---------|---------|
| PILOT_W | 50W | Minimum power for "Zen relay engaged" classification |
| ZEN_STEPS | [0, 200, 400, 800, 1200, 1600, 2000, 2400] | Stepped charge levels |
| PIB_HIGH | 1200W | Combined PIB above this → step Zen up |
| PIB_MAXED | 1400W | Combined PIB above this → fast step-up |
| PIB_LOW | 200W | Combined PIB below this → step Zen down |
| PIB_TAPER_CAP | 600W | Per-PIB charge cap below this = in taper zone |
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
