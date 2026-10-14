"""External setpoint: an optimiser (DAO) says how much, the brain says how.

The four tariff gates are on/off. A setpoint is a gate with a magnitude:
a wattage the Zendure must follow, with the PIB permissions chosen so the
PIBs can never fight it. Positive = charge, negative = discharge, 0 = hold.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "battery-coordinator" / "app"))

from brains.permission_fsm import PermissionFSM, State
from coordinator_logic import Reading


def _r(p1=0, zen_power=0, zen_soc=50, pibs=(0, 0), pib_socs=(50, 50), **extra):
    return Reading(
        p1=p1,
        pibs=list(pibs),
        pib_socs=list(pib_socs),
        zen_power=zen_power,
        zen_soc=zen_soc,
        **extra,
    )


class TestReading:
    def test_setpoint_is_absent_by_default(self):
        assert _r().setpoint is None

    def test_setpoint_is_carried_on_the_reading(self):
        assert _r(setpoint=1200).setpoint == 1200


def _brain(**kw):
    return PermissionFSM(allow_grid_charge=True, allow_grid_export=True, **kw)


def _run(brain, reading, ticks, t0=0):
    return [brain.decide(reading, t=t0 + i) for i in range(ticks)]


def _perms(decisions):
    return [(d.pib_mode, d.pib_permissions) for d in decisions if d.pib_mode is not None]


class TestSetpointDirection:
    def test_positive_setpoint_charges_zendure_with_pibs_charge_only(self):
        brain = _brain()
        ds = _run(brain, _r(p1=800, setpoint=1500), ticks=10)
        assert brain.state == State.SETPOINT
        assert ds[-1].target == 1500
        assert _perms(ds) == [("zero", ["charge_allowed"])]

    def test_negative_setpoint_discharges_zendure_with_pibs_discharge_only(self):
        brain = _brain()
        ds = _run(brain, _r(p1=-300, setpoint=-2000), ticks=10)
        assert brain.state == State.SETPOINT
        assert ds[-1].target == -2000
        assert _perms(ds) == [("zero", ["discharge_allowed"])]

    def test_zero_setpoint_holds_zendure_and_pibs(self):
        """0 is "hold": the PIBs keep their SOC instead of zero-tracking."""
        brain = _brain()
        ds = _run(brain, _r(p1=600, setpoint=0), ticks=10)
        assert brain.state == State.SETPOINT
        assert ds[-1].target == 0
        assert _perms(ds) == [("standby", None)]

    def test_full_rate_charge_setpoint_sends_pibs_to_full(self):
        """DAO asking the Zendure's full rate means "charge now": PIBs join in."""
        brain = _brain(max_charge_w=2400)
        ds = _run(brain, _r(p1=800, setpoint=2400), ticks=10)
        assert ds[-1].target == 2400
        assert _perms(ds) == [("to_full", ["charge_allowed"])]

    def test_setpoint_above_max_counts_as_full_rate(self):
        brain = _brain(max_charge_w=2400)
        ds = _run(brain, _r(p1=800, setpoint=4000), ticks=5)
        assert _perms(ds) == [("to_full", ["charge_allowed"])]

    def test_full_rate_to_partial_charge_drops_pibs_back_to_surplus_only(self):
        brain = _brain(max_charge_w=2400)
        ds = _run(brain, _r(setpoint=2400), ticks=5)
        ds += _run(brain, _r(setpoint=1200), ticks=5, t0=5)
        assert _perms(ds) == [("to_full", ["charge_allowed"]), ("zero", ["charge_allowed"])]


class TestSetpointLimits:
    def test_setpoint_is_clamped_to_zendure_limits(self):
        brain = _brain(max_charge_w=2400, max_discharge_w=2400)
        assert _run(brain, _r(setpoint=4000), ticks=3)[-1].target == 2400
        assert _run(brain, _r(setpoint=-4000), ticks=3, t0=10)[-1].target == -2400

    def test_setpoint_below_pilot_floor_is_a_hold(self):
        brain = _brain()
        ds = _run(brain, _r(setpoint=30), ticks=3)
        assert ds[-1].target == 0
        assert _perms(ds) == [("standby", None)]

    def test_positive_setpoint_at_soc_ceiling_idles_zendure(self):
        brain = _brain(zen_soc_max=100)
        assert _run(brain, _r(zen_soc=100, setpoint=2000), ticks=3)[-1].target == 0

    def test_negative_setpoint_at_soc_floor_idles_zendure(self):
        brain = _brain(zen_soc_min=10)
        assert _run(brain, _r(zen_soc=10, setpoint=-2000), ticks=3)[-1].target == 0


