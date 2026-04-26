"""Brain handles arbitrary PIB count via Reading.pibs / Reading.pib_socs.

The 2-PIB construction style is tested everywhere else; this file covers
N-PIB scenarios (1 PIB, 3 PIBs, 4 PIBs) so the array refactor doesn't
silently regress when a fleet adds or removes batteries.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "battery-coordinator" / "app"))

from coordinator_logic import Reading
from brains.permission_fsm import PermissionFSM, _total_charge_cap, _all_in_taper


def _r(p1, pibs, pib_socs, *, zen_power=0, zen_soc=50, solar=0):
    return Reading(p1=p1, pibs=pibs, pib_socs=pib_socs,
                   zen_power=zen_power, zen_soc=zen_soc, solar=solar)


class TestReadingConstruction:
    def test_one_pib(self):
        r = _r(p1=0, pibs=[400], pib_socs=[50])
        assert r.pibs == [400]
        assert r.pib_socs == [50]
        assert r.pib_count == 1

    def test_three_pibs(self):
        r = _r(p1=0, pibs=[800, 700, 600], pib_socs=[50, 60, 70])
        assert r.pibs == [800, 700, 600]
        assert r.pib_socs == [50, 60, 70]
        assert r.pib_count == 3

    def test_four_pibs(self):
        r = _r(p1=0, pibs=[800, 700, 600, 500], pib_socs=[50, 55, 60, 65])
        assert r.pibs == [800, 700, 600, 500]
        assert r.pib_socs == [50, 55, 60, 65]
        assert r.pib_count == 4

    def test_mismatched_lengths_rejected(self):
        with pytest.raises(ValueError, match="must be the same length"):
            Reading(p1=0, pibs=[100, 200], pib_socs=[50],
                    zen_power=0, zen_soc=50)


class TestHelpers:
    def test_total_charge_cap_sums_each_pib(self):
        # All <93% SOC → each can charge 800W.
        r = _r(p1=0, pibs=[0, 0, 0, 0], pib_socs=[50, 60, 70, 80])
        assert _total_charge_cap(r) == 4 * 800

    def test_total_charge_cap_with_taper(self):
        # 97% SOC → 240W cap. 50% → 800W. Sum.
        r = _r(p1=0, pibs=[0, 0], pib_socs=[97, 50])
        assert _total_charge_cap(r) == 240 + 800

    def test_all_in_taper_requires_every_pib(self):
        # One PIB at 50% (cap 800) means NOT everyone in taper.
        r = _r(p1=0, pibs=[0, 0, 0], pib_socs=[97, 98, 50])
        assert _all_in_taper(r, threshold=600) is False

    def test_all_in_taper_when_all_above_threshold_soc(self):
        r = _r(p1=0, pibs=[0, 0, 0], pib_socs=[97, 98, 99])
        # All caps (240, 180, 120) < 600 → all in taper.
        assert _all_in_taper(r, threshold=600) is True


class TestBrainWithFourPibs:
    def test_wakes_to_charge_on_p1_export_with_four_pibs(self):
        brain = PermissionFSM()
        for tick in range(20):
            brain.decide(
                _r(p1=-500, pibs=[0, 0, 0, 0], pib_socs=[50, 50, 50, 50],
                   solar=2000, zen_soc=20),
                t=tick,
            )
        assert brain.state.value == "CHARGE"

    def test_partial_taper_across_four_pibs_keeps_stepped_mode(self):
        # 1 PIB tapered, 3 not → not "all in taper" → brain stays stepped.
        brain = PermissionFSM()
        brain.state = brain.state.__class__("CHARGE")
        d = brain.decide(
            _r(p1=-500, pibs=[800, 800, 800, 240],
               pib_socs=[50, 50, 50, 97],
               zen_power=0, zen_soc=50, solar=4000),
            t=0,
        )
        # NOM mode would target near-max charge; stepped mode starts at 0.
        assert d.target == 0

    def test_all_four_pibs_in_taper_switches_to_nom(self):
        brain = PermissionFSM()
        brain.state = brain.state.__class__("CHARGE")
        d = brain.decide(
            _r(p1=-500, pibs=[100, 100, 100, 100],
               pib_socs=[97, 98, 97, 98],  # all in taper (caps 240 / 180)
               zen_power=400, zen_soc=50, solar=2000),
            t=0,
        )
        # NOM: target = zen_power - p1 = 400 - (-500) = 900
        assert d.target == 900

    def test_pib_discharge_exits_when_all_pibs_empty(self):
        brain = PermissionFSM()
        brain.state = brain.state.__class__("PIB_DISCHARGE")
        for tick in range(5):
            brain.decide(
                _r(p1=300, pibs=[0, 0, 0, 0], pib_socs=[0, 0, 0, 0],
                   zen_soc=10),
                t=tick,
            )
        assert brain.state.value == "SLEEP"

    def test_pib_discharge_stays_when_one_of_four_has_charge(self):
        brain = PermissionFSM()
        brain.state = brain.state.__class__("PIB_DISCHARGE")
        for tick in range(5):
            brain.decide(
                _r(p1=300, pibs=[0, 0, 0, -100],
                   pib_socs=[0, 0, 0, 25], zen_soc=10),
                t=tick,
            )
        # PIB 4 still has charge → don't go to SLEEP.
        assert brain.state.value == "PIB_DISCHARGE"
