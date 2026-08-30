"""Dynamic-tariff gates: inhibit entities + opt-in grid charge.

The brain is P1-driven and has no price model. On a dynamic tariff the
cheap-import case (EV charging at a €0.00 market price) makes "zero the
meter" the wrong objective — the batteries drain into the EV and have
nothing left for the expensive evening peak.

Policy lives in Home Assistant; the brain only consumes three booleans:
inhibit_discharge, inhibit_charge, force_charge.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "battery-coordinator" / "app"))

from brains.permission_fsm import PermissionFSM, State
from coordinator_logic import Reading


def _r(p1=0, zen_power=0, zen_soc=50, pibs=(0, 0), pib_socs=(50, 50), **gates):
    return Reading(
        p1=p1,
        pibs=list(pibs),
        pib_socs=list(pib_socs),
        zen_power=zen_power,
        zen_soc=zen_soc,
        **gates,
    )


def _settle(brain, reading, ticks, t0=0):
    """Run the brain over a steady reading; return the last Decision."""
    d = None
    for i in range(ticks):
        d = brain.decide(reading, t=t0 + i)
    return d


def _run(brain, reading, ticks, t0=0):
    """Run the brain over a steady reading; return every Decision.

    PIB mode/permissions are emitted on the entry tick of a transition and
    then only re-asserted on the 5-minute heartbeat, so assertions about
    them have to look across the window rather than at the last tick.
    """
    return [brain.decide(reading, t=t0 + i) for i in range(ticks)]


def _state(brain):
    """Read brain.state through a call.

    Asserting on the attribute directly makes mypy narrow it to a literal
    and then flag every later comparison as non-overlapping — the brain
    mutates between asserts, so the narrowing is wrong.
    """
    return brain.state


def _modes(decisions):
    return [d.pib_mode for d in decisions if d.pib_mode is not None]


class TestInhibitDischarge:
    """The EV case: cheap import now, expensive tonight — hold the SOC."""

    def test_discharge_stops_when_inhibited(self):
        """Batteries covering an EV load stand down once HA raises the gate."""
        brain = PermissionFSM()
        # Wake into DISCHARGE on sustained import.
        _settle(brain, _r(p1=1500, zen_soc=80), ticks=60)
        assert _state(brain) == State.DISCHARGE

        ds = _run(brain, _r(p1=1500, zen_power=-1500, zen_soc=80, inhibit_discharge=True), ticks=5, t0=60)
        assert _state(brain) == State.SLEEP
        assert ds[-1].target == 0
        assert _modes(ds) == ["standby"]
        assert ds[0].inhibited is True

    def test_inhibited_sleep_does_not_wake_into_discharge(self):
        """Sustained import well past WAKE_DISCHARGE_S must not wake it."""
        brain = PermissionFSM()
        d = _settle(brain, _r(p1=3000, zen_soc=80, inhibit_discharge=True), ticks=300)
        assert _state(brain) == State.SLEEP
        assert d.target == 0

    def test_standby_is_actually_sent_over_the_startup_carveout(self):
        """_should_send suppresses target=0 while Zen discharges into demand.

        That carve-out exists for the startup holdoff, but it fires on
        exactly the reading this feature must act on (Zen discharging,
        P1 importing) — the gate has to punch through it or the inhibit
        is silently a no-op.
        """
        brain = PermissionFSM()
        _settle(brain, _r(p1=1500, zen_soc=80), ticks=60)
        brain.mark_sent(-1500, 60)

        sends = [
            brain.decide(_r(p1=1500, zen_power=-1500, zen_soc=80, inhibit_discharge=True), t=61 + i) for i in range(5)
        ]
        assert any(d.send and d.target == 0 for d in sends)

    def test_release_resumes_normal_discharge(self):
        """Clearing the gate must not leave the brain stuck in SLEEP."""
        brain = PermissionFSM()
        _settle(brain, _r(p1=1500, zen_soc=80, inhibit_discharge=True), ticks=100)
        assert _state(brain) == State.SLEEP

        _settle(brain, _r(p1=1500, zen_soc=80), ticks=100, t0=100)
        assert _state(brain) == State.DISCHARGE

    def test_charging_is_unaffected(self):
        """inhibit_discharge must not block harvesting solar surplus."""
        brain = PermissionFSM()
        _settle(brain, _r(p1=-2000, zen_soc=40, inhibit_discharge=True), ticks=60)
        assert _state(brain) == State.CHARGE

    def test_never_discharges_while_inhibited(self):
        """Invariant sweep: no negative target under any P1/SOC combination."""
        brain = PermissionFSM()
        for tick, p1 in enumerate(range(-3000, 3001, 100)):
            for soc in (5, 20, 50, 95):
                d = brain.decide(
                    _r(p1=p1, zen_power=-800, zen_soc=soc, pibs=(-400, -400), inhibit_discharge=True),
                    t=tick * 10,
                )
                assert d.target >= 0, f"discharged {d.target}W at p1={p1} soc={soc}"


class TestInhibitCharge:
    """Mirror image — used to stop charging when export is worth more."""

    def test_charge_stops_when_inhibited(self):
        brain = PermissionFSM()
        _settle(brain, _r(p1=-2000, zen_soc=40), ticks=60)
        assert _state(brain) == State.CHARGE

        ds = _run(brain, _r(p1=-2000, zen_power=800, zen_soc=40, inhibit_charge=True), ticks=5, t0=60)
        assert _state(brain) == State.SLEEP
        assert ds[-1].target == 0
        assert _modes(ds) == ["standby"]
        assert ds[0].inhibited is True

    def test_standby_is_actually_sent_over_the_surplus_carveout(self):
        """Symmetric to the discharge carve-out — Zen charging from surplus."""
        brain = PermissionFSM()
        _settle(brain, _r(p1=-2000, zen_soc=40), ticks=60)
        brain.mark_sent(800, 60)

        sends = [brain.decide(_r(p1=-2000, zen_power=800, zen_soc=40, inhibit_charge=True), t=61 + i) for i in range(5)]
        assert any(d.send and d.target == 0 for d in sends)

    def test_inhibited_sleep_does_not_wake_into_charge(self):
        brain = PermissionFSM()
        _settle(brain, _r(p1=-3000, zen_soc=40, inhibit_charge=True), ticks=300)
        assert _state(brain) == State.SLEEP

    def test_discharging_is_unaffected(self):
        brain = PermissionFSM()
        _settle(brain, _r(p1=1500, zen_soc=80, inhibit_charge=True), ticks=60)
        assert _state(brain) == State.DISCHARGE


class TestGridCharge:
    """Opt-in arbitrage: import deliberately while the market price is low."""

    def test_force_charge_ignored_when_not_allowed(self):
        """Default off — the 'never charge from the grid' rule still holds."""
        brain = PermissionFSM()
        d = _settle(brain, _r(p1=500, zen_soc=40, force_charge=True), ticks=300)
        assert _state(brain) != State.GRID_CHARGE
        assert d.target <= 0 or brain.state == State.CHARGE

    def test_force_charge_pulls_from_grid_when_allowed(self):
        brain = PermissionFSM(allow_grid_charge=True)
        ds = _run(brain, _r(p1=500, zen_soc=40, force_charge=True), ticks=10)
        assert _state(brain) == State.GRID_CHARGE
        assert ds[-1].target == brain.max_charge
        assert _modes(ds) == ["to_full"]
        assert ds[0].pib_permissions == ["charge_allowed"]

    def test_grid_charge_wins_over_a_discharge_state(self):
        """Price flips cheap mid-discharge — stop draining, start filling."""
        brain = PermissionFSM(allow_grid_charge=True)
        _settle(brain, _r(p1=1500, zen_soc=40), ticks=60)
        assert _state(brain) == State.DISCHARGE

        d = _settle(brain, _r(p1=1500, zen_power=-1500, zen_soc=40, force_charge=True), ticks=5, t0=60)
        assert _state(brain) == State.GRID_CHARGE
        assert d.target > 0

    def test_grid_charge_stops_at_soc_ceiling(self):
        brain = PermissionFSM(allow_grid_charge=True, zen_soc_max=90)
        d = _settle(brain, _r(p1=500, zen_soc=90, force_charge=True, pib_socs=(100, 100)), ticks=10)
        assert _state(brain) != State.GRID_CHARGE
        assert d.target == 0

    def test_release_returns_to_normal_control(self):
        brain = PermissionFSM(allow_grid_charge=True)
        _settle(brain, _r(p1=500, zen_soc=40, force_charge=True), ticks=10)
        assert _state(brain) == State.GRID_CHARGE

        _settle(brain, _r(p1=1500, zen_soc=40), ticks=100, t0=10)
        assert _state(brain) == State.DISCHARGE

    def test_inhibit_charge_beats_force_charge(self):
        """Contradictory gates: the inhibit is the conservative one, it wins."""
        brain = PermissionFSM(allow_grid_charge=True)
        d = _settle(brain, _r(p1=500, zen_soc=40, force_charge=True, inhibit_charge=True), ticks=10)
        assert _state(brain) != State.GRID_CHARGE
        assert d.target == 0


class TestBackwardCompatibility:
    """Readings without gates must behave exactly as before."""

    def test_gates_default_to_off(self):
        r = _r(p1=1500, zen_soc=80)
        assert r.inhibit_discharge is False
        assert r.inhibit_charge is False
        assert r.force_charge is False

    def test_ungated_discharge_still_wakes(self):
        brain = PermissionFSM()
        _settle(brain, _r(p1=1500, zen_soc=80), ticks=60)
        assert _state(brain) == State.DISCHARGE


class TestGateDoesNotSpamTheDevice:
    """A held gate must not turn into a per-tick write.

    main.py maps target=0 in SLEEP to Zendure standby(), which is a FLASH
    write. The inhibit bypasses the send-suppression carve-outs, so if that
    bypass stayed armed for every tick of a held gate it would burn flash
    at 1 Hz for as long as the EV was plugged in.
    """

    def test_held_inhibit_sends_once_then_falls_back_to_idle_heartbeat(self):
        brain = PermissionFSM()
        _settle(brain, _r(p1=1500, zen_soc=80), ticks=60)
        brain.mark_sent(-1500, 60)

        r = _r(p1=1500, zen_power=-1500, zen_soc=80, inhibit_discharge=True)
        sends = 0
        for i in range(600):
            d = brain.decide(r, t=61 + i)
            if d.send:
                sends += 1
                brain.mark_sent(d.target, 61 + i)

        # One entry send, then only the 300s idle heartbeat over 10 minutes.
        assert sends <= 4, f"{sends} sends in 600 ticks — gate is re-arming every tick"
        assert sends >= 1

    def test_inhibited_flag_marks_only_the_entry_tick(self):
        brain = PermissionFSM()
        _settle(brain, _r(p1=1500, zen_soc=80), ticks=60)
        ds = _run(brain, _r(p1=1500, zen_power=-1500, zen_soc=80, inhibit_discharge=True), ticks=20, t0=60)
        assert [d.inhibited for d in ds].count(True) == 1

    def test_grid_charge_reasserts_to_full_on_the_pib_heartbeat(self):
        """to_full must survive drift — the PIBs stop charging without it."""
        brain = PermissionFSM(allow_grid_charge=True)
        ds = _run(brain, _r(p1=500, zen_soc=40, force_charge=True), ticks=700)
        assert _modes(ds).count("to_full") >= 2


class TestForceDischarge:
    """Deliberate max-rate discharge, exporting the surplus.

    This is the one gate that switches OFF a safety rule rather than adding
    one, so it needs its own opt-in (`allow_grid_export`) — distinct from
    `allow_grid_charge`, which gates import. Only the Zendure can export:
    the PIBs have no watt setpoint and no discharge-regardless mode, so they
    only ever cover real load.
    """

    def test_ignored_when_export_not_allowed(self):
        """Default off — the no-export rule still stands."""
        brain = PermissionFSM()
        d = _settle(brain, _r(p1=200, zen_soc=80, force_discharge=True), ticks=300)
        assert _state(brain) != State.FORCE_DISCHARGE
        assert d.target >= -PermissionFSM().max_discharge  # no forced max

    def test_discharges_at_max_when_allowed(self):
        brain = PermissionFSM(allow_grid_export=True)
        ds = _run(brain, _r(p1=200, zen_soc=80, force_discharge=True), ticks=10)
        assert _state(brain) == State.FORCE_DISCHARGE
        assert ds[-1].target == -brain.max_discharge

    def test_pibs_are_never_charge_enabled_while_exporting(self):
        """The cross-charge trap: PIBs in zero mode would see the Zendure's
        export as surplus and absorb it, charging off our own output."""
        brain = PermissionFSM(allow_grid_export=True)
        ds = _run(brain, _r(p1=200, zen_soc=80, force_discharge=True), ticks=10)
        perms = [d.pib_permissions for d in ds if d.pib_permissions is not None]
        assert perms, "no PIB permissions emitted on entry"
        for p in perms:
            assert "charge_allowed" not in p, f"PIBs could absorb our own export: {p}"
            assert p == ["discharge_allowed"]

    def test_keeps_exporting_instead_of_backing_off(self):
        """The whole point: it must survive the guards that normally stop an
        export — the pilot-floor hold-off and the DISCHARGE->CHARGE flip."""
        brain = PermissionFSM(allow_grid_export=True)
        zen = 0.0
        targets = []
        for i in range(400):
            p1 = 150 + zen  # tiny house load; zen negative => grid export
            d = brain.decide(_r(p1=p1, zen_power=zen, zen_soc=80, force_discharge=True), t=i)
            targets.append(d.target)
            zen += (d.target - zen) * 0.5
        assert _state(brain) == State.FORCE_DISCHARGE, f"fell out to {_state(brain)}"
        assert targets[-1] == -brain.max_discharge
        assert zen < -2000, f"settled at {zen:.0f}W, not exporting at max"

    def test_no_export_invariant_still_holds_when_flag_is_off(self):
        """Regression guard: the opt-in must not weaken the default path."""
        brain = PermissionFSM()  # allow_grid_export defaults False
        zen = 0.0
        for i in range(400):
            p1 = 150 + zen
            d = brain.decide(_r(p1=p1, zen_power=zen, zen_soc=80), t=i)
            zen += (d.target - zen) * 0.5
        assert zen > -50, f"exported {abs(zen):.0f}W with the flag off"

    def test_inhibit_discharge_beats_force_discharge(self):
        brain = PermissionFSM(allow_grid_export=True)
        d = _settle(
            brain,
            _r(p1=200, zen_soc=80, force_discharge=True, inhibit_discharge=True),
            ticks=10,
        )
        assert _state(brain) != State.FORCE_DISCHARGE
        assert d.target == 0

    def test_force_charge_beats_force_discharge(self):
        """Contradictory commands resolve to charging, never a relay fight."""
        brain = PermissionFSM(allow_grid_charge=True, allow_grid_export=True)
        d = _settle(
            brain,
            _r(p1=200, zen_soc=40, force_discharge=True, force_charge=True),
            ticks=10,
        )
        assert _state(brain) == State.GRID_CHARGE
        assert d.target > 0

    def test_stops_at_the_soc_floor(self):
        brain = PermissionFSM(allow_grid_export=True, zen_soc_min=20)
        d = _settle(brain, _r(p1=200, zen_soc=20, force_discharge=True), ticks=10)
        assert _state(brain) != State.FORCE_DISCHARGE
        assert d.target == 0

    def test_release_returns_to_normal_control(self):
        brain = PermissionFSM(allow_grid_export=True)
        _settle(brain, _r(p1=200, zen_soc=80, force_discharge=True), ticks=10)
        assert _state(brain) == State.FORCE_DISCHARGE
        _settle(brain, _r(p1=1500, zen_soc=80), ticks=100, t0=10)
        assert _state(brain) == State.DISCHARGE

    def test_permissions_reasserted_on_the_pib_heartbeat(self):
        brain = PermissionFSM(allow_grid_export=True)
        ds = _run(brain, _r(p1=200, zen_soc=80, force_discharge=True), ticks=700)
        emitted = [d.pib_permissions for d in ds if d.pib_permissions is not None]
        assert len(emitted) >= 2, "desired PIB state never re-asserted"
