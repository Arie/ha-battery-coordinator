# Battery Coordinator: Clean-Sheet Brain Design

Read `docs/system-behavior.md` first — it documents all observed hardware behavior, delays, control options, and constraints from production testing.

## The problem

Three battery systems (1x Zendure 2400W, 2x HomeWizard PIB 800W) share one P1 meter. All three try to zero-meter. They fight each other, cause oscillation, and trigger PIB firmware lockouts (30s stuck after direction change). The Zendure has a relay that clicks on direction changes — minimizing clicks extends hardware life.

## What we control

- **Zendure**: charge/discharge rate (1W granularity, 5-7s response), direction, standby
- **PIBs**: three modes only — `zero` (autonomous zero-metering), `to_full` (charge at max), `standby`
- We CANNOT control individual PIB power levels or direction

## What we've learned (4 brain iterations)

1. **Multiple zero-meters on one P1 = feedback hell.** Any approach where both PIBs and Zendure track P1 causes oscillation.
2. **Separation of roles works.** One entity holds fixed, the other tracks. No feedback.
3. **PIB direction lockout is the dominant constraint.** A 10s transient causes 30s of 1600W capacity loss. Preventing PIBs from flipping direction is worth more than perfect zero-metering.
4. **PIBs in `to_full` mode are perfectly predictable.** Constant charge at SOC-dependent max. No direction flips, no lockout, no oscillation. The Zendure handles all dynamics.
5. **Solar-based formulas avoid feedback loops.** `target = solar - pib_rate - base_load` doesn't feed back through P1, unlike `target = zen - p1`.
6. **Transient absorption by reducing (not reversing) works.** Zendure drops from 1900W to 200W during a Quooker — PIBs see 350W on P1 instead of 2200W, so they reduce charge instead of flipping to discharge.
7. **Latch the target during transients.** Don't let absorbed values overwrite the pre-transient target. Snap back when P1 normalizes.

## Current best scores (sim with realistic transient loads + PIB lockout model)

| Brain | Relay clicks | Cross-charge | Export |
|-------|-------------|-------------|--------|
| PibHunterZenFixed | 602 | 177 Wh | 335 kWh |
| PibFullZenNOM | 78 | 60 Wh | 422 kWh |

PibFullZenNOM has 7x fewer clicks but 87 kWh more export. The export gap is unexplained — same battery capacity should give same total capture.

## Design a new brain

Using the hardware behavior doc and these learnings, design the optimal control strategy. Consider:

- When should PIBs be in `to_full` vs `zero` vs `standby`?
- How should the Zendure calculate its target in each PIB mode?
- How should transients be handled without causing PIB lockout?
- How should the system behave at sunrise, sunset, overnight, and during sustained loads?
- How should SOC limits (PIBs full/empty, Zendure full/empty) affect the strategy?
- What causes the export gap between the two current brains?

The code is at `/Users/ameeldijk/Projects/ha-addon-battery-coordinator/`. Brain logic in `battery-coordinator/app/coordinator_logic.py`. Sim and scoring in `tests/plot_day.py` and `tests/score.py`. Existing brains (`PibLeaderZenFollower`, `PibHunterZenFixed`, `PibFullZenNOM`) are in coordinator_logic.py for reference.

Score with:
```bash
cd /Users/ameeldijk/Projects/ha-addon-battery-coordinator
PYTHONPATH=tests:battery-coordinator/app uv run python -c "
from plot_day import simulate_consecutive
from coordinator_logic import YourBrain
from score import score_simulation
from pathlib import Path
data_dir = Path('tests/data')
months = [[f'2025-{m:02d}-{d}' for d in [14,15,16]] for m in range(1,13)]
available = [g for g in months if all((data_dir/f'{d}.json').exists() for d in g)]
clicks = cross_wh = export_kwh = 0
for group in available:
    paths = [str(data_dir/f'{d}.json') for d in group]
    r = simulate_consecutive(paths, brain_factory=YourBrain)
    s = score_simulation(r)
    clicks += s.relay_clicks; cross_wh += s.cross_charge_w * s.total_hours; export_kwh += s.grid_export_kwh
print(f'clicks={clicks}  cross={cross_wh:.0f}Wh  export={export_kwh:.0f}kWh')
"
```