class TestSetpointPrecedence:
    def test_setpoint_is_ignored_without_both_opt_ins(self):
        for kw in ({"allow_grid_charge": True}, {"allow_grid_export": True}, {}):
            brain = PermissionFSM(**kw)
            _run(brain, _r(setpoint=1500), ticks=10)
            assert brain.state != State.SETPOINT, kw

    def test_inhibit_discharge_outranks_a_negative_setpoint(self):
        brain = _brain()
        _run(brain, _r(setpoint=-2000), ticks=5)
        ds = _run(brain, _r(setpoint=-2000, inhibit_discharge=True), ticks=5, t0=5)
        assert brain.state == State.SLEEP
        assert ds[-1].target == 0
        assert _perms(ds) == [("standby", None)]

    def test_inhibit_charge_outranks_a_positive_setpoint(self):
        brain = _brain()
        _run(brain, _r(setpoint=2000), ticks=5)
        ds = _run(brain, _r(setpoint=2000, inhibit_charge=True), ticks=5, t0=5)
        assert brain.state == State.SLEEP
        assert ds[-1].target == 0

    def test_manual_force_charge_outranks_a_negative_setpoint(self):
        brain = _brain()
        _run(brain, _r(setpoint=-2000), ticks=5)
        ds = _run(brain, _r(setpoint=-2000, force_charge=True), ticks=5, t0=5)
        assert brain.state == State.GRID_CHARGE
        assert ds[-1].target == 2400

    def test_manual_force_discharge_outranks_a_positive_setpoint(self):
        brain = _brain()
        _run(brain, _r(setpoint=2000), ticks=5)
        ds = _run(brain, _r(setpoint=2000, force_discharge=True), ticks=5, t0=5)
        assert brain.state == State.FORCE_DISCHARGE
        assert ds[-1].target == -2400


class TestSetpointLifecycle:
    def test_sign_change_reissues_pib_permissions_once(self):
        brain = _brain()
        ds = _run(brain, _r(setpoint=1500), ticks=5)
        ds += _run(brain, _r(setpoint=1200), ticks=5, t0=5)  # same sign: quiet
        ds += _run(brain, _r(setpoint=-1500), ticks=5, t0=10)  # sign flip: re-lock
        assert _perms(ds) == [("zero", ["charge_allowed"]), ("zero", ["discharge_allowed"])]

    def test_setpoint_withdrawn_drops_to_sleep_standby(self):
        brain = _brain()
        _run(brain, _r(setpoint=-1500), ticks=5)
        ds = _run(brain, _r(setpoint=None), ticks=3, t0=5)
        assert brain.state == State.SLEEP
        assert ds[-1].target == 0
        assert _perms(ds) == [("standby", None)]

    def test_no_setpoint_leaves_the_brain_exactly_as_before(self):
        """Feature off: the normal wake into DISCHARGE still happens."""
        brain = _brain()
        _run(brain, _r(p1=1500, zen_soc=80), ticks=60)
        assert brain.state == State.DISCHARGE


def _closed_loop(brain, load_w, ticks, setpoint, t0=0, zen_soc=80, soc_per_kwh=None):
    """Close the P1 loop with PIBs that behave like HomeWizard zero mode.

    The PIBs honour whatever permissions the brain last emitted: they
    discharge into import only with discharge_allowed and charge from
    export only with charge_allowed, up to 800W each. That is exactly the
    behaviour that makes a permission the cross-charge lock.

    Returns (p1, zen_power, pib_total) samples.
    """
    zen = 0.0
    pib = 0.0
    perms: list[str] = []
    samples = []
    for i in range(ticks):
        p1 = load_w + zen + pib
        r = Reading(
            p1=p1, pibs=[pib / 2, pib / 2], pib_socs=[50, 50], zen_power=zen, zen_soc=zen_soc, setpoint=setpoint
        )
        d = brain.decide(r, t=t0 + i)
        samples.append((p1, zen, pib))
        if d.pib_permissions is not None:
            perms = d.pib_permissions
        if d.pib_mode == "standby":
            perms = []
        zen += (d.target - zen) * 0.5
        # Zero-mode controller: move the PIB output by the residual on the
        # meter, but only in a direction the permissions allow.
        want = max(-1600.0, min(1600.0, pib - p1))
        if want < 0 and "discharge_allowed" not in perms:
            want = 0.0
        if want > 0 and "charge_allowed" not in perms:
            want = 0.0
        pib += (want - pib) * 0.5
        if soc_per_kwh is not None:
            zen_soc += (zen / 3600.0 / 1000.0) * soc_per_kwh  # zen<0 = discharging
    return samples


