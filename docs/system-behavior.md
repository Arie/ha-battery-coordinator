# Battery System Observed Behavior

All observations from production testing (2026-03-30 through 2026-04-06) unless noted otherwise.

## Hardware

- **Zendure 2400 AC**: 2400W inverter, 8.16kWh battery (2x AB2000 packs). Serial: HEC4NENCN512642. IP: 192.168.1.116.
- **HomeWizard Plug-in Battery x2**: 800W each, 2.7kWh each. Autonomous zero-metering via P1 meter.
- **HomeWizard P1 meter**: IP 192.168.1.148. Reads grid power (P1) every ~1s. Local API v2 (HTTPS, bearer token).
- **SolarEdge SE6K inverter**: 6kW peak. Reports AC power via HA sensor.
- **Base household load**: ~280W (measured without Zendure idle draw).

## P1 Meter

The P1 meter is the single source of truth for grid power. Positive = importing from grid, negative = exporting to grid.

- **Update rate**: ~1s
- **All batteries and solar affect P1**: P1 = household_load - solar + pib1 + pib2 + zendure (sign convention: battery charging is positive/adds to P1)
- **Accuracy**: appears instantaneous, no smoothing observed

## HomeWizard PIBs (Plug-in Batteries)

### Control modes (via P1 meter local API)

Three modes available via `PUT /api/batteries`:

| Mode | API value | Behavior |
|------|-----------|----------|
| Zero-metering | `"zero"` | Autonomous: reads P1, adjusts power to drive P1 toward 0. Default mode. |
| Full charge | `"to_full"` | Charges at SOC-dependent max rate regardless of P1. Auto-switches to zero when 100%. |
| Standby | `"standby"` | Neither charges nor discharges. Near 0W draw. |

No discharge-only mode exists. Discharge only happens via zero-metering when P1 is positive (household importing).

### Zero-metering behavior

- **Response time**: Reads P1 every ~1s. First adjustment visible after 1-2s.
- **Convergence**: Each PIB takes ~half the P1 gap per adjustment. Two PIBs converge in 2-3 iterations (5-10s total).
- **Deadband**: Holds steady when |P1| < ~20W.
- **No overshoot observed**: Adjustments are incremental (additive to current power), not absolute.
- **SOC balancing**: When two PIBs have different SOCs, they swap load between themselves. One increases, other decreases. Transparent to external observers.

### Charge rate (SOC-dependent taper)

Observed max charge rates:

| SOC | Max charge per PIB |
|-----|-------------------|
| < 93% | 800W |
| 93% | 720W |
| 94% | 600W |
| 95% | 480W |
| 96% | 480W |
| 97% | 240W |
| 98% | 180W |
| 99% | 120W |
| 100% | 0W |

### Discharge rate (SOC-dependent taper)

| SOC | Max discharge per PIB |
|-----|----------------------|
| > 11% | 800W |
| 11% | 660W |
| 10% | 600W |
| 9% | 540W |
| 8% | 480W |
| 7% | 420W |
| 6% | 360W |
| 5% | 300W |
| 4% | 240W |
| 3% | 180W |
| 2% | 120W |
| 1% | 120W |
| 0% | 120W |

### Direction change behavior (CRITICAL)

When a PIB changes direction (charge to discharge or vice versa), the firmware imposes a **~30-second lockout**:

1. PIB decides to flip direction (e.g., from +800W charge to discharge)
2. PIB immediately drops to a small pilot power in the NEW direction
3. Pilot values during lockout: **+8W** (switching to charge), **-34W** (switching to discharge)
4. PIB holds at this pilot for ~30 seconds
5. After lockout, PIB resumes normal zero-metering in the new direction

The lockout timer starts from the PIB's internal decision point, which is ~5s before we observe it on the power sensor (the sensor reading lags the actual power change).

**This is the single most important constraint.** A 10-second transient (e.g., Quooker) causes PIBs to flip direction, triggering 30s of near-zero power. During this lockout, 1600W of battery capacity is unavailable.

### Full charge (to_full) behavior

- Charges at SOC-dependent max rate immediately
- Does NOT react to P1 at all — purely time/capacity based
- When battery reaches 100%, automatically switches to zero mode
- No direction lockout issues since direction never changes
- Power is constant and predictable (depends only on SOC)

