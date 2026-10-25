"""Solar curtailer: decides the inverter's active power limit (0-100 %)."""

import math
import sys
from itertools import pairwise
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "battery-coordinator" / "app"))

from curtailer import CurtailDecision, SolarCurtailer


def _run(c, seconds, *, t0=0, **kw):
    """Drive decide() at 1 s steps; apply writes. Returns [(t, decision)] for writes."""
    kw.setdefault("p1", 0.0)
    kw.setdefault("solar_w", 3000.0)
    kw.setdefault("curtail", False)
    kw.setdefault("import_pays", False)
    kw.setdefault("saturated", True)
    out = []
    for t in range(t0, t0 + seconds):
        d = c.decide(t=float(t), **kw)
        if d.write is not None:
            c.mark_written(d.write, float(t))
            out.append((t, d))
    return out


def _put(c, limit, t=0.0):
    c.mark_written(limit, t)


def test_startup_without_gate_writes_100_once():
    c = SolarCurtailer()
    w = _run(c, 10)
    assert [(t, d.write, d.reason) for t, d in w] == [(0, 100, "startup")]


def test_gate_off_restores_100_once():
    c = SolarCurtailer()
    _put(c, 60)
    w = _run(c, 10, t0=1)  # min_write_s holds the write back until t=3
    assert [(t, d.write, d.reason) for t, d in w] == [(3, 100, "gate off")]


def test_import_pays_writes_zero_and_outranks_curtail():
    c = SolarCurtailer()
    d = c.decide(p1=-1500, solar_w=3000, curtail=True, import_pays=True, saturated=True, t=0.0)
    assert d == CurtailDecision(0, "import pays")


def test_no_trim_while_not_saturated():
    c = SolarCurtailer()
    _put(c, 100)
    w = _run(c, 120, t0=1, curtail=True, p1=-1500, saturated=False)
    assert w == []


def test_trim_down_after_settle():
    c = SolarCurtailer()
    _put(c, 100)
    w = _run(c, 40, t0=0, curtail=True, p1=-530, solar_w=3000)
    assert w[0][0] == 20
    assert w[0][1].write == 41
    assert w[0][1].reason == "trim down"


def test_trim_up_after_settle():
    c = SolarCurtailer()
    _put(c, 42)
    w = _run(c, 10, t0=0, curtail=True, p1=400, solar_w=3000)
    t, d = w[0]
    assert t >= 5
    assert d.write == math.ceil((3000 + 430) / 60)
    assert d.write > 42
    assert d.reason == "trim up"


def test_deadband_is_quiet():
    c = SolarCurtailer()
    _put(c, 50)
    w = _run(c, 300, t0=1, curtail=True, p1=-60.0, solar_w=3000)
    w += _run(c, 300, t0=301, curtail=True, p1=0.0, solar_w=3000)
    assert all(d.reason == "heartbeat" for _, d in w)
    assert all(d.write == 50 for _, d in w)


def test_min_write_interval():
    c = SolarCurtailer(settle_down_s=0, settle_up_s=0)
    _put(c, 50)
    times = []
    for t in range(1, 60):
        p1 = -800.0 if t % 2 else 800.0
        d = c.decide(p1=p1, solar_w=3000, curtail=True, import_pays=False, saturated=True, t=float(t))
        if d.write is not None:
            c.mark_written(d.write, float(t))
            times.append(t)
    assert len(times) > 3
    assert all(b - a >= 3 for a, b in pairwise(times))


def test_solar_zero_never_trims_down():
    c = SolarCurtailer()
    _put(c, 100)
    w = _run(c, 120, t0=1, curtail=True, p1=-800, solar_w=0)
    assert w == []


def test_cloud_holds_limit_until_inverter_is_held_back():
    """The sun, not the limit, binds under a cloud: raising the limit would
    only queue up an export burst for when the sun returns."""
    c = SolarCurtailer()
    _put(c, 40)  # 2400 W cap
    w = _run(c, 60, t0=1, curtail=True, p1=400, solar_w=1500)
    assert w == []
    w = _run(c, 10, t0=61, curtail=True, p1=400, solar_w=2380)
    assert w and w[0][0] == 61 + c.settle_up_s
    assert w[0][1].reason == "trim up" and w[0][1].write > 40


