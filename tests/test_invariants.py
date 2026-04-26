"""Property-based invariant tests for all brains.

Generate random sequences of sensor readings and verify that fundamental
invariants hold regardless of the scenario. Catches edge cases that
specific test scenarios miss.
"""

import sys
import random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "battery-coordinator" / "app"))

from coordinator_logic import Reading
from brains.permission_fsm import PermissionFSM


def _random_reading(rng, zen_soc=50, pib1_soc=50, pib2_soc=50):
    """Generate a plausible random sensor reading."""
    solar = rng.choice([0, 0, 0, 200, 500, 1000, 2000, 3000, 4000, 5000])
    p1 = rng.randint(-3000, 3000)
    zen_power = rng.randint(-2400, 2400)
    pib1 = rng.randint(-800, 800)
    pib2 = rng.randint(-800, 800)

    return Reading(
        p1=p1,
        pibs=[pib1, pib2],
        pib_socs=[pib1_soc, pib2_soc],
        zen_power=zen_power,
        zen_soc=zen_soc,
        solar=solar,
    )


def _steady_reading(p1=0, solar=0, zen_power=0, zen_soc=50, pib1=0, pib2=0,
                    pib1_soc=50, pib2_soc=50):
    return Reading(
        p1=p1,
        pibs=[pib1, pib2],
        pib_socs=[pib1_soc, pib2_soc],
        zen_power=zen_power,
        zen_soc=zen_soc,
        solar=solar,
    )


class TestNoRapidStateBouncing:
    """State should not bounce between the same two states rapidly.

    Production bug 2026-04-08: SLEEP↔CHARGE flapping every 14s when
    all batteries full. Also CHARGE↔DISCHARGE bouncing during oven
    preheating cycles."""

    def _count_bounces(self, brain, readings, max_bounces=5):
        """Run brain through readings, return number of A→B→A bounces."""
        bounces = 0
        states = []
        for tick, r in enumerate(readings):
            brain.decide(r, t=tick)
            state = brain.state.value if hasattr(brain.state, 'value') else brain._phase
            states.append(state)
            if len(states) >= 3:
                if states[-1] == states[-3] and states[-1] != states[-2]:
                    bounces += 1
        return bounces

    def test_no_bounce_pib_99_100_boundary(self):
        """PIBs at 99-100% SOC boundary — pib_max_charge oscillates.
        Should not cause SLEEP↔CHARGE flapping.
        Production bug 2026-04-08 19:01."""
        brain = PermissionFSM()
        readings = []
        for tick in range(200):
            # PIB1 oscillates 99↔100%, PIB2 at 100%
            pib1_soc = 100 if tick % 4 < 2 else 99
            readings.append(_steady_reading(
                p1=-2500, solar=3000, zen_soc=100,
                pib1_soc=pib1_soc, pib2_soc=100,
            ))
        bounces = self._count_bounces(brain, readings)
        assert bounces <= 2, f"Bounced {bounces} times at PIB 99/100% boundary"

    def test_no_bounce_all_full(self):
        """All batteries at 100%, exporting — should not bounce."""
        brain = PermissionFSM()
        readings = [_steady_reading(p1=-3000, solar=4000, zen_soc=100,
                                     pib1_soc=100, pib2_soc=100)
                    for _ in range(200)]
        bounces = self._count_bounces(brain, readings)
        assert bounces <= 2, f"Bounced {bounces} times with all batteries full"

    def test_no_bounce_all_empty_importing(self):
        """All batteries empty, importing — should stay in SLEEP, not
        bounce SLEEP→DISCHARGE→PIB_DISCHARGE→SLEEP.
        Production bug 2026-04-10 07:41."""
        brain = PermissionFSM()
        readings = [_steady_reading(p1=300, solar=150, zen_soc=10,
                                     pib1_soc=0, pib2_soc=0)
                    for _ in range(200)]
        bounces = self._count_bounces(brain, readings)
        assert bounces <= 2, f"Bounced {bounces} times with all batteries empty and importing"

    def test_no_bounce_all_empty(self):
        """All batteries empty, importing — should not bounce."""
        brain = PermissionFSM()
        readings = [_steady_reading(p1=500, solar=0, zen_soc=10,
                                     pib1_soc=0, pib2_soc=0)
                    for _ in range(200)]
        bounces = self._count_bounces(brain, readings)
        assert bounces <= 2, f"Bounced {bounces} times with all batteries empty"

    def test_no_bounce_oven_cycling(self):
        """Oven preheating: 2200W on/off every 15s — should not cause state bouncing."""
        brain = PermissionFSM()
        readings = []
        for tick in range(300):
            oven_on = (tick % 30) < 15  # 15s on, 15s off
            load = 2200 if oven_on else 0
            p1 = -1500 + load  # 1500W solar surplus minus load
            readings.append(_steady_reading(
                p1=p1, solar=2000, zen_power=500, zen_soc=50,
                pib1=200, pib2=200, pib1_soc=50, pib2_soc=50,
            ))
        bounces = self._count_bounces(brain, readings)
        assert bounces <= 5, f"Bounced {bounces} times during oven cycling"

    def test_no_bounce_ev_plugunplug(self):
        """EV plugs in then unplugs — limited state changes."""
        brain = PermissionFSM()
        readings = []
        # Steady state discharge
        for _ in range(40):
            readings.append(_steady_reading(p1=300, solar=0, zen_power=-300,
                                            zen_soc=80))
        # EV plugs in (big load)
        for _ in range(60):
            readings.append(_steady_reading(p1=3500, solar=0, zen_power=-2400,
                                            zen_soc=70, pib1=-800, pib2=-800,
                                            pib1_soc=60, pib2_soc=60))
        # EV unplugs
        for _ in range(60):
            readings.append(_steady_reading(p1=-1800, solar=0, zen_power=-2400,
                                            zen_soc=60))
        # Settle
        for _ in range(40):
            readings.append(_steady_reading(p1=5, solar=0, zen_power=-300,
                                            zen_soc=55))
        bounces = self._count_bounces(brain, readings)
        assert bounces <= 5, f"Bounced {bounces} times during EV plug/unplug"


