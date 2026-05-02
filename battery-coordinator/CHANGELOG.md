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