class TestSetpointClosedLoop:
    def test_pibs_never_charge_while_zendure_exports(self):
        brain = _brain()
        samples = _closed_loop(brain, load_w=400, ticks=120, setpoint=-2400)
        assert min(p1 for p1, _, _ in samples[20:]) < -1500  # the export is real
        assert max(pib for _, _, pib in samples) <= 0

    def test_pibs_never_discharge_while_zendure_grid_charges(self):
        brain = _brain()
        samples = _closed_loop(brain, load_w=400, ticks=120, setpoint=2400)
        assert max(p1 for p1, _, _ in samples[20:]) > 2000  # the import is real
        assert min(pib for _, _, pib in samples) >= 0

    def test_no_export_unless_the_setpoint_asks_for_it(self):
        brain = _brain()
        for sp in (0, 1500):
            samples = _closed_loop(brain, load_w=300, ticks=120, setpoint=sp)
            assert all(p1 >= -5 for p1, _, _ in samples), sp

    def test_zero_setpoint_holds_the_pibs_too(self):
        """A hold means the whole fleet keeps its SOC; the house imports."""
        brain = _brain()
        for load in (600, -900):  # deficit and surplus
            samples = _closed_loop(brain, load_w=load, ticks=120, setpoint=0)
            p1, zen, pib = samples[-1]
            assert abs(zen) < 1 and abs(pib) < 1, load
            assert abs(p1 - load) < 1, load

    def test_stuck_negative_setpoint_stops_at_the_soc_floor(self):
        brain = _brain(zen_soc_min=10)
        # 1 kWh drains 100% so the floor is reached within the run.
        samples = _closed_loop(brain, load_w=300, ticks=1500, setpoint=-2400, zen_soc=12, soc_per_kwh=100)
        assert min(zen for _, zen, _ in samples) < -2000  # it did discharge first
        assert abs(samples[-1][1]) < 5  # and stopped at the floor


class TestStopRequestsReachTheDevice:
    """A stop must be sent on the tick it is decided, even when the
    reading looks like one the _should_send carve-outs exist for."""

    def test_inhibit_while_setpoint_charging_from_surplus_is_sent(self):
        brain = _brain()
        _run(brain, _r(p1=-1200, zen_power=1500, setpoint=1500), ticks=10)
        d = brain.decide(_r(p1=-1200, zen_power=1500, setpoint=1500, inhibit_charge=True), t=10)
        assert brain.state == State.SLEEP
        assert d.target == 0 and d.send is True and d.inhibited is True

    def test_inhibit_while_grid_charging_is_sent(self):
        brain = _brain()
        _run(brain, _r(p1=-1200, zen_power=2400, force_charge=True), ticks=10)
        assert brain.state == State.GRID_CHARGE
        d = brain.decide(_r(p1=-1200, zen_power=2400, force_charge=True, inhibit_charge=True), t=10)
        assert brain.state == State.SLEEP
        assert d.target == 0 and d.send is True and d.inhibited is True

    def test_inhibit_while_force_discharging_is_sent(self):
        brain = _brain()
        _run(brain, _r(p1=800, zen_power=-2400, force_discharge=True), ticks=10)
        assert brain.state == State.FORCE_DISCHARGE
        d = brain.decide(_r(p1=800, zen_power=-2400, force_discharge=True, inhibit_discharge=True), t=10)
        assert brain.state == State.SLEEP
        assert d.target == 0 and d.send is True and d.inhibited is True

    def test_entering_setpoint_hold_with_a_running_zendure_sends_immediately(self):
        """Restart at a 0 setpoint while the Zendure is still charging: the
        hold must go out on the entry tick, or the PIBs (now permitted both
        ways) discharge into the Zendure's import."""
        brain = _brain()
        d = brain.decide(_r(p1=1500, zen_power=1500, setpoint=0), t=0)
        assert brain.state == State.SETPOINT
        assert d.target == 0 and d.send is True

    def test_entering_setpoint_reversal_sends_immediately(self):
        brain = _brain()
        d = brain.decide(_r(p1=1500, zen_power=1500, setpoint=-1000), t=0)
        assert brain.state == State.SETPOINT
        assert d.target == -1000 and d.send is True
