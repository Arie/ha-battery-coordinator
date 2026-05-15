"""Property-based invariant tests for all brains.

Generate random sequences of sensor readings and verify that fundamental
invariants hold regardless of the scenario. Catches edge cases that
specific test scenarios miss.
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "battery-coordinator" / "app"))

from brains.permission_fsm import PermissionFSM
from coordinator_logic import Reading


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


def _steady_reading(p1=0, solar=0, zen_power=0, zen_soc=50, pib1=0, pib2=0, pib1_soc=50, pib2_soc=50):
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
            state = brain.state.value
            states.append(state)
            if len(states) >= 3 and states[-1] == states[-3] and states[-1] != states[-2]:
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
            readings.append(
                _steady_reading(
                    p1=-2500,
                    solar=3000,
                    zen_soc=100,
                    pib1_soc=pib1_soc,
                    pib2_soc=100,
                )
            )
        bounces = self._count_bounces(brain, readings)
        assert bounces <= 2, f"Bounced {bounces} times at PIB 99/100% boundary"

    def test_no_bounce_all_full(self):
        """All batteries at 100%, exporting — should not bounce."""
        brain = PermissionFSM()
        readings = [_steady_reading(p1=-3000, solar=4000, zen_soc=100, pib1_soc=100, pib2_soc=100) for _ in range(200)]
        bounces = self._count_bounces(brain, readings)
        assert bounces <= 2, f"Bounced {bounces} times with all batteries full"

    def test_no_bounce_all_empty_importing(self):
        """All batteries empty, importing — should stay in SLEEP, not
        bounce SLEEP→DISCHARGE→PIB_DISCHARGE→SLEEP.
        Production bug 2026-04-10 07:41."""
        brain = PermissionFSM()
        readings = [_steady_reading(p1=300, solar=150, zen_soc=10, pib1_soc=0, pib2_soc=0) for _ in range(200)]
        bounces = self._count_bounces(brain, readings)
        assert bounces <= 2, f"Bounced {bounces} times with all batteries empty and importing"

    def test_no_bounce_all_empty(self):
        """All batteries empty, importing — should not bounce."""
        brain = PermissionFSM()
        readings = [_steady_reading(p1=500, solar=0, zen_soc=10, pib1_soc=0, pib2_soc=0) for _ in range(200)]
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
            readings.append(
                _steady_reading(
                    p1=p1,
                    solar=2000,
                    zen_power=500,
                    zen_soc=50,
                    pib1=200,
                    pib2=200,
                    pib1_soc=50,
                    pib2_soc=50,
                )
            )
        bounces = self._count_bounces(brain, readings)
        assert bounces <= 5, f"Bounced {bounces} times during oven cycling"

    def test_no_bounce_ev_plugunplug(self):
        """EV plugs in then unplugs — limited state changes."""
        brain = PermissionFSM()
        readings = []
        # Steady state discharge
        for _ in range(40):
            readings.append(_steady_reading(p1=300, solar=0, zen_power=-300, zen_soc=80))
        # EV plugs in (big load)
        for _ in range(60):
            readings.append(
                _steady_reading(
                    p1=3500, solar=0, zen_power=-2400, zen_soc=70, pib1=-800, pib2=-800, pib1_soc=60, pib2_soc=60
                )
            )
        # EV unplugs
        for _ in range(60):
            readings.append(_steady_reading(p1=-1800, solar=0, zen_power=-2400, zen_soc=60))
        # Settle
        for _ in range(40):
            readings.append(_steady_reading(p1=5, solar=0, zen_power=-300, zen_soc=55))
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
                    p1=-850,
                    solar=2500,
                    zen_power=0,
                    zen_soc=36,
                    pib1=800,
                    pib2=240,
                    pib1_soc=84,
                    pib2_soc=97,
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
                p1=-591,
                solar=4754,
                zen_power=2386,
                zen_soc=56,
                pib1=60,
                pib2=60,
                pib1_soc=80,
                pib2_soc=80,
            ),
            t=100,
        )
        # Brain may decide target=0 in stepped mode, but it must NOT mark
        # the decision as send=True (which would push standby to Zendure).
        assert not (d.target == 0 and d.send), (
            "Brain wants to send target=0 (standby) while Zen is charging "
            "from surplus (zen_power=2386, p1=-591). Would kill the Zen."
        )

    def test_full_battery_during_charge_sends_standby(self):
        """At zen_soc == zen_soc_max the SOC clamp pins target=0; the
        'don't kill a working Zendure' guard must NOT suppress that send.
        Battery is full — brain genuinely wants Zen to stop. The guard's
        intent is 'this is a stepped-mode-baseline 0, not a real stop',
        which doesn't apply when the SOC ceiling is the reason.

        Without this carve-out, the brain stops talking to the Zendure
        for as long as the CHARGE→SLEEP transition takes to fire (up to
        FLIP_S = 30s) while a saturated battery is asked to keep
        absorbing 800W+ of surplus."""
        from brains.permission_fsm import State

        brain = PermissionFSM()
        brain.state = State.CHARGE
        brain._zen_step_idx = 3  # 800W stepped baseline
        brain.last_sent_target = 800
        brain.last_send_time = 0

        # zen_soc just hit 100%. PIBs not all in taper (one at 50%) so we
        # stay in stepped mode → target = 800W → SOC clamp pins to 0.
        d = brain.decide(
            _steady_reading(
                p1=-1500, solar=3000, zen_power=800, zen_soc=100, pib1=400, pib2=400, pib1_soc=50, pib2_soc=50
            ),
            t=10,
        )
        assert d.target == 0, f"Expected SOC clamp to pin target=0, got {d.target}"
        assert d.send is True, (
            "Brain refused to send standby with zen_soc=100 while Zen still "
            "drawing 800W from surplus. The 'don't kill a working Zendure' "
            "safety guard incorrectly suppressed a legitimate battery-full "
            "stop, leaving a saturated Zen pinned at its last commanded "
            "target until the CHARGE→SLEEP transition eventually fires."
        )

    def test_empty_battery_during_discharge_sends_standby(self):
        """Mirror of the SOC-max case: at zen_soc <= zen_soc_min the SOC
        clamp pins discharge target=0; the symmetric SLEEP-startup safety
        guard must not suppress that send when the battery is the reason."""
        from brains.permission_fsm import State

        brain = PermissionFSM()
        brain.state = State.SLEEP
        brain.last_sent_target = -800
        brain.last_send_time = 0

        # Zen drained to floor while SLEEP-state startup detection is
        # still considering whether to adopt DISCHARGE. last_zen_power is
        # negative (still discharging), p1 is positive (importing).
        d = brain.decide(
            _steady_reading(p1=400, zen_power=-800, zen_soc=10, pib1=0, pib2=0, pib1_soc=50, pib2_soc=50),
            t=10,
        )
        assert d.target == 0
        assert d.send is True, (
            "Brain refused to send standby at zen_soc=zen_soc_min while Zen "
            "still discharging. SOC floor must override the SLEEP-startup "
            "discharge-safety guard."
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
            _steady_reading(p1=300, solar=0, zen_power=0, zen_soc=50, pib1=0, pib2=0, pib1_soc=50, pib2_soc=50),
            t=100,
        )
        # Either the brain transitions out of CHARGE or it stays and
        # safely commands standby — as long as we don't crash.
        assert d.target <= 0, f"Unexpected target={d.target} when Zen is idle and importing"


class TestSunriseFlipWithoutSolarSensor:
    """DISCHARGE → CHARGE must work when no solar sensor is configured.

    Older versions required `solar > 50` in addition to P1 export — without
    a solar entity that always read 0 and the flip never fired, leaving
    the brain dribbling its discharge floor (~50W) into export until SOC
    hit the minimum. Now P1 export alone is the signal."""

    def test_discharge_to_charge_flips_with_zero_solar(self):
        brain = PermissionFSM()
        brain.state = brain.state.__class__("DISCHARGE")
        # Sustained P1 export, but solar entity reads 0 (not configured).
        for tick in range(int(PermissionFSM.FLIP_S) + 5):
            brain.decide(
                _steady_reading(p1=-500, solar=0, zen_power=-500, zen_soc=70, pib1=0, pib2=0, pib1_soc=80, pib2_soc=80),
                t=tick,
            )
        assert brain.state.value == "CHARGE", (
            "Brain stayed in DISCHARGE despite sustained -500W export — sunrise flip should not require a solar entity."
        )

    def test_pib_discharge_to_charge_flips_with_zero_solar(self):
        brain = PermissionFSM()
        brain.state = brain.state.__class__("PIB_DISCHARGE")
        for tick in range(int(PermissionFSM.FLIP_S) + 5):
            brain.decide(
                _steady_reading(
                    p1=-500, solar=0, zen_power=0, zen_soc=10, pib1=-200, pib2=-200, pib1_soc=40, pib2_soc=40
                ),
                t=tick,
            )
        assert brain.state.value == "CHARGE"


class TestStandbyAtSaturation:
    """When everything is saturated and P1 is idle, brain should put PIBs
    in standby instead of leaving them awake in zero-mode burning idle.
    Symmetric: full + no demand, and empty + no surplus."""

    def test_charge_to_sleep_at_relaxed_full_with_idle_p1(self):
        """Near-full (99/99/99 — taper noise, not literal 100/100) + P1
        idle → SLEEP after FLIP_S."""
        brain = PermissionFSM()
        brain.state = brain.state.__class__("CHARGE")

        for tick in range(int(PermissionFSM.FLIP_S) + 5):
            brain.decide(
                _steady_reading(p1=0, solar=0, zen_power=0, zen_soc=99, pib1=0, pib2=0, pib1_soc=99, pib2_soc=99),
                t=tick,
            )

        assert brain.state.value == "SLEEP", (
            f"Expected SLEEP but got {brain.state.value}. Near-full + idle P1 should let PIBs standby."
        )

    def test_full_sleep_wakes_to_discharge_on_demand(self):
        """After SLEEPing at full saturation, P1 import wakes to DISCHARGE."""
        brain = PermissionFSM()
        brain.state = brain.state.__class__("CHARGE")

        for tick in range(int(PermissionFSM.FLIP_S) + 5):
            brain.decide(
                _steady_reading(p1=0, zen_soc=100, pib1_soc=100, pib2_soc=100),
                t=tick,
            )
        assert brain.state.value == "SLEEP"

        base = int(PermissionFSM.FLIP_S) + 5
        for tick in range(int(PermissionFSM.WAKE_DISCHARGE_S) + 5):
            brain.decide(
                _steady_reading(p1=500, zen_soc=100, pib1_soc=100, pib2_soc=100),
                t=base + tick,
            )
        assert brain.state.value == "DISCHARGE", (
            f"Expected DISCHARGE but got {brain.state.value}. SLEEP-from-full should still wake on P1 demand."
        )

    def test_drain_sleep_wakes_to_charge_on_surplus(self):
        """After draining to SLEEP via PIB_DISCHARGE, surplus still wakes."""
        brain = PermissionFSM()
        brain.state = brain.state.__class__("PIB_DISCHARGE")

        # PIB_DISCHARGE→SLEEP now has a 3s exit holdoff; loop a few ticks.
        for tick in range(5):
            brain.decide(
                _steady_reading(p1=0, zen_soc=10, pib1=0, pib2=0, pib1_soc=0, pib2_soc=0),
                t=tick,
            )
        assert brain.state.value == "SLEEP"

        for tick in range(int(PermissionFSM.WAKE_CHARGE_S) + 5):
            brain.decide(
                _steady_reading(p1=-500, solar=2000, zen_soc=10, pib1=0, pib2=0, pib1_soc=0, pib2_soc=0),
                t=tick + 1,
            )
        assert brain.state.value == "CHARGE", (
            f"Expected CHARGE but got {brain.state.value}. Drained SLEEP must still wake on solar surplus."
        )


class TestSleepEntryUsesStandby:
    """All paths into SLEEP should park PIBs in standby (not the old
    'zero+charge_allowed' fast-capture mode). The fast-capture saved a
    handful of seconds at sunrise but cost continuous PIB idle."""

    def test_pib_discharge_to_sleep_uses_standby(self):
        brain = PermissionFSM()
        brain.state = brain.state.__class__("PIB_DISCHARGE")

        # PIB_DISCHARGE→SLEEP has a 3s exit holdoff; capture the
        # transition tick (when pib_mode flips to standby).
        transition_d = None
        for tick in range(5):
            d = brain.decide(
                _steady_reading(p1=0, zen_power=0, zen_soc=10, pib1=0, pib2=0, pib1_soc=0, pib2_soc=0),
                t=tick,
            )
            if d.pib_mode is not None:
                transition_d = d

        assert brain.state.value == "SLEEP"
        assert transition_d is not None and transition_d.pib_mode == "standby", (
            f"Expected pib_mode='standby' on PIB_DISCHARGE→SLEEP, got "
            f"{transition_d.pib_mode if transition_d else None!r}."
        )

    def test_discharge_to_sleep_uses_standby(self):
        brain = PermissionFSM()
        brain.state = brain.state.__class__("DISCHARGE")

        d = brain.decide(
            _steady_reading(p1=0, zen_power=0, zen_soc=10, pib1=0, pib2=0, pib1_soc=0, pib2_soc=0),
            t=0,
        )

        assert brain.state.value == "SLEEP"
        assert d.pib_mode == "standby", f"Expected pib_mode='standby' on DISCHARGE→SLEEP, got {d.pib_mode!r}."


class TestPIBModeHeartbeat:
    """Brain re-asserts the desired PIB mode periodically so silent drift
    (failed PUT, external app toggle, integration refresh) self-heals."""

    def test_heartbeats_after_transition(self):
        brain = PermissionFSM()

        # Wake to DISCHARGE — captures the transition tick when pib_mode=standby fires.
        transition_t = None
        for tick in range(int(PermissionFSM.WAKE_DISCHARGE_S) + 5):
            d = brain.decide(
                _steady_reading(p1=500, zen_soc=80, pib1_soc=80, pib2_soc=80),
                t=tick,
            )
            if d.pib_mode == "standby":
                transition_t = tick
        assert brain.state.value == "DISCHARGE"
        assert transition_t is not None, "Transition should have set pib_mode=standby"

        # Between transition and heartbeat: no spurious sends.
        sends_before_heartbeat = 0
        for tick in range(transition_t + 1, transition_t + int(PermissionFSM.PIB_HEARTBEAT_S) - 1):
            d = brain.decide(
                _steady_reading(p1=500, zen_power=-500, zen_soc=70, pib1_soc=70, pib2_soc=70),
                t=tick,
            )
            if d.pib_mode is not None:
                sends_before_heartbeat += 1
        assert sends_before_heartbeat == 0, (
            f"Sent pib_mode {sends_before_heartbeat} times before heartbeat — "
            "should be silent until PIB_HEARTBEAT_S elapses."
        )

        # Past heartbeat: re-asserts.
        d = brain.decide(
            _steady_reading(p1=500, zen_power=-500, zen_soc=70, pib1_soc=70, pib2_soc=70),
            t=transition_t + int(PermissionFSM.PIB_HEARTBEAT_S) + 1,
        )
        assert d.pib_mode == "standby", f"Heartbeat should re-assert pib_mode=standby, got {d.pib_mode!r}"

    def test_no_spurious_send_at_startup(self):
        """Fresh brain in SLEEP with no transition fired → no pib_mode at all."""
        brain = PermissionFSM()
        for tick in range(int(PermissionFSM.PIB_HEARTBEAT_S) + 10):
            d = brain.decide(
                _steady_reading(p1=0, zen_soc=50, pib1_soc=50, pib2_soc=50),
                t=tick,
            )
            assert d.pib_mode is None, (
                f"Spurious pib_mode={d.pib_mode!r} at tick {tick} — brain "
                "has not transitioned, so it has no opinion to assert."
            )


class TestZenStandbyHeartbeat:
    """Zen target=0 (flash-standby) should also be re-asserted periodically,
    not just sent once on entry."""

    def test_heartbeats_in_pib_discharge(self):
        brain = PermissionFSM()
        brain.state = brain.state.__class__("PIB_DISCHARGE")
        # Pretend we were discharging at -800W just before draining.
        brain.last_sent_target = -800
        brain.last_send_time = 0

        # Past the 5s ramp-protection guard, brain sends target=0 once.
        d = brain.decide(
            _steady_reading(p1=500, zen_power=0, zen_soc=10, pib1=-200, pib2=-200, pib1_soc=40, pib2_soc=40),
            t=10,
        )
        assert d.target == 0
        assert d.send is True, "First standby send after PIB_DISCHARGE entry"
        brain.mark_sent(d.target, t=10)

        # Between first send and heartbeat: silent.
        for tick in range(11, 39):
            d = brain.decide(
                _steady_reading(p1=500, zen_power=0, zen_soc=10, pib1=-200, pib2=-200, pib1_soc=40, pib2_soc=40),
                t=tick,
            )
            assert d.send is False, f"Spurious send at t={tick} (only {tick - 10}s elapsed since standby)"

        # Past 30s: heartbeat re-asserts.
        d = brain.decide(
            _steady_reading(p1=500, zen_power=0, zen_soc=10, pib1=-200, pib2=-200, pib1_soc=40, pib2_soc=40),
            t=42,
        )
        assert d.target == 0 and d.send is True, (
            f"Heartbeat should re-assert standby at 30s+, got target={d.target} send={d.send}"
        )

    def test_no_spurious_send_at_startup(self):
        """Fresh brain in SLEEP, no last_sent_target → no spurious standbys."""
        brain = PermissionFSM()
        for tick in range(60):
            d = brain.decide(
                _steady_reading(p1=0, zen_soc=50, pib1_soc=50, pib2_soc=50),
                t=tick,
            )
            assert d.send is False, (
                f"Spurious send at t={tick} — brain has nothing to assert (last_sent_target is None)."
            )


class TestStartupDetection:
    """Brain restart in mid-state should detect what's actually happening
    rather than land in SLEEP and force-stop work-in-progress."""

    def test_sleep_to_pib_discharge_when_zen_drained_pibs_active(self):
        """Restart while Zen is drained and PIBs are providing the load —
        brain must adopt PIB_DISCHARGE, not SLEEP. Without this, the new
        PIB heartbeat would force-standby PIBs that are covering demand.

        Requires a few ticks of consistent agreement so a single noisy
        first reading can't lock the brain into a wrong state."""
        brain = PermissionFSM()
        transition_decision = None
        for tick in range(5):
            d = brain.decide(
                _steady_reading(p1=400, zen_power=0, zen_soc=10, pib1=-400, pib2=-400, pib1_soc=40, pib2_soc=40),
                t=tick,
            )
            if d.pib_mode is not None:
                transition_decision = d
        assert brain.state.value == "PIB_DISCHARGE", (
            f"Expected PIB_DISCHARGE on restart with drained Zen + active PIBs, got {brain.state.value}."
        )
        assert transition_decision is not None, "Transition should have emitted pib_mode"
        assert transition_decision.pib_mode == "zero"
        assert transition_decision.pib_permissions == ["discharge_allowed"]

    def test_single_noisy_reading_does_not_lock_state(self):
        """A single transient first reading shouldn't bypass the holdoffs.
        Sustained agreement is required before any startup-detection
        transition fires."""
        brain = PermissionFSM()
        # First tick suggests an in-progress charge (Zen at +2400W)…
        brain.decide(
            _steady_reading(p1=-500, zen_power=2400, zen_soc=50, pib1=0, pib2=0, pib1_soc=50, pib2_soc=50),
            t=0,
        )
        # …but next tick is back to idle (the first read was noise).
        brain.decide(
            _steady_reading(p1=0, zen_power=0, zen_soc=50, pib1=0, pib2=0, pib1_soc=50, pib2_soc=50),
            t=1,
        )
        assert brain.state.value == "SLEEP", (
            f"Brain locked into {brain.state.value} after a single noisy "
            "reading. Startup guards should require sustained agreement."
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
                _steady_reading(p1=300, pib1=0, pib2=0, zen_soc=10, pib1_soc=0, pib2_soc=0),
                t=tick,
            )

        assert brain.state.value == "SLEEP", (
            f"Expected SLEEP but got {brain.state.value}. PIBs at 0% producing 0W should exit PIB_DISCHARGE."
        )

    def test_settles_to_sleep_despite_pib_power_noise(self):
        """PIB power sensor noise — readings bouncing 0/15/0/12 — used to
        keep PIB_DISCHARGE pinned because the guard required all
        |p|<10 every tick. With holdoff hysteresis, transient noise
        within a holdoff window doesn't reset the timer — brain reaches
        SLEEP after sustained near-zero power."""
        brain = PermissionFSM()
        brain.state = brain.state.__class__("PIB_DISCHARGE")

        # 20 ticks with SOC at 0 and power oscillating in noise band.
        powers = [0, 12, 0, 15, 0, 8, 0, 14, 0, 9, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        for tick, p in enumerate(powers):
            brain.decide(
                _steady_reading(p1=300, pib1=p, pib2=0, zen_soc=10, pib1_soc=0, pib2_soc=0),
                t=tick,
            )
        # By tick 19, even if the noise dance prevented exit early on,
        # the long tail of zeros should let the brain reach SLEEP.
        assert brain.state.value == "SLEEP", (
            f"Brain stuck in {brain.state.value} after 20 ticks at 0% "
            "SOC. Sensor noise on PIB power shouldn't prevent SLEEP."
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
                f"Seed {seed}: {grid_charge_ticks} ticks of grid charging (target > 100W with P1 > 100W and no solar)"
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
            f"Still discharging {d.target}W after 60s of -1000W P1. Should have reduced to near-pilot."
        )


class TestP1ContradictDebounce:
    """A brief P1 spike contradicting our state direction shouldn't
    trigger fast step-down on its own — it has to persist a few ticks."""

    def test_brief_p1_spike_no_fast_step_down(self):
        """A 1-tick p1 spike during CHARGE shouldn't accelerate step-down.

        Setup: brain in CHARGE at step 4 (1200W), PIBs been at low (0W)
        for 7 ticks — between FAST (5s) and NORMAL (15s) holdoff. Without
        debounce, a single-tick p1>+200W spike tips us past 'p1_contradicts'
        which uses FAST, firing step-down. With debounce (3s sustained),
        a 1-tick spike is ignored.
        """
        brain = PermissionFSM()
        brain.state = brain.state.__class__("CHARGE")
        brain._zen_step_idx = 4  # 1200W
        brain._last_step_up_t = -100  # cooldown long elapsed

        # 7 ticks of PIBs idle, no contradicting P1 → pib_low_since=0
        for tick in range(7):
            brain.decide(
                _steady_reading(
                    p1=-50, solar=2000, zen_power=1200, zen_soc=50, pib1=0, pib2=0, pib1_soc=50, pib2_soc=50
                ),
                t=tick,
            )
        assert brain._zen_step_idx == 4

        # 1-tick contradicting P1 spike at t=7. PIBs still idle.
        # Without debounce: t - pib_low_since = 7 >= STEP_HOLDOFF_FAST (5)
        # → fast step-down fires immediately.
        # With 3s sustained debounce: contradiction not yet "real",
        # falls back to normal 15s holdoff which hasn't elapsed.
        brain.decide(
            _steady_reading(p1=300, solar=2000, zen_power=1200, zen_soc=50, pib1=0, pib2=0, pib1_soc=50, pib2_soc=50),
            t=7,
        )
        assert brain._zen_step_idx == 4, (
            "A 1-tick p1>+200W spike triggered fast step-down. "
            "Contradiction should require a few sustained ticks before "
            "shortening the step holdoff."
        )

    def test_sustained_contradiction_still_fast_steps(self):
        """Sustained p1_contradicts does eventually trigger fast step-down."""
        brain = PermissionFSM()
        brain.state = brain.state.__class__("CHARGE")
        brain._zen_step_idx = 4
        brain._last_step_up_t = -100

        # Establish low-PIB state without contradiction
        for tick in range(7):
            brain.decide(
                _steady_reading(
                    p1=-50, solar=2000, zen_power=1200, zen_soc=50, pib1=0, pib2=0, pib1_soc=50, pib2_soc=50
                ),
                t=tick,
            )
        # Now sustain p1>+200 for >= 3s + STEP_HOLDOFF_FAST (5s)
        for tick in range(7, 7 + 8):
            brain.decide(
                _steady_reading(
                    p1=300, solar=2000, zen_power=1200, zen_soc=50, pib1=0, pib2=0, pib1_soc=50, pib2_soc=50
                ),
                t=tick,
            )
        assert brain._zen_step_idx < 4, (
            f"Sustained contradiction should still allow fast step-down, but step is still {brain._zen_step_idx}."
        )


class TestPIBSendFailureRetry:
    """A failed PIB permission PUT must trigger immediate retry on the next
    tick — waiting 5 minutes for the heartbeat is the difference between
    'cross-charge prevented' and 'lockout never asserted, batteries fight'.
    """

    def test_mark_pib_send_failed_forces_next_tick_resend(self):
        brain = PermissionFSM()
        transition_t = None
        # Drive a SLEEP→DISCHARGE transition so brain has a desired mode.
        for tick in range(int(PermissionFSM.WAKE_DISCHARGE_S) + 5):
            d = brain.decide(
                _steady_reading(p1=500, zen_soc=80, pib1_soc=80, pib2_soc=80),
                t=tick,
            )
            if d.pib_mode == "standby":
                transition_t = tick
        assert transition_t is not None, "Should have transitioned and emitted pib_mode"

        # Simulate the PUT failing on the transition — caller signals it.
        brain.mark_pib_send_failed()

        # Next tick should re-emit pib_mode immediately, not wait for the
        # 5-minute heartbeat.
        d = brain.decide(
            _steady_reading(p1=500, zen_power=-500, zen_soc=70, pib1_soc=70, pib2_soc=70),
            t=transition_t + 1,
        )
        assert d.pib_mode == "standby", (
            "After mark_pib_send_failed, next decide() must re-emit the "
            "desired pib_mode. Without this, a single failed PUT means "
            f"{PermissionFSM.PIB_HEARTBEAT_S}s of cross-charge risk."
        )

    def test_no_resend_without_failure_signal(self):
        # Sanity: without the failure signal, normal heartbeat throttle
        # applies and there's no spurious extra send.
        brain = PermissionFSM()
        transition_t = None
        for tick in range(int(PermissionFSM.WAKE_DISCHARGE_S) + 5):
            d = brain.decide(
                _steady_reading(p1=500, zen_soc=80, pib1_soc=80, pib2_soc=80),
                t=tick,
            )
            if d.pib_mode == "standby":
                transition_t = tick
        assert transition_t is not None, "Should have transitioned and emitted pib_mode"
        d = brain.decide(
            _steady_reading(p1=500, zen_power=-500, zen_soc=70, pib1_soc=70, pib2_soc=70),
            t=transition_t + 1,
        )
        assert d.pib_mode is None


class TestPIBCommandRate:
    """Should not send more than a few PIB commands per minute in steady state."""

    def test_steady_charge(self):
        """During steady charging, PIB commands should be rare."""
        brain = PermissionFSM()
        commands = 0
        for tick in range(300):  # 5 minutes
            d = brain.decide(
                _steady_reading(
                    p1=-500, solar=2000, zen_power=1000, zen_soc=50, pib1=400, pib2=400, pib1_soc=50, pib2_soc=50
                ),
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


class TestChargeNomBoundaryRamp:
    """When CHARGE switches from stepped to NOM (PIBs entering taper),
    the target shouldn't jump ~1.4kW in a single tick — Zen overshoot
    follows. Clamp the first NOM tick to one step above the current
    stepped target."""

    def test_first_nom_tick_clamps_to_one_step_jump(self):
        brain = PermissionFSM()
        brain.state = brain.state.__class__("CHARGE")
        brain._zen_step_idx = 3  # 800W stepped baseline

        # Drive a stepped tick first so the brain knows it was stepped.
        brain.decide(
            _steady_reading(
                p1=-200, solar=2000, zen_power=800, zen_soc=50, pib1=400, pib2=400, pib1_soc=50, pib2_soc=50
            ),
            t=0,
        )

        # Next tick: PIBs hit taper (97% SOC). NOM target = zen_power - p1
        # = 800 - (-2000) = 2800 → clamped to max 2400.
        # The first NOM tick should clamp to current_step + 400 = 1200,
        # not jump to 2400.
        d = brain.decide(
            _steady_reading(
                p1=-2000, solar=4000, zen_power=800, zen_soc=50, pib1=240, pib2=240, pib1_soc=97, pib2_soc=97
            ),  # both in taper
            t=1,
        )
        assert d.target <= 1200, (
            f"First NOM tick jumped to {d.target}W from a stepped baseline "
            f"of 800W. Should have clamped to one step (~+400W) above the "
            "stepped target to avoid Zen overshoot."
        )

    def test_subsequent_nom_ticks_use_full_target(self):
        """After the first NOM tick has 'announced' NOM mode, subsequent
        ticks can use the full computed NOM target."""
        brain = PermissionFSM()
        brain.state = brain.state.__class__("CHARGE")
        brain._zen_step_idx = 3  # 800W

        # Stepped tick
        brain.decide(
            _steady_reading(
                p1=-200, solar=2000, zen_power=800, zen_soc=50, pib1=400, pib2=400, pib1_soc=50, pib2_soc=50
            ),
            t=0,
        )
        # First NOM tick (clamped)
        brain.decide(
            _steady_reading(
                p1=-2000, solar=4000, zen_power=800, zen_soc=50, pib1=240, pib2=240, pib1_soc=97, pib2_soc=97
            ),
            t=1,
        )
        # Subsequent NOM tick — full NOM applies. zen_power=1200, p1=-2000
        # → nom = 1200 - (-2000) = 3200 → clamped to max 2400.
        d = brain.decide(
            _steady_reading(
                p1=-2000, solar=4000, zen_power=1200, zen_soc=50, pib1=240, pib2=240, pib1_soc=97, pib2_soc=97
            ),
            t=2,
        )
        assert d.target == 2400


class TestP1Convergence:
    """P1 should converge toward zero within reasonable time
    (unless batteries are at their limits)."""

    def test_surplus_captured(self):
        """With solar surplus and PIBs maxed, brain should step up Zendure."""
        brain = PermissionFSM()
        # Wake up and let PIBs saturate
        for tick in range(15):
            brain.decide(
                _steady_reading(p1=-2000, solar=3000, zen_soc=20, pib1=800, pib2=800, pib1_soc=20, pib2_soc=20),
                t=tick,
            )
        # PIBs maxed, surplus still exporting — Zendure should charge
        d = brain.decide(
            _steady_reading(
                p1=-2000, solar=3000, zen_power=0, zen_soc=20, pib1=800, pib2=800, pib1_soc=20, pib2_soc=20
            ),
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


class TestTransitionTimersResetOnStateEntry:
    """A transition's holdoff (`_since`) must reset each time the brain
    enters the source state, not carry stale state from a prior visit.

    Without the reset, leaving a state via a fast transition while a slow
    transition was partway through its holdoff lets the slow one fire
    prematurely on the next visit (it sees `t - _since` exceeding the
    holdoff because _since was set during the previous visit).
    """

    def test_wake_holdoff_does_not_carry_over_between_visits(self):
        from brains.permission_fsm import State

        brain = PermissionFSM()

        # Visit 1: arm SLEEP→CHARGE wake (WAKE_CHARGE_S=10s) for 5s of
        # sustained P1 export — half the holdoff, so it shouldn't fire.
        for tick in range(5):
            brain.decide(
                _steady_reading(p1=-500, zen_power=0, zen_soc=50, pib1=0, pib2=0, pib1_soc=50, pib2_soc=50),
                t=tick,
            )
        assert brain.state == State.SLEEP

        # Simulate having taken a different transition out of SLEEP and
        # then returning. Direct manipulation skips the natural FSM path
        # so the test stays focused on the timer-reset invariant rather
        # than the choreography of getting back to SLEEP.
        brain.state = State.CHARGE
        brain._init_step_for_new_state()
        brain.state = State.SLEEP
        brain._init_step_for_new_state()

        # Re-arm the wake transition at t=6. With the reset: arms at t=6,
        # fires at t=16. Without the reset: stale _since=0, fires at t=10.
        for tick in range(6, 12):
            brain.decide(
                _steady_reading(p1=-500, zen_power=0, zen_soc=50, pib1=0, pib2=0, pib1_soc=50, pib2_soc=50),
                t=tick,
            )
        assert brain.state == State.SLEEP, (
            f"Wake-charge transition fired prematurely after SLEEP "
            f"re-entry (state={brain.state.value}). The _since timer was "
            "not reset on state entry — a stale ~11-second-old timestamp "
            "from the previous SLEEP visit let the 10s holdoff appear "
            "elapsed after only ~5s of fresh signal."
        )


class TestChargeFlipSuppressedWhileSteppingDown:
    """When PIBs are idle and P1 is importing during CHARGE, but the
    import is caused by Zen absorbing more than the available solar at
    the current step, the brain should step down toward 0 instead of
    flipping to DISCHARGE.

    Production bug 2026-05-02 18:00: cloud halved solar while Zen was at
    step 1200W. Brain stepped down 2400→2000→1600→1200 but the CHARGE→
    DISCHARGE flip (FLIP_S=30s) won the race — fired a mode-switch relay
    click to discharge at 50W, immediately over-discharged, and flipped
    back to CHARGE 35s later. A wasted relay click."""

    def test_no_flip_while_zen_step_above_zero(self):
        brain = PermissionFSM()
        brain.state = brain.state.__class__("CHARGE")
        brain._zen_step_idx = 4  # step = 1200W

        # Cloud scenario: solar dropped, Zen at +1200W is the cause of
        # import. PIBs idle (charge-only, nothing to absorb).
        for tick in range(int(PermissionFSM.FLIP_S) + 5):
            brain.decide(
                _steady_reading(
                    p1=+250, solar=1500, zen_power=1200, zen_soc=56, pib1=8, pib2=8, pib1_soc=50, pib2_soc=50
                ),
                t=tick,
            )

        assert brain.state.value != "DISCHARGE", (
            f"Brain flipped to DISCHARGE while Zen was stepping down "
            f"(now at step {brain._current_step()}W). The Zen charge was "
            "causing the P1 import — should step down, not relay-click."
        )

    def test_bottom_step_is_pilot_not_zero(self):
        """Stepping all the way down should land at PILOT_W (50W), not 0W.
        At 0W the brain sends standby (smartMode=0), which clears ac_mode.
        When solar returns and the brain steps back up, the cleared ac_mode
        forces a mode-switch relay click. At PILOT_W the brain sends
        charge(50) which keeps ac_mode=AC_CHARGE — stepping back up is
        seamless.

        Production observation 2026-05-11 18:25: cloud caused step-down
        to 0 → standby → cloud cleared → step up with mode switch.
        Avoidable relay click."""
        brain = PermissionFSM()
        brain.state = brain.state.__class__("CHARGE")
        brain._zen_step_idx = 2  # step = 400W

        # Step all the way down.
        for tick in range(30):
            brain.decide(
                _steady_reading(
                    p1=+500, solar=1500, zen_power=400, zen_soc=60, pib1=8, pib2=8, pib1_soc=50, pib2_soc=50
                ),
                t=tick,
            )

        from coordinator_logic import PILOT_W

        assert brain._current_step() == PILOT_W, (
            f"Bottom step is {brain._current_step()}W, expected {PILOT_W}W. "
            "Step 0W triggers standby → clears ac_mode → relay click on "
            "step-up. PILOT_W keeps the Zen in charge mode."
        )


class TestStepJumpDown:
    """Symmetric to the existing step-up jump: when PIBs are idle and P1 is
    heavily importing in CHARGE, the brain should jump the step down in one
    tick instead of walking 1600→1200→800→400→200→50 at 5s/step.

    Production observation 2026-05-12 08:43: EV turned on, P1 spiked to
    +1600W while Zen was at step 1600. Brain took 25s walking down while
    pulling 1600W from the grid."""

    def test_jump_down_on_heavy_import(self):
        brain = PermissionFSM()
        brain.state = brain.state.__class__("CHARGE")
        brain._zen_step_idx = 5  # step = 1600W
        brain._last_step_up_t = -999

        # Sustained heavy import (EV turned on). PIBs idle. Needs:
        # 3s p1_contradicts debounce + 5s STEP_HOLDOFF_FAST = fires at ~t=5.
        for tick in range(7):
            brain.decide(
                _steady_reading(
                    p1=+1600,
                    solar=4000,
                    zen_power=1600,
                    zen_soc=50,
                    pib1=8,
                    pib2=8,
                    pib1_soc=50,
                    pib2_soc=50,
                ),
                t=tick,
            )

        from coordinator_logic import PILOT_W

        assert brain._current_step() <= 200, (
            f"Step is {brain._current_step()}W after 7s of +1600W import. "
            f"Should have jumped down near {PILOT_W}W, not walked slowly."
        )

    def test_moderate_import_partial_jump(self):
        brain = PermissionFSM()
        brain.state = brain.state.__class__("CHARGE")
        brain._zen_step_idx = 5  # step = 1600W
        brain._last_step_up_t = -999

        # Moderate import — should jump partway, not all the way down.
        for tick in range(7):
            brain.decide(
                _steady_reading(
                    p1=+600,
                    solar=4000,
                    zen_power=1600,
                    zen_soc=50,
                    pib1=8,
                    pib2=8,
                    pib1_soc=50,
                    pib2_soc=50,
                ),
                t=tick,
            )

        step = brain._current_step()
        assert step <= 1200 and step >= 400, (
            f"Step is {step}W after +600W import at step 1600. Expected partial jump to ~800-1000W range."
        )


class TestDischargeHelpOverDischargeHoldoff:
    """The DISCHARGE_HELP → DISCHARGE bail on `r.p1 < P1_OVER_DISCHARGE`
    must filter the 1–2-tick PIB activation transient. The HW P1 meter's
    autonomous PIB controller slams from 0 to ~max in a single tick on
    `zero+discharge_allowed`; with combined Zen+PIB discharge briefly
    exceeding load by ~1.6 kW, P1 spikes negative for a tick or two
    before NOM equilibrium catches up. Pre-fix, this caused 22 state
    bounces in 5 minutes of heavy EV load (production 2026-05-01)."""

    def test_pib_overshoot_does_not_bail_help_immediately(self):
        """1-tick P1 dip below P1_OVER_DISCHARGE during PIB activation
        must not exit DISCHARGE_HELP. P1 returns positive within 1-2s
        as the PIBs settle to NOM."""
        brain = PermissionFSM()
        brain.state = brain.state.__class__("DISCHARGE_HELP")

        # Tick 0: PIB slam-on transient — P1 briefly below -100.
        brain.decide(
            _steady_reading(p1=-200, zen_power=-2400, pib1=-781, pib2=-803, pib1_soc=98, pib2_soc=94),
            t=0,
        )
        # Tick 1-2: PIBs settling, P1 back above -100.
        brain.decide(
            _steady_reading(p1=+50, zen_power=-2400, pib1=-500, pib2=-600, pib1_soc=98, pib2_soc=94),
            t=1,
        )
        brain.decide(
            _steady_reading(p1=+200, zen_power=-2400, pib1=-400, pib2=-450, pib1_soc=98, pib2_soc=94),
            t=2,
        )

        assert brain.state.value == "DISCHARGE_HELP", (
            f"Expected DISCHARGE_HELP, got {brain.state.value}. A 1-tick "
            "P1 overshoot during the PIB activation transient should not "
            "trigger an immediate exit — the holdoff filters it."
        )

    def test_real_over_discharge_still_exits(self):
        """Sustained P1 < P1_OVER_DISCHARGE for the holdoff window (i.e.,
        load actually dropped) must still exit DISCHARGE_HELP."""
        brain = PermissionFSM()
        brain.state = brain.state.__class__("DISCHARGE_HELP")

        # 5 consecutive ticks of -300W — load really dropped.
        for tick in range(5):
            brain.decide(
                _steady_reading(p1=-300, zen_power=-2400, pib1=-700, pib2=-700, pib1_soc=80, pib2_soc=80),
                t=tick,
            )

        assert brain.state.value == "DISCHARGE", (
            f"Expected DISCHARGE after sustained over-discharge, got "
            f"{brain.state.value}. A real load drop must still trigger "
            "the bail, just with a small holdoff to filter PIB transients."
        )