class TestChargeStepsUpWhenPIBsSaturated:
    """In CHARGE state with PIBs at their current taper-limited capacity,
    Zendure should step up to absorb remaining surplus.

    Production bug 2026-04-18 19:52-20:16: one PIB at 84% SOC (maxed at
    800W), other at 97-98% SOC (tapered to ~240W). Combined = 1040W, below
    the fixed PIB_HIGH=1200W threshold, so step-up never fired. P1 exported
    ~850W to grid for 24 minutes before the first PIB also entered taper."""

    def test_zen_steps_up_with_one_pib_deep_in_taper(self):
        brain = PermissionFSM()
        brain.state = brain.state.__class__("CHARGE")

        # PIB1 at 84% → cap=800W, absorbing 800W (maxed at its current cap).
        # PIB2 at 97% → cap=240W, absorbing 240W (deep taper, maxed).
        # Zen at 36% SOC, currently 0W, massive solar surplus.
        for tick in range(30):
            d = brain.decide(
                _steady_reading(
                    p1=-850, solar=2500,
                    zen_power=0, zen_soc=36,
                    pib1=800, pib2=240,
                    pib1_soc=84, pib2_soc=97,
                ),
                t=tick,
            )

        assert d.target > 0, (
            f"Zen still at {d.target}W after 30s of -850W export with PIBs "
            f"saturated at 1040W (their current cap). Step-up guard is not "
            f"catching taper-limited saturation."
        )


class TestDoesNotKillWorkingZendure:
    """When the brain enters CHARGE while the Zendure is already charging
    from genuine solar surplus, it must NOT send a standby command.

    Production bug 2026-04-26 10:10: add-on took over from systemd while
    Zen was at +2386W. Stepped CHARGE mode starts at step 0, so brain
    computes target=0. With the deadband, that becomes a 'send standby'
    once anything else has been sent. The Zendure was happily charging
    from a 4.7kW solar surplus — sending standby would have killed it
    and dumped >2kW to grid."""

    def test_target_zero_with_zen_charging_from_surplus_is_not_sent(self):
        brain = PermissionFSM()
        brain.last_sent_target = -500  # pretend we discharged earlier today
        brain.last_send_time = 0
        d = brain.decide(
            _steady_reading(
                p1=-591, solar=4754,
                zen_power=2386, zen_soc=56,
                pib1=60, pib2=60,
                pib1_soc=80, pib2_soc=80,
            ),
            t=100,
        )
        # Brain may decide target=0 in stepped mode, but it must NOT mark
        # the decision as send=True (which would push standby to Zendure).
        assert not (d.target == 0 and d.send), (
            f"Brain wants to send target=0 (standby) while Zen is charging "
            f"from surplus (zen_power=2386, p1=-591). Would kill the Zen."
        )

    def test_target_zero_with_zen_idle_can_still_send_standby(self):
        # Negative case: when Zen is already idle, target=0 with send=True
        # is still legitimate (brain re-confirms standby for a stalled Zen).
        brain = PermissionFSM()
        brain.last_sent_target = 800  # we charged earlier
        brain.last_send_time = 0
        # Zen at 0W (idle), P1 importing — brain SHOULD send a stop.
        # We just verify the safety guard didn't fire here.
        d = brain.decide(
            _steady_reading(p1=300, solar=0, zen_power=0, zen_soc=50,
                            pib1=0, pib2=0, pib1_soc=50, pib2_soc=50),
            t=100,
        )
        # Either the brain transitions out of CHARGE or it stays and
        # safely commands standby — as long as we don't crash.
        assert d.target <= 0, (
            f"Unexpected target={d.target} when Zen is idle and importing"
        )


