"""PermissionFSM accepts per-instance tunable overrides."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "battery-coordinator" / "app"))

from brains.permission_fsm import PermissionFSM


class TestDefaults:
    def test_no_kwargs_keeps_class_defaults(self):
        b = PermissionFSM()
        assert b.PIB_HIGH == 1200
        assert b.FLIP_S == 30
        assert b.WAKE_CHARGE_S == 10
        assert b.NOM_DEADBAND == 10
        assert b.P1_EXPORT == -100
        assert b.P1_IMPORT == 200


class TestOverrides:
    def test_each_tunable_can_be_overridden(self):
        b = PermissionFSM(
            pib_high_w=1500,
            pib_maxed_w=1800,
            pib_low_w=300,
            pib_taper_cap_w=700,
            step_holdoff_s=20,
            flip_s=45,
            wake_charge_s=15,
            wake_discharge_s=45,
            help_enter_s=20,
            help_exit_s=25,
            nom_deadband_w=20,
            p1_export_w=-150,
            p1_import_w=250,
        )
        assert b.PIB_HIGH == 1500
        assert b.PIB_MAXED == 1800
        assert b.PIB_LOW == 300
        assert b.PIB_TAPER_CAP == 700
        assert b.STEP_HOLDOFF == 20
        assert b.FLIP_S == 45
        assert b.WAKE_CHARGE_S == 15
        assert b.WAKE_DISCHARGE_S == 45
        assert b.HELP_ENTER_S == 20
        assert b.HELP_EXIT_S == 25
        assert b.NOM_DEADBAND == 20
        assert b.P1_EXPORT == -150
        assert b.P1_IMPORT == 250

    def test_override_does_not_leak_across_instances(self):
        a = PermissionFSM(pib_high_w=1500)
        b = PermissionFSM()
        assert a.PIB_HIGH == 1500
        assert b.PIB_HIGH == 1200  # class default unchanged

    def test_override_propagates_into_transition_table(self):
        # WAKE_CHARGE_S is baked into a Transition object at __init__,
        # so the override has to take effect before the table is built.
        b = PermissionFSM(wake_charge_s=99)
        sleep_transitions = b._transitions[b.state.__class__("SLEEP")]
        # Find the "Normal wake to CHARGE" transition (the one with WAKE_CHARGE_S holdoff).
        wake_holdoffs = [t.holdoff_s for t, _ in sleep_transitions if t.holdoff_s > 0]
        assert 99 in wake_holdoffs