def test_stale_solar_reading_does_not_block_trim_up():
    """An unreachable solar sensor reads 0: fail open, not held at the limit."""
    c = SolarCurtailer()
    _put(c, 40)
    w = _run(c, 10, t0=1, curtail=True, p1=400, solar_w=0)
    assert w and w[0][0] == 1 + c.settle_up_s
    assert w[0][1].reason == "trim up" and w[0][1].write > 40


def test_sustained_import_trims_up_even_when_not_held_back():
    """solar_rated_w set too high makes the cap look unreached forever; a
    minute of import overrides the held-back test."""
    c = SolarCurtailer(rated_w=6000)
    _put(c, 30)  # 1800 W cap, solar stuck at 1500 (< cap - margin)
    w = _run(c, 120, t0=1, curtail=True, p1=400, solar_w=1500)
    assert w
    assert w[0][0] >= 1 + c.UNHELD_IMPORT_S
    assert w[0][1].reason == "trim up" and w[0][1].write > 30


def test_failed_write_retries_after_min_interval():
    c = SolarCurtailer()
    d = c.decide(p1=0, solar_w=0, curtail=False, import_pays=False, saturated=False, t=10.0)
    assert d.write == 100
    c.mark_write_failed(10.0)
    assert c.limit is None
    for t in (11.0, 12.0):
        assert c.decide(p1=0, solar_w=0, curtail=False, import_pays=False, saturated=False, t=t).write is None
    assert c.decide(p1=0, solar_w=0, curtail=False, import_pays=False, saturated=False, t=13.0).write == 100


def test_gate_flap_writes_100_once_per_off_edge():
    c = SolarCurtailer()
    reasons = []
    t = 0
    for gate in (True, False, True, False):
        for _ in range(5):
            d = c.decide(p1=0, solar_w=0, curtail=False, import_pays=gate, saturated=False, t=float(t))
            if d.write is not None:
                c.mark_written(d.write, float(t))
                reasons.append(d.reason)
            t += 1
    assert reasons.count("gate off") == 2
    assert reasons == ["import pays", "gate off", "import pays", "gate off"]


def test_heartbeat_reasserts_while_gated():
    c = SolarCurtailer()
    _put(c, 0, 0.0)
    assert c.decide(p1=0, solar_w=0, curtail=False, import_pays=True, saturated=False, t=299.0).write is None
    d = c.decide(p1=0, solar_w=0, curtail=False, import_pays=True, saturated=False, t=300.0)
    assert d == CurtailDecision(0, "heartbeat")


def test_startup_with_gate_on_does_not_assume_a_limit():
    c = SolarCurtailer()
    assert c.limit is None
    d = c.decide(p1=0, solar_w=0, curtail=False, import_pays=True, saturated=False, t=0.0)
    assert d.write == 0 and d.reason == "import pays"


def test_give_back_raises_a_low_limit_while_batteries_have_room():
    """P1 parked at ~0 by soaking PIBs never reads as import: give back."""
    c = SolarCurtailer()
    _put(c, 9)  # 540 W cap, panels at it
    w = _run(c, 20, t0=1, curtail=True, p1=0.0, solar_w=540, saturated=False)
    assert w and w[0][0] == 1 + c.settle_up_s
    assert w[0][1] == CurtailDecision(9 + c.GIVE_BACK_STEP_PCT, "give back")


def test_no_give_back_when_saturated_or_exporting():
    c = SolarCurtailer()
    _put(c, 9)
    assert _run(c, 60, t0=1, curtail=True, p1=0.0, solar_w=540, saturated=True) == []
    assert _run(c, 15, t0=61, curtail=True, p1=-40.0, solar_w=540, saturated=False) == []


def test_give_back_clamps_at_100():
    c = SolarCurtailer()
    _put(c, 98)
    w = _run(c, 30, t0=1, curtail=True, p1=0.0, solar_w=5880, saturated=False)
    assert [d.write for _, d in w] == [100]
