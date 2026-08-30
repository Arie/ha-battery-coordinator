## 1.4.0

- Add `force_discharge_entity` and `allow_grid_export`, plus a `FORCE_DISCHARGE` state. While the entity is on the Zendure runs at full discharge regardless of household demand, feeding any surplus to the grid. This is the first gate that switches a safety rule *off* rather than adding one, so it carries its own opt-in — deliberately separate from `allow_grid_charge`, which governs import. Conflating the two would let an opt-out of one rule silently disable the other. Config validation rejects `allow_grid_export` without a trigger entity, matching the grid-charge guard.
- **Only the Zendure can export, so the ceiling is ~2400W, not the fleet's 4000W.** The HomeWizard plug-in batteries expose no power setpoint (`target_power_w` is read-only) and have no discharge-regardless mode, so in `zero` mode they only ever cover real load. They are additionally held to `discharge_allowed` only for the duration: with `charge_allowed` they would read the negative P1 the Zendure is creating as surplus and charge from our own export. The permission is the hardware lock that prevents that, and a test asserts it is never granted in this state.
- `FORCE_DISCHARGE` is gate-owned with an empty transition table, like `GRID_CHARGE`. That is what exempts it from the pilot-floor hold-off and the `DISCHARGE->CHARGE` flip — both of which exist to stop exactly the export this state is for. With `allow_grid_export` off (the default) those guards are untouched and the no-export invariant still holds; a regression test runs the same closed loop with the flag off and asserts nothing is fed back.
- Precedence, mirroring the charge side: `inhibit_discharge` beats `force_discharge` (a stop request always wins), and `force_charge` beats `force_discharge` if both are asserted — resolving a contradiction toward charging rather than slamming the relay the other way. The SOC floor is enforced both on entry and by the existing clamp.

## 1.3.1

- Migrate the HomeWizard PIB calls off the deprecated `standby` and `to_full` modes. HomeWizard's v2 battery API replaces both with permissions and a `charge_to_full` flag, and states permissions "will be the only option in the future". `standby` now goes out as `mode=zero` with `permissions=[]`; `to_full` as `mode=zero` with `charge_to_full=true`. The translation lives in `HWP1Meter.set_mode()` rather than the brain: "standby" is a semantic state there, appearing in eight transitions and in the heartbeat's desired-state tracking, so rewriting those to express a wire format would churn tested logic for no gain. One translation point, one place to change when the API moves again.
- Send `charge_to_full` on every PIB write, not just when entering to-full. The flag is sticky on the device — it persists until something sets it false — so omitting it when leaving GRID_CHARGE would have left the PIBs charging to 100% indefinitely. Verified against a live meter, which reports the field on current firmware.
- Drop any permission passed alongside `standby`. The two are contradictory and the device auto-switches out of standby the moment a permission is added, so sending both would silently produce a live battery instead of an idle one. The brain never does this today; the guard stops a future caller from tripping over it.
- Send `X-Api-Version: 2` on every P1 request. Responses are currently identical without it, but pinning the version stops a future default change from silently altering the shape this client parses.

## 1.3.0

