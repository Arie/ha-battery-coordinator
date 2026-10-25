"""Curtailment levels: what the brain does when exporting costs money.

HA raises one of two levels on the Reading. `curtail_export` (feed-in is
negative): soak the surplus into the batteries before the panels get
throttled. `import_pays` (buying is paid): the panels go to 0% and the PIBs
go to_full so the house buys as much as it can. Neither may break the
cross-charge lock: PIBs never hold charge_allowed while the Zendure exports.
"""

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent / "battery-coordinator" / "app"))

from brains.permission_fsm import PermissionFSM, State
from coordinator_logic import Reading, pib_max_charge
from curtailer import SolarCurtailer


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
    def test_saturated_under_setpoint_when_pibs_soak_at_their_cap(self):
        brain = _brain()
        _run(brain, _r(setpoint=1500), ticks=5)
        assert brain.state == State.SETPOINT
        assert brain.batteries_saturated(_r(setpoint=1500, pibs=(800, 800), pib_socs=(50, 50))) is True
        assert brain.batteries_saturated(_r(setpoint=1500, pibs=(200, 200), pib_socs=(50, 50))) is False
        assert brain.batteries_saturated(_r(setpoint=1500, pib_socs=(50, 50))) is False
        assert brain.batteries_saturated(_r(setpoint=1500, pib_socs=(99, 99))) is True

    def test_tapering_pibs_with_headroom_are_not_saturated(self):
        """95% still takes 480 W each: idle PIBs there can soak more."""
        brain = _brain()
        _run(brain, _r(setpoint=0), ticks=5)
        assert brain.batteries_saturated(_r(setpoint=0, pibs=(100, 100), pib_socs=(95, 95))) is False
        assert brain.batteries_saturated(_r(setpoint=0, pibs=(480, 480), pib_socs=(95, 95))) is True

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


class TestInhibitChargeBlocksImportPays:
    """A stop request beats a go request: inhibit_charge outranks the
    import-pays to_full override, as it outranks force_charge."""

    def test_inhibit_charge_blocks_import_pays_override(self):
        brain = _brain()
        ds = _run(brain, _r(import_pays=True, inhibit_charge=True), ticks=10)
        assert not any(mode == "to_full" for mode, _ in _perms(ds))
        ds = _run(brain, _r(import_pays=True), ticks=10, t0=10)
        assert _perms(ds) == [("to_full", ["charge_allowed"])]

    def test_inhibit_charge_rising_restores_state_action(self):
        brain = _brain()
        ds = _run(brain, _r(import_pays=True), ticks=5)
        ds += _run(brain, _r(import_pays=True, inhibit_charge=True), ticks=5, t0=5)
        assert _perms(ds) == [("to_full", ["charge_allowed"]), ("standby", None)]


