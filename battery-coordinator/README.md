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
- Optional: a **solar power** sensor entity in HA — shown in logs for diagnostics; the battery brain doesn't depend on it. Required for solar curtailment (below), where it must be the inverter's own AC output.
- Optional, for solar curtailment: an inverter power-limit `number` entity in HA, e.g. `number.solaredge_se6k_active_power_limit` from [solaredge_modbus_multi](https://github.com/WillCodeForCats/solaredge-modbus-multi).

## Configuration

The Configuration tab has every option documented inline. Required:

- **Zendure IP** — local IP of the Zendure 2400 AC.
- **HW P1 IP** — local IP of the HomeWizard P1 meter.
- **HW P1 token** — bearer token from the HomeWizard app (see above).

Defaulted (edit only if your entity IDs differ):

- **PIB SOC entities** — `sensor.plug_in_battery_state_of_charge`, `_2`, ...
- **PIB power entities** — `sensor.plug_in_battery_power`, `_2`, ...

Up to 4 PIBs supported; each is treated as a first-class unit by the brain.

Optional — dynamic tariffs:

- **Hold-SOC entity** (`inhibit_discharge_entity`) — while on, the batteries stop discharging and hold their charge. Point it at a template sensor that goes on when power is cheap, or while an EV is charging.
- **Stop-charging entity** (`inhibit_charge_entity`) — while on, the batteries stop charging.
- **Grid-charge trigger entity** (`force_charge_entity`) — while on, the batteries charge from the grid at full rate. Requires **Allow grid charging**.
- **Force-discharge trigger entity** (`force_discharge_entity`) — while on, the Zendure discharges at full rate and exports the surplus. Requires **Allow grid export**. Only the Zendure can export (~2400W); the plug-in batteries have no power setpoint and only cover real load.
- **Allow grid export** (`allow_grid_export`) — opts out of the "never discharge into export" rule. Off by default, and separate from **Allow grid charging**: one governs importing, the other exporting.
- **Allow grid charging** (`allow_grid_charge`) — opts out of the "never charge from the grid" safety rule. Off by default. The add-on refuses to start if you enable it without a trigger entity.

Any on/off entity works (`input_boolean`, `binary_sensor`, template). The brain has no price model by design — Home Assistant decides *when*, the add-on handles *how*. All four default to off, which behaves exactly as before. See the main repository README for worked automation examples.

Optional — solar curtailment: see [Solar curtailment](#solar-curtailment) below.

Brain tuning constants are pre-filled with production-tested defaults. Don't change them unless you know what you're doing — they're exposed for diagnostics.

## Solar curtailment

Once net metering (saldering) ends, exporting at a negative feed-in price costs money. Solar curtailment throttles the inverter so the house, the batteries and the car take the solar first, and only what is still left over is thrown away. On the rare quarter-hours where *buying* pays, the panels go to 0% and the plug-in batteries fill from the grid.

As with the tariff gates, the add-on has no price model: two HA on/off entities say *when*, the add-on decides *how*.

| Level | HA condition | What the add-on does |
|---|---|---|
| Normal | both entities off | as before; the limit is restored to 100% once |
| Don't export | `curtail_export_entity` on | batteries and car soak per their normal or optimiser control; the inverter trims only the export that remains |
| Import pays | `import_pays_entity` on | inverter to 0%; PIBs `to_full`, charge only; the Zendure still follows the optimiser setpoint |

Options:

| Option | Default | Purpose |
|---|---|---|
| `curtail_export_entity` | `""` | on → don't export |
| `import_pays_entity` | `""` | on → import pays (implies curtail) |
| `solar_limit_entity` | `""` | the 0–100% inverter limit the add-on writes |
| `solar_rated_w` | 6000 | inverter rated AC power, to turn watts into percent |
| `curtail_target_w` | -30 | grid power the trim aims at (a slight export, see below) |
| `curtail_deadband_w` | 30 | ± band around the target with no action |
| `curtail_step_pct` | 1 | smallest limit change written |
| `curtail_min_write_s` | 3 | minimum seconds between writes |
| `curtail_settle_down_s` | 20 | export must last this long before the limit goes down |
| `curtail_settle_up_s` | 5 | import must last this long before the limit goes up |

Either gate entity requires `solar_limit_entity` and `solar_entity`. `solar_limit_entity` on its own is valid: the add-on then only ever restores 100%.

**How the trim behaves.** While "don't export" is on:

- The limit goes **down** only when the grid exports beyond the band *and* no battery can take more for `curtail_settle_down_s`. "No battery can take more" means the Zendure is following an optimiser setpoint, is at max charge or its SOC ceiling, or is barred by the stop-charging entity; *and* the PIBs are all at 99% or more, already charging at 90% or more of their combined current cap, or not permitted to charge. The 20 s default covers the PIBs' measured ~20 s ramp, so the trim never throttles what they are about to soak.
- The limit goes **up** when the grid imports beyond the band for `curtail_settle_up_s`, regardless of the batteries — but only while the inverter is actually held back by the cap (output within 150 W of it), or the solar sensor reads 0, or the import has lasted 60 s. Raising a cap the sun does not reach changes nothing and only queues up an export burst for when the clouds pass; the 0-W and 60-s cases fail open on a stale sensor or a wrong `solar_rated_w`. The 60-s fallback takes one step per minute of import, not one per `curtail_settle_up_s`, so a long cloud cannot walk the limit back to 100%.
- The limit is also **given back** 5% at a time while the inverter is held back by the cap, the batteries can still take more, and the grid sits at or above the target for `curtail_settle_up_s`. Charge-only PIBs with room soak whatever the panels make, so a limit that is too low — for instance right after an import-pays window left it at 0% — never shows up as import; without this rule it would stay low and throw away what the batteries could have stored.
- Each step is computed from the inverter's **actual output**, not from 100%: at 3000 W of 6000 W with 500 W export the new limit is (3000 − 500) / 6000 = 41.7%, written as 41% (trim-down rounds down, trim-up rounds up). Never above 100.
- While a gate is on the current limit is re-asserted every 5 minutes, in case the inverter rebooted.
- The -30 W target is deliberate. Charge-only PIBs with room left soak that small export, so the grid sits above the target and the trim gives solar back by itself: "soak first, throttle last" comes out of the loop, not just out of the saturation check. The grid settles between about -60 and 0 W.

**Batteries while curtailing.** The plug-in batteries never discharge while the panels are throttled — that would trade stored energy for free solar. Under "don't export" an optimiser setpoint of 0 or more gives them zero mode with charge only (a negative setpoint keeps discharge only). Under "import pays" they get `to_full` with charge only, whatever the setpoint; if the Zendure is exporting they go to standby instead (the cross-charge lock wins), and the stop-charging entity keeps them out of `to_full` altogether. Without an optimiser setpoint the Zendure may not discharge while either gate is on, so it does not cover the very import that pays.

**The register.** The limit entity writes SolarEdge's 0xF001 (`I_Power_Limit`, dynamic power control), which `solaredge_modbus_multi` marks as not saved to the inverter. That is the only register the add-on touches, and only through Home Assistant's `number.set_value` — the add-on never opens a Modbus connection itself (the inverter accepts a single Modbus TCP client, and HA's integration holds it). It writes only on change. Volatility is not confirmed by SolarEdge, so keep an HA automation on `sensor.<inverter>_write_count` (it counts flash-backed writes and must stay 0).

**Fail-open.** Anything unexpected returns the inverter to 100%: add-on start, both gates off, gate entities unavailable or HA unreachable for 60 s (gates read as off), the P1 meter unreadable for 15 s (its last value would otherwise steer the limit), and add-on stop (SIGTERM, best effort). If Home Assistant itself is down nothing can restore 100% — but then nothing else writes the inverter either, and an inverter reboot clears 0xF001. For the add-on-dead case, an HA watchdog automation (limit below 100 while neither gate is on for 2 minutes → set 100) closes the gap.

**Measured response** (SE6K, 2026-10-25, partly cloudy): 100% → 30% moved P1 from -1771 W to +1374 W in the first sample after the HA call returned, and 30% → 100% from +95 W to -2635 W. The HA service call takes ~1 s and the inverter follows within ~1 s. The slow element is the PIBs, which took ~20 s in zero mode to cover the import a step created — which is why the trim is deliberately the slowest loop: it acts on whatever the batteries leave over, after they have settled.

**For a 2027 Day Ahead Optimizer setup** set `tax_refund: false`, energy taxes on production to 0 and the supplier production cost to your feed-in fee, so DAO plans the Zendure and the car into the negative hours. The add-on never overrides DAO's Zendure setpoint.

## Operations

- **Dry run** — runs the brain but never sends commands. Useful for validating the setup before going live.
- **Log level** — `info` shows per-tick decisions and state transitions; `debug` adds even more detail.

The Log tab shows live decisions like:

```
12:09:00  P1: -1354W  🔌 +0/+0W (100/100%)  ☀️ 4098W  🪫 +2215W (84%)  [CHARGE] → charge 2400W (+185)
```

Read it as: P1 grid power, per-PIB power and SOC, solar production, Zendure power and SOC, current state, and the target (with the diff vs current Zen power). State transitions are logged on their own line, e.g. `State: SLEEP → CHARGE`.

An active tariff gate appears after the state: `🔒D` hold-SOC, `🔒C` stop-charging, `⚡F` grid-charge, `⚡D` force-discharge, `🌞X` don't export, `💶I` import pays, `🎯+800` optimiser setpoint. While the inverter is throttled its limit follows the solar figure (`☀️ 3747W ⛔62%`), and every limit write gets its own line, e.g. `SolarEdge limit 100→62% (trim down)` (reasons: startup, gate off, import pays, trim down, trim up, give back, heartbeat, shutdown). Consecutive failed writes are retried at a doubling interval, from `curtail_min_write_s` up to a minute.

```
18:41:02  P1: +2180W  🔌 +0/+0W (78/81%)  ☀️    0W  🪫    +0W (64%)  [SLEEP] [🔒D] → hold (+0)
```

That line says the house is importing 2.2kW and the batteries are deliberately sitting it out.
