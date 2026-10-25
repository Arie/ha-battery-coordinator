"""Curtailment levels: what the brain does when exporting costs money.

HA raises one of two levels on the Reading. `curtail_export` (feed-in is
negative): soak the surplus into the batteries before the panels get
throttled. `import_pays` (buying is paid): the panels go to 0% and the PIBs
go to_full so the house buys as much as it can. Neither may break the
cross-charge lock: PIBs never hold charge_allowed while the Zendure exports.
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


def _brain(**kw):
    return PermissionFSM(allow_grid_charge=True, allow_grid_export=True, **kw)


def _run(brain, reading, ticks, t0=0):
    return [brain.decide(reading, t=t0 + i) for i in range(ticks)]


def _perms(decisions):
    return [(d.pib_mode, d.pib_permissions) for d in decisions if d.pib_mode is not None]


DISCHARGE_STATES = (State.DISCHARGE, State.DISCHARGE_HELP, State.PIB_DISCHARGE)


class TestCurtailUnderSetpoint:
    def test_curtail_zero_setpoint_pibs_charge_only(self):
        brain = _brain()
        ds = _run(brain, _r(setpoint=0, curtail_export=True), ticks=10)
        assert brain.state == State.SETPOINT
        assert _perms(ds) == [("zero", ["charge_allowed"])]

    def test_curtail_zero_setpoint_waits_for_zendure_discharge_to_stop(self):
        brain = _brain()
        ds = _run(brain, _r(setpoint=-1500, zen_power=-1500, curtail_export=True), ticks=5)
        ds += _run(brain, _r(setpoint=0, zen_power=-1500, curtail_export=True), ticks=5, t0=5)
        assert _perms(ds) == [("zero", ["discharge_allowed"]), ("standby", None)]
        ds += _run(brain, _r(setpoint=0, zen_power=0, curtail_export=True), ticks=5, t0=10)
        assert _perms(ds)[-1] == ("zero", ["charge_allowed"])

    def test_curtail_negative_setpoint_keeps_discharge_only(self):
        brain = _brain()
        ds = _run(brain, _r(setpoint=-1000, curtail_export=True), ticks=10)
        assert _perms(ds) == [("zero", ["discharge_allowed"])]

    def test_curtail_does_not_touch_the_zendure_target(self):
        plain = _run(_brain(), _r(setpoint=1500), ticks=20)
        curtailed = _run(_brain(), _r(setpoint=1500, curtail_export=True), ticks=20)
        assert [d.target for d in plain] == [d.target for d in curtailed]

    def test_curtail_guard_does_not_cancel_setpoint(self):
        brain = _brain()
        ds = _run(brain, _r(setpoint=-1000, curtail_export=True), ticks=10)
        assert brain.state == State.SETPOINT
        assert ds[-1].target == -1000


class TestImportPays:
    def test_import_pays_sends_pibs_to_full(self):
        brain = _brain()
        ds = _run(brain, _r(setpoint=0, import_pays=True), ticks=10)
        assert _perms(ds) == [("to_full", ["charge_allowed"])]

    def test_import_pays_pibs_standby_while_zendure_exports(self):
        brain = _brain()
        ds = _run(brain, _r(setpoint=-2000, import_pays=True), ticks=10)
        assert _perms(ds) == [("standby", None)]

    def test_import_pays_end_restores_state_pib_action(self):
        for sp, own in ((1500, ("zero", ["charge_allowed"])), (0, ("zero", ["discharge_allowed"]))):
            brain = _brain()
            ds = _run(brain, _r(setpoint=sp, import_pays=True), ticks=10)
            ds += _run(brain, _r(setpoint=sp), ticks=5, t0=10)
            assert _perms(ds) == [("to_full", ["charge_allowed"]), own], sp

    def test_import_pays_end_restores_state_action_without_setpoint(self):
        brain = _brain()
        _run(brain, _r(p1=-1500), ticks=20)
        assert brain.state == State.CHARGE
        ds = _run(brain, _r(p1=-1500, import_pays=True), ticks=5, t0=20)
        ds += _run(brain, _r(p1=-1500), ticks=5, t0=25)
        assert _perms(ds) == [("to_full", ["charge_allowed"]), ("zero", ["charge_allowed"])]

    def test_import_pays_without_setpoint_overrides_any_state(self):
        brain = _brain()
        ds = _run(brain, _r(p1=-1500, import_pays=True), ticks=20)
        assert brain.state == State.CHARGE
        assert _perms(ds) == [("to_full", ["charge_allowed"])]

    def test_heartbeat_reasserts_the_override(self):
        brain = _brain()
        ds = _run(brain, _r(p1=-1500, import_pays=True), ticks=PermissionFSM.PIB_HEARTBEAT_S + 5)
        assert _perms(ds) == [("to_full", ["charge_allowed"])] * 2


class TestNoSetpointDischargeGuard:
    def test_no_setpoint_curtail_blocks_discharge(self):
        for flag in ("curtail_export", "import_pays"):
            brain = _brain()
            for i in range(120):
                d = brain.decide(_r(p1=1500, zen_soc=80, **{flag: True}), t=i)
                assert brain.state not in DISCHARGE_STATES, flag
                assert d.target >= 0, flag

    def test_discharge_already_running_is_stopped(self):
        brain = _brain()
        _run(brain, _r(p1=1500, zen_soc=80), ticks=60)
        assert brain.state == State.DISCHARGE
        brain.decide(_r(p1=1500, zen_soc=80, curtail_export=True), t=60)
        assert brain.state == State.SLEEP


class TestBatteriesSaturated:
    def test_saturated_under_setpoint_needs_tapering_pibs(self):
        brain = _brain()
        _run(brain, _r(setpoint=1500), ticks=5)
        assert brain.state == State.SETPOINT
        assert brain.batteries_saturated(_r(setpoint=1500, pib_socs=(50, 50))) is False
        assert brain.batteries_saturated(_r(setpoint=1500, pib_socs=(99, 99))) is True

    def test_saturated_without_setpoint(self):
        brain = _brain()
        _run(brain, _r(p1=-1500), ticks=20)
        assert brain.state == State.CHARGE
        assert brain.batteries_saturated(_r(zen_soc=100, pib_socs=(100, 100))) is True
        assert brain.batteries_saturated(_r(zen_soc=60, zen_power=400, pib_socs=(100, 100))) is False
        assert brain.batteries_saturated(_r(zen_soc=60, zen_power=2300, pib_socs=(100, 100))) is True
        assert brain.batteries_saturated(_r(zen_soc=100, pib_socs=(50, 100))) is False

    def test_saturated_with_no_pibs(self):
        brain = _brain()
        assert brain.batteries_saturated(_r(zen_soc=100, pibs=(), pib_socs=())) is True


def test_lock_never_charge_while_zendure_exports():
    setpoints = list(range(-2400, 2401, 600))
    sweep = setpoints + setpoints[::-1] + [None, -2400, 0, 2400, -1200, None]
    for curtail in (False, True):
        for import_pays in (False, True):
            brain = _brain()
            zen = 0.0
            perms: list[str] = []
            t = 0
            for sp in sweep:
                for _ in range(15):
                    r = _r(p1=-800, zen_power=zen, setpoint=sp, curtail_export=curtail, import_pays=import_pays)
                    d = brain.decide(r, t=t)
                    t += 1
                    if d.pib_mode is not None:
                        perms = [] if d.pib_mode == "standby" else list(d.pib_permissions or [])
                    if d.target < 0:
                        assert "charge_allowed" not in perms, (sp, curtail, import_pays, brain.state)
                    zen += (d.target - zen) * 0.5