class _CurtailLoop:
    """Close the P1 loop around the brain and the curtailer, 1 s per tick.

    Inverter: solar = min(sun, limit% of 6000 W); a write takes effect the
    tick after it is made. Two PIBs in HomeWizard zero mode move 1/20 of
    the way per tick toward soaking the export, each capped by
    pib_max_charge(soc), and only while the brain's last permissions are
    zero mode with charge_allowed. SOC rises 1% per 27 Wh. The Zendure
    follows the brain's target under a DAO setpoint of 0.
    p1 = load - solar + pib_charge + zen (positive = import).
    """

    RATED_W = 6000.0

    def __init__(self, *, load_w, pib_soc, sun_w):
        self.brain = _brain()
        self.curtailer = SolarCurtailer()
        self.load_w = load_w
        self.sun_w = sun_w
        self.socs = [float(pib_soc), float(pib_soc)]
        self.pibs = [0.0, 0.0]
        self.zen = 0.0
        self.mode: str | None = None
        self.perms: list[str] = []
        self.limit_pct = 100
        self.t = 0
        self.writes: list[tuple[int, int]] = []
        self.samples: list[dict[str, Any]] = []

    def tick(self):
        solar = min(self.sun_w, self.limit_pct / 100 * self.RATED_W)
        p1 = self.load_w - solar + sum(self.pibs) + self.zen
        r = _r(
            p1=p1,
            zen_power=self.zen,
            pibs=tuple(self.pibs),
            pib_socs=tuple(self.socs),
            setpoint=0,
            curtail_export=True,
        )
        d = self.brain.decide(r, t=self.t)
        if d.pib_mode is not None:
            self.mode = d.pib_mode
            self.perms = [] if d.pib_mode == "standby" else list(d.pib_permissions or [])
        c = self.curtailer.decide(
            p1=p1,
            solar_w=solar,
            curtail=True,
            import_pays=False,
            saturated=self.brain.batteries_saturated(r),
            t=self.t,
        )
        self.samples.append(
            {
                "t": self.t,
                "p1": p1,
                "solar": solar,
                "sun": self.sun_w,
                "socs": tuple(self.socs),
                "pib": sum(self.pibs),
                "cap": sum(pib_max_charge(soc) for soc in self.socs),
                "limit": self.limit_pct,
            }
        )
        if c.write is not None:
            self.curtailer.mark_written(c.write, self.t)
            self.writes.append((self.t, c.write))
            self.limit_pct = c.write

        self.zen += (d.target - self.zen) * 0.5
        charging = self.mode == "zero" and "charge_allowed" in self.perms
        share = (sum(self.pibs) - p1) / len(self.pibs)
        for i, soc in enumerate(self.socs):
            want = max(0.0, min(float(pib_max_charge(soc)), share)) if charging else 0.0
            self.pibs[i] += (want - self.pibs[i]) / 20
            self.socs[i] = min(100.0, soc + self.pibs[i] / 3600 / 27)
        self.t += 1

    def run(self, ticks):
        start = len(self.samples)
        for _ in range(ticks):
            self.tick()
        return self.samples[start:]


class TestCurtailClosedLoop:
    def test_soaks_before_throttling(self):
        loop = _CurtailLoop(load_w=500, pib_soc=60, sun_w=4000)
        samples = loop.run(5000)
        # The first minute is the model PIBs' own 1/20-per-tick ramp-in.
        assert all(t >= 60 for t, _ in loop.writes)
        settled = samples[60:]
        over = [s for s in settled if s["sun"] - loop.load_w > s["cap"]]
        under = [s for s in settled if s["sun"] - loop.load_w <= s["cap"]]
        assert over  # the surplus outran the PIBs, so throttling was in play
        starved = [s for s in over if s["pib"] < 0.9 * s["cap"]]
        assert not starved, starved[:3]
        assert all(s["limit"] == 100 for s in under)
        assert any(w < 100 for _, w in loop.writes), "never throttled the excess"

    def test_holds_p1_in_band_once_full(self):
        loop = _CurtailLoop(load_w=500, pib_soc=100, sun_w=4000)
        loop.run(120)
        tail = loop.run(480)
        in_band = [s for s in tail if -90 <= s["p1"] <= 30]
        assert len(in_band) >= 0.9 * len(tail), min(s["p1"] for s in tail)

    def test_bounded_writes_steady_sun(self):
        loop = _CurtailLoop(load_w=500, pib_soc=100, sun_w=4000)
        loop.run(600)
        assert len([t for t, _ in loop.writes if t >= 120]) <= 20, loop.writes

    def test_cloud_recovers(self):
        # Saturated (97%, 240 W each) PIBs still soak, so the throttled
        # panels (~1.7 kW) exceed the clouded sun. The sun then binds, not
        # the limit: raising the limit would only queue up an export burst
        # for when the sun returns, so it must hold.
        loop = _CurtailLoop(load_w=1200, pib_soc=97, sun_w=4000)
        loop.run(240)
        throttled = loop.limit_pct
        assert throttled < 100 and throttled / 100 * loop.RATED_W > 1500
        loop.sun_w = 1500
        window = loop.run(60)
        assert sum(s["p1"] for s in window) / len(window) < 300
        assert all(s["limit"] == throttled for s in window)
        assert loop.limit_pct == throttled
        loop.sun_w = 4000
        after = loop.run(60)
        assert min(s["p1"] for s in after) >= -300
