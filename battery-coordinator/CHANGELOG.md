## 0.5.0

- Per-tick Zendure charge/discharge writes now include `smartMode: 1` (RAM-only) so the rapid NOM tracking doesn't wear out the inverter's flash. Standby still uses `smartMode: 0` (persist to Flash) so the device can deep-sleep without re-writes. Net: thousands of flash writes per day → a handful at sleep/wake transitions.

## 0.4.2

- Restored full per-tick INFO line (`P1 / PIB / Solar / Zen / target`). State transitions still get their own one-line INFO marker for grep. Sends are tagged inline on the relevant tick.

## 0.4.1

- Quieter logs by default. Per-tick decisions are now `DEBUG` (~86k lines/day → 0 unless `log_level: debug`). `INFO` is reserved for state transitions, sent commands, errors, and a 60-second operational summary (`60s: state=… avg P1=… Zen=… PIBs=… sends=N`). Set `log_level: debug` to get the old per-second view back.

## 0.4.0

- **Breaking cleanup.** Removed 6 legacy brain implementations (PibLeaderZenFollower, ZenLeader, PibHunter, PibFull, ConsecutiveBatteries, PermissionBrain) and all their tests + simulation infrastructure. PermissionFSM is the only brain that ships now.
- `Reading` no longer accepts the legacy `pib1` / `pib2` kwargs or exposes the read-only properties — pass `pibs=[...]` and `pib_socs=[...]` lists. The brain handles arbitrary lengths.
- Logged decisions now show per-PIB power and SOC (e.g. `🔌 +800/+240W (84/97%)`) instead of a single combined value.

## 0.3.1

- Brain now treats each PIB as a first-class unit. `Reading` carries `pibs` and `pib_socs` lists of arbitrary length; transition guards iterate them with `sum(...)` / `all(...)` / `any(...)` instead of hardcoding pib1+pib2. With 3-4 PIBs every unit's taper, SOC, and power flows into the brain.
- Backward-compat properties (`r.pib1`, `r.pib2`, `r.pib1_soc`, `r.pib2_soc`) keep older callers working.

## 0.3.0

- Read per-PIB SOC and power from HA entities (HW P1 `/api/batteries` exposes neither — verified against firmware 6.0305).
- Two new add-on options, `pib_soc_entities` and `pib_power_entities`, defaulted to the names HA's HomeWizard integration uses; up to 4 batteries supported.
- For 3-4 PIBs, the lead unit (PIB 1) stays individual and the rest aggregate into PIB 2 (sum for power, average for SOC) so the brain's per-PIB taper logic keeps working on the lead unit.

## 0.2.3

- Don't kill a Zendure that's charging from genuine surplus when the brain enters CHARGE in stepped mode (previously the deadband would have sent `standby` after the first non-zero command was issued).

## 0.2.2

- Parse Zendure JSON regardless of declared Content-Type (Zendure local API returns JSON with a non-`application/json` content-type, which aiohttp was rejecting).
- Log the actual exception when the Zendure read fails, instead of silently swallowing it.

## 0.2.1

- Use host networking so the add-on can reach the Zendure and HW P1 meter on the LAN.
- Read combined PIB SOC from the HomeWizard P1 `/api/batteries` endpoint instead of hardcoding 50% (which made the brain wake into DISCHARGE on empty batteries).

## 0.2.0

- Rewrote add-on around the production PermissionFSM brain (direct device APIs, no HA polling needed).
- Added `/data/options.json` support so the auto-generated configuration form actually drives the brain.
- All brain tuning constants are now exposed as add-on options.
- Optional solar sensor is read through the supervisor proxy (no manual HA token needed).

## 0.1.0

- Initial add-on stub (legacy zone-based brain). Superseded by 0.2.0.