### Standby behavior

- Power drops to ~0W within 2-3 seconds
- No observable idle draw
- Wake from standby to zero mode takes ~2-3 seconds

## Zendure 2400 AC

### Control interface

Controlled via REST API through Home Assistant REST commands. Commands sent to Zendure cloud API which relays to the device.

Available commands:

| Command | HA service | Payload | Use |
|---------|-----------|---------|-----|
| Charge (mode switch) | `rest_command.zendure_x_laden` | `{"sn": "...", "inputLimit": watts}` | First charge command (switches relay to charge mode) |
| Charge (adjust) | `rest_command.zendure_x_laden_balanceren` | `{"sn": "...", "inputLimit": watts}` | Adjust charge rate without relay switch |
| Discharge (mode switch) | `rest_command.zendure_x_ontladen` | `{"sn": "...", "outputLimit": watts}` | First discharge command (switches relay to discharge mode) |
| Discharge (adjust) | `rest_command.zendure_x_ontladen_balanceren` | `{"sn": "...", "outputLimit": watts}` | Adjust discharge rate without relay switch |
| Standby | `rest_command.zendure_standby` | `{"sn": "...", "properties": {"smartMode": 0, "outputLimit": 0, "inputLimit": 0}}` | Deep standby (Flash mode) |

### Ramp behavior

- **Ramp rate**: ~500W/s linear. From 0 to 2400W in ~5 seconds.
- **Command latency**: ~1-2s from REST command to start of ramp.
- **Total response time**: ~5-7s from command to target reached.
- **Mid-ramp commands**: New commands overwrite in-flight ramp. The Zendure starts ramping to the new target immediately.

### Relay behavior

The Zendure has a physical relay that switches between charge and discharge modes.

- **Relay click**: Audible click when switching direction (charge ↔ discharge). Causes ~1s power interruption.
- **Relay lifetime**: Limited. Minimizing clicks is a primary design goal.
- **No relay click** when adjusting power within the same direction (e.g., charge 500W → charge 1500W).
- **No relay click** when going to standby from either direction.
- **Relay click** when going from standby to charge/discharge (wake).

### Standby modes

Two levels of standby:

| Mode | smartMode | Power draw | Wake time | Description |
|------|-----------|-----------|-----------|-------------|
| RAM (light standby) | 1 | ~80W | ~2s | Inverter warm, ready to ramp. Default after setting inputLimit/outputLimit to 0. |
| Flash (deep standby) | 0 | ~0W | ~34s (measured 2026-04-06) | Inverter off. Sent via `zendure_standby` command. |

The HA integration has a configurable "standby delay" (5-30 minutes, default 15) that auto-transitions from RAM to Flash after inactivity. Our coordinator bypasses this by sending smartMode=0 directly.

### Idle draw

- **At pilot (50W charge)**: Actual draw is ~78W due to inverter losses. ~37% loss at minimum power.
- **In RAM standby**: ~80W parasitic draw.
- **In Flash standby**: ~0W (confirmed overnight 2026-04-05).

### Thermal throttling

- Inverter temperature sensor available in HA.
- At 62°C inverter temp, derates from 2400W to ~2200W sustained.
- Top unit (inverter + battery1) runs hottest.
- Relevant for sustained high-power operation in summer.

### SOC behavior

- **SOC range**: 0-100% reported. Usable range in coordinator: 10-100%.
- **At 100% SOC**: Cannot charge. Target should be 0 or discharge.
- **At 10% SOC**: Cannot discharge. Target should be 0 or charge.
- **SOC accuracy**: Generally reliable. No significant hysteresis observed.

## Household loads (transient profiles)

Observed transient loads that affect battery coordination:

| Appliance | Power | Duration | Frequency | Pattern |
|-----------|-------|----------|-----------|---------|
| Quooker (boiling water tap) | 2200W | 10s | Every ~33 minutes | Sharp spike, predictable timing |
| Washing machine (heating element) | 2200W | ~10s per cycle | ~6 cycles per wash | Burst pattern during wash cycle |
| Induction cooktop | 1000-3000W | 30s - 5min | Dinner hours (17:00-20:00) | Variable, user-dependent |
| Heat pump (Itho ground source) | ~1000W | 46-80 minutes | 2-3 runs/day | Sustained step change |
| Dishwasher | Variable | Hours | 1-2/day | Long running, lower peak |
| EV charger (Renault, future: Peblar) | ~3600W | Hours | When plugged in | Very sustained |