- Add dynamic-tariff gates. The brain is P1-driven and has no price model, so on a dynamic tariff it optimises the wrong thing: an EV charging during a €0.00 market hour looks like any other load, and the batteries drain 13.5 kWh into it that would have been worth far more against the evening peak. Three optional HA on/off entities now gate the brain — `inhibit_discharge_entity` (hold SOC), `inhibit_charge_entity` (stop charging), and `force_charge_entity` (charge from the grid deliberately). Policy stays in Home Assistant automations where price thresholds, EV state, and forecasts can be combined and tuned without redeploying the add-on; the brain only consumes the resulting booleans. Gates are checked ahead of the transition table so they can override any state, including DISCHARGE, whose own table has no "stop covering load" guard. An inhibit-forced standby also bypasses the two `_should_send` carve-outs that suppress `target=0` — one of them fires on exactly the reading this feature exists to stop (Zen discharging while P1 imports), which would have made the gate a silent no-op.
- Add `allow_grid_charge` (default off) and a `GRID_CHARGE` state. Opts out of the "never charge from the grid" hard rule, which is correct under net metering — where a grid-charged kWh can only be resold at a loss — but is a policy choice on a dynamic tariff. Requires an explicit `force_charge_entity`; config validation rejects the flag without one rather than silently doing nothing. In `GRID_CHARGE` the Zendure runs flat out at max charge (P1 carries no signal worth tracking when importing is the point) and the PIBs switch to `to_full`. `inhibit_charge` outranks `force_charge` so a stuck-on automation can never out-argue a stop request.
- **Fix the Zendure parking at a permanent 50W grid export.** In DISCHARGE the target was clamped to `min(-PILOT_W, nom)` — a hard -50W floor. With house demand below that, the surplus went to the grid and P1 settled at ~-50W, which is inside the dead zone above the `DISCHARGE->CHARGE` guard (`p1 < -100W`): no transition could see it and the brain stayed in DISCHARGE indefinitely. Roughly 1.2 kWh/day fed back with the house idle, and — worse — it silently drained the pack to `zen_soc_min` over a few quiet days, defeating the point of holding SOC for the evening peak. Net metering hid the cost; a dynamic tariff with feed-in charges does not. The brain now stops instead of holding the floor, with 25W of hysteresis (`EXPORT_HYST_W`) so a load hovering at the boundary doesn't flip the target every tick. A held-off zero still heartbeats, so drift recovery is unchanged. Found by closing the P1 loop in the test harness — the existing scenario tests fed the brain a fixed P1 that ignored what it had just commanded, so a self-inflicted export was invisible to them.
- Add closed-loop no-export invariants (`TestNeverFeedsBackToGrid`, `TestPIBsNeverFeedBack`). The PIB-led discharge paths were already clean — the HomeWizard zero-controller damps its own overshoot, and sustained export revokes `discharge_allowed` via the CHARGE transition — but nothing pinned that.
- Gate entities fail closed to off: an unreachable HA holds the last-known value for 60s, then reverts to pre-feature behaviour. A grid charge never starts on a reading that could not be confirmed.

## 1.2.10

