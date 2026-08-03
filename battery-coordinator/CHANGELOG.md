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