class TestPIBDischargeExitsWhenEmpty:
    """PIB_DISCHARGE should transition to SLEEP when PIBs are truly empty."""

    def test_pibs_at_zero_soc_exits(self):
        """PIBs at 0% SOC producing 0W should trigger SLEEP.
        Production bug 2026-04-09 02:15."""
        brain = PermissionFSM()
        brain.state = brain.state.__class__("PIB_DISCHARGE")

        for tick in range(10):
            brain.decide(
                _steady_reading(p1=300, pib1=0, pib2=0, zen_soc=10,
                                pib1_soc=0, pib2_soc=0),
                t=tick,
            )

        assert brain.state.value == "SLEEP", (
            f"Expected SLEEP but got {brain.state.value}. "
            "PIBs at 0% producing 0W should exit PIB_DISCHARGE."
        )


class TestNeverChargeFromGrid:
    """Brain should never charge batteries from the grid (positive Zendure
    target while P1 is importing and no solar)."""

    def test_random_sequences(self):
        """Run 10 random 500-tick sequences, verify no grid charging."""
        for seed in range(10):
            rng = random.Random(seed)
            brain = PermissionFSM()
            grid_charge_ticks = 0

            for tick in range(500):
                r = _random_reading(rng)
                d = brain.decide(r, t=tick)

                # Grid charging = target > 100W AND P1 > 100 (importing) AND no solar
                if d.target > 100 and r.p1 > 100 and r.solar < 50:
                    grid_charge_ticks += 1

            assert grid_charge_ticks <= 5, (
                f"Seed {seed}: {grid_charge_ticks} ticks of grid charging "
                f"(target > 100W with P1 > 100W and no solar)"
            )


class TestNeverDischargeToExport:
    """Brain should not sustain discharge while heavily exporting."""

    def test_discharge_during_heavy_export(self):
        """If P1 < -500 for 30+ seconds, brain should not keep discharging."""
        brain = PermissionFSM()
        brain.state = brain.state.__class__("DISCHARGE")
        brain.last_sent_target = -1000
        brain.last_send_time = 0

        # Sustained heavy export
        for tick in range(60):
            d = brain.decide(
                _steady_reading(p1=-1000, solar=0, zen_power=-1000, zen_soc=50),
                t=tick,
            )

        # Target should have reduced significantly
        assert d.target > -200, (
            f"Still discharging {d.target}W after 60s of -1000W P1. "
            "Should have reduced to near-pilot."
        )


class TestPIBCommandRate:
    """Should not send more than a few PIB commands per minute in steady state."""

    def test_steady_charge(self):
        """During steady charging, PIB commands should be rare."""
        brain = PermissionFSM()
        commands = 0
        for tick in range(300):  # 5 minutes
            d = brain.decide(
                _steady_reading(p1=-500, solar=2000, zen_power=1000, zen_soc=50,
                                pib1=400, pib2=400, pib1_soc=50, pib2_soc=50),
                t=tick,
            )
            if d.pib_mode is not None:
                commands += 1
        # Allow startup commands but not sustained chatter
        assert commands <= 5, f"Sent {commands} PIB commands in 300 ticks of steady charge"

    def test_steady_discharge(self):
        """During steady discharge, PIB commands should be rare."""
        brain = PermissionFSM()
        commands = 0
        for tick in range(300):
            d = brain.decide(
                _steady_reading(p1=5, solar=0, zen_power=-400, zen_soc=50),
                t=tick,
            )
            if d.pib_mode is not None:
                commands += 1
        assert commands <= 5, f"Sent {commands} PIB commands in 300 ticks of steady discharge"


class TestP1Convergence:
    """P1 should converge toward zero within reasonable time
    (unless batteries are at their limits)."""

    def test_surplus_captured(self):
        """With solar surplus and PIBs maxed, brain should step up Zendure."""
        brain = PermissionFSM()
        # Wake up and let PIBs saturate
        for tick in range(15):
            brain.decide(
                _steady_reading(p1=-2000, solar=3000, zen_soc=20,
                                pib1=800, pib2=800, pib1_soc=20, pib2_soc=20),
                t=tick,
            )
        # PIBs maxed, surplus still exporting — Zendure should charge
        d = brain.decide(
            _steady_reading(p1=-2000, solar=3000, zen_power=0, zen_soc=20,
                            pib1=800, pib2=800, pib1_soc=20, pib2_soc=20),
            t=20,
        )
        assert d.target > 0, f"Should be charging with PIBs maxed and 2000W surplus, target={d.target}"

    def test_deficit_covered(self):
        """With import and charged batteries, brain should discharge."""
        brain = PermissionFSM()
        for tick in range(35):
            brain.decide(
                _steady_reading(p1=1000, solar=0, zen_soc=80),
                t=tick,
            )
        d = brain.decide(
            _steady_reading(p1=1000, solar=0, zen_power=0, zen_soc=80),
            t=40,
        )
        assert d.target < 0, f"Should be discharging with 1000W deficit, target={d.target}"