- Stop sending the PIBs to standby while they're still carrying load. All three DISCHARGE_HELP exits judged raw P1, but in HELP the PIBs run the HomeWizard zero-mode controller whose entire job is to hold raw P1 at zero — so raw P1 measures the PIBs' success, not the load. Every dip below -100W was read as "load dropped", the PIBs were parked, and ~5s later P1 jumped by exactly the power just removed (production 2026-08-03 13:39-13:51: seven full cycles in twelve minutes, each importing ~1kW for the 15s HELP_ENTER holdoff before the PIBs came back). Exits now use `p1 - sum(pibs)` — what P1 would read with the PIBs at standby. The slow HELP_EXIT guard also switched from combined battery *output* (which counted the PIBs' own over-discharge as load, so it never fired while they helped) to actual household demand `abs(zen_power) + p1_without_pibs`; it now does real work when the Zendure is thermally derated.

## 1.2.9

- Detect Zendure stuck at 0W output. After a power cut the inverter can accept commands (API returns success) but produce no output, leaving the brain sending discharge targets into a void while importing from grid. After 60s of sustained 0W output despite active discharge commands, the brain falls back to PIB_DISCHARGE so the PIBs can cover the load (~1600W). The stuck timer accumulates across DISCHARGE↔DISCHARGE_HELP bouncing — the 15s state ping-pong no longer resets detection. Logs `(Zendure unresponsive — falling back to PIBs)` on the state transition.

## 1.2.8

- Fix Zendure staying in RAM (smartMode=1) during SLEEP/PIB_DISCHARGE. The 30s Zen heartbeat was sending `hold_zero` (RAM) every tick, undoing the initial flash standby within 30 seconds. Now the heartbeat dispatch is state-based: SLEEP/PIB_DISCHARGE always send `standby` (smartMode=0, flash deep-sleep), active states (CHARGE etc.) send `hold_zero` (RAM, warm idle). Heartbeat interval extended to 300s for idle states so the device can actually deep-sleep between pokes.

## 1.2.7

- Fix CHARGE→DISCHARGE flip stuck in NOM mode. When NOM is at its floor (PILOT_W) with P1 importing, PIBs in deep taper may briefly absorb 120W on cloud-clear moments — resetting the `pib_abs < 50` holdoff. Brain was stuck at 50W for 16+ minutes importing 600-4800W from grid. Bypass the pib_abs check when in NOM mode (`_charge_was_nom`); P1 importing + step at floor + 30s sustained is sufficient.

## 1.2.6

- Lower PIB_TAPER_CAP from 600 to 300. At 95-96% SOC PIBs still have ~480W capacity each — the brain was entering NOM mode and fighting with the PIBs' own zero-tracking, oscillating targets every 2-3s. Now NOM only fires at 97%+ (per-PIB cap ≤240W) where PIBs are genuinely near their limit.

## 1.2.5

- Step-down jump: symmetric to the existing step-up jump. When PIBs are idle and P1 is heavily importing (>200W for 3s+) in CHARGE, the brain now jumps the step down in one tick (`current_step - P1`) instead of walking one step at a time at 5s/step. At step 1600 with P1=+1600 (EV turned on), jumps to PILOT_W in 1 tick instead of 25s of grid import.

## 1.2.4

- AppArmor profile rewritten from the official HA add-on template. Previous attempts missed s6-overlay v3 paths (`/package/**`, `/command/**`) and the `file,` capability.

## 1.2.1

- Bottom charge step is now PILOT_W (50W) instead of 0W. At 0W the brain sent flash-standby which cleared ac_mode — stepping back up after a passing cloud required a mode-switch relay click. At 50W the brain stays in charge mode (smartMode=1 RAM), so stepping back up is seamless. Saves one relay click per cloud event.

## 1.2.0

- Security hardening: dropped `host_network` (addon is a pure HTTP client — Docker bridge NAT suffices), added AppArmor profile restricting the container to Python execution + config reads + outbound TCP, declared `hassio_role: default` (minimum privilege). Expected HA security rating improvement from ~3 to ~5.

## 1.1.2

- Suppress CHARGE→DISCHARGE flip while Zen is still at a positive step. When a cloud halves solar, the Zendure charge step is itself the cause of grid import — the correct response is stepping down (2400→2000→1600→...→0), not a relay-click flip to discharge. The flip guard now requires `current_step == 0` before firing. At step 0 with sustained import, the flip fires normally (genuine load > solar). Saves one relay click per cloud event.

## 1.1.1

- Filter PIB activation transient out of the DISCHARGE_HELP over-discharge bail. The HW P1 meter's autonomous PIB controller slams from 0 to ~max in one tick when DISCHARGE_HELP unlocks them, briefly overshooting load by ~1.6 kW and pulling P1 negative for 1-2 ticks. Pre-fix, the brain bailed to DISCHARGE on that single-tick spike and re-entered HELP 15s later — observed 22 state bounces in 5 minutes under heavy EV load (production 2026-05-01). Added 3s holdoff on `r.p1 < P1_OVER_DISCHARGE`; real load drops still exit, just 3s later.

## 1.1.0

- Heartbeat PIB mode and Zen flash-standby. The brain used to send `pib_mode` and `target=0` (standby) once on transition and never re-check. A failed PUT, firmware reboot, or external app toggle would silently leave the device in the wrong state for hours. Brain now re-asserts PIB mode every 5 min and Zen standby every 30 s — idempotent commands, drift self-heals within a bounded window.
- All paths into SLEEP now park PIBs in true standby. Previously `DISCHARGE → SLEEP` and `PIB_DISCHARGE → SLEEP` left PIBs in `zero+charge_allowed` ("fast solar capture" at sunrise). The continuous standby-idle draw outweighed the 10-second sunrise headstart by orders of magnitude — sunrise wake now goes through `SLEEP → CHARGE` with the standard `WAKE_CHARGE_S` holdoff.
- `CHARGE → SLEEP` fires when effectively full + P1 idle (`all PIBs ≥99%`, `Zen ≥99%`, `P1 < P1_IMPORT`) instead of requiring strict 100/100. Taper noise kept the strict guard from firing in practice, leaving PIBs awake in zero-mode.
- Added SLEEP startup detection for `PIB_DISCHARGE` (Zen drained + PIBs covering load). Without it, the new heartbeat would force-stop PIBs that were providing the load after a coordinator restart.

## 1.0.1

- Sunrise flip (DISCHARGE → CHARGE) no longer requires a solar sensor — P1 export alone triggers it. Solar entity is now purely informational. Without solar configured, prior versions would dribble ~50W into export at sunrise until the battery drained.

## 1.0.0

Initial release.