### Transient impact on PIBs

A 2200W Quooker spike with PIBs at 800W charge:
1. t=0: P1 jumps from ~0 to +2200W
2. t=1-2s: PIBs see P1 positive, start reducing charge
3. t=3-5s: PIBs may cross zero and flip to discharge (-300 to -800W)
4. t=5s: If PIBs flipped → 30s lockout begins. PIBs stuck at -34W each.
5. t=10s: Quooker ends. P1 goes very negative (surplus).
6. t=10-40s: PIBs in lockout, cannot charge. 1600W of capacity unavailable.
7. t=40s: Lockout ends. PIBs resume charging.

**Total impact**: ~30s of reduced solar capture per Quooker event. With ~44 events/day, that's ~22 minutes of reduced capacity.

## Delays summary

| What | Delay | Notes |
|------|-------|-------|
| P1 meter update | ~1s | Effectively real-time |
| PIB reads P1 | ~1s | Reacts to P1 every second |
| PIB convergence | 5-10s | 2-3 iterations at ~50% gap closure each |
| PIB direction lockout | 30s | From internal decision, we see it ~5s later |
| Zendure command latency | 1-2s | REST API round-trip |
| Zendure ramp | ~5s | 500W/s linear to target |
| Zendure total response | 5-7s | Command to target reached |
| Coordinator send interval | 5s (PibHunterZenFixed) / 1s (PibFullZenNOM charge mode) | Minimum time between commands |
| Standby (RAM) | Immediate | ~80W idle draw remains |
| Standby (Flash) wake | ~34s | smartMode=0, measured 2026-04-06: 34s to first power output |

## Feedback loops and constraints

### The fundamental multi-controller problem

Three controllers (PIB1, PIB2, Zendure) all read the same P1 signal. Any change by one controller affects P1, which the others react to. This creates feedback loops:

1. **PIB-PIB feedback**: Both PIBs adjust simultaneously. Each takes ~50% of the gap. Combined: ~100% correction per cycle. Generally stable (slight overshoot possible with 2x 50% = 100%).

2. **PIB-Zendure feedback**: If both PIBs and Zendure track P1, they fight. Zendure changes → P1 changes → PIBs react → P1 changes → Zendure reacts → oscillation.

3. **Ramp feedback**: During Zendure ramp (5s), P1 is in a transient state. Readings during ramp don't reflect steady state. Calculating a new target from ramp-state P1 produces garbage.

### Strategies to avoid feedback

- **Separation of roles**: One controller tracks P1, others hold fixed. No feedback between fixed-rate controllers and the tracker.
- **Separation of timescales**: Fast controller (PIBs, 1s) handles transients. Slow controller (Zendure, 5s) handles baseline. Slow must be much slower than fast to avoid interference.
- **Latching**: Calculate target once from settled readings, hold it. Don't recalculate during ramp.
- **Solar-based formula**: When PIBs are constant (full-charge mode), calculate Zendure target from solar (doesn't feed back) instead of P1 (feeds back).

## Control options summary

| What we control | How | Latency | Granularity |
|----------------|-----|---------|-------------|
| Zendure charge/discharge rate | REST command via HA | 5-7s total | 1W steps |
| Zendure direction (charge/discharge) | Mode switch command | 5-7s + relay click | Binary |
| Zendure standby | smartMode command | Seconds to minutes | On/off |
| PIB mode (zero/charge/standby) | HW P1 local API | 2-3s | Three modes |
| PIB charge/discharge rate | NOT controllable | N/A | Autonomous in zero mode |
| PIB direction | NOT controllable | N/A | Autonomous in zero mode |

### What we CAN'T control

- Individual PIB power levels (only mode: zero/charge/standby)
- PIB direction change lockout (firmware behavior)
- PIB SOC balancing between units (firmware behavior)
- Zendure thermal throttling (hardware protection)
- Zendure relay lifetime (physical constraint)
- P1 meter reading (observation only)
- Solar production (weather-dependent)
- Household load (user behavior)
