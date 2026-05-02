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

    def test_class_constants_match_DEFAULTS(self):
        # Both Config() and bare PermissionFSM() pull from DEFAULTS — if a
        # class constant drifts from the dict, the two paths produce
        # different brains. Pin them together.
        d = PermissionFSM.DEFAULTS
        b = PermissionFSM()
        assert b.PIB_HIGH == d["pib_high_w"]
        assert b.PIB_MAXED == d["pib_maxed_w"]
        assert b.PIB_LOW == d["pib_low_w"]
        assert b.PIB_TAPER_CAP == d["pib_taper_cap_w"]
        assert b.STEP_HOLDOFF == d["step_holdoff_s"]
        assert b.FLIP_S == d["flip_s"]
        assert b.WAKE_CHARGE_S == d["wake_charge_s"]
        assert b.WAKE_DISCHARGE_S == d["wake_discharge_s"]
        assert b.HELP_ENTER_S == d["help_enter_s"]
        assert b.HELP_EXIT_S == d["help_exit_s"]
        assert b.NOM_DEADBAND == d["nom_deadband_w"]
        assert b.P1_EXPORT == d["p1_export_w"]
        assert b.P1_IMPORT == d["p1_import_w"]


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


class TestP1ExportSemanticIsolated:
    """p1_export_w controls grid-export detection only. PIB-discharge
    detection (used in startup guards) lives on a separate constant so
    tuning the export threshold doesn't accidentally retune the PIB
    direction-detection threshold."""

    def test_overriding_p1_export_does_not_change_pib_discharge_detect(self):
        a = PermissionFSM()
        b = PermissionFSM(p1_export_w=-300)
        assert a.PIB_DISCHARGE_DETECT == b.PIB_DISCHARGE_DETECT, (
            "Tuning p1_export_w changed PIB_DISCHARGE_DETECT. The two have "
            "different meanings (grid-side vs battery-side) and should not "
            "be coupled by accident."
        )

    def test_p1_export_override_takes_effect(self):
        b = PermissionFSM(p1_export_w=-300)
        assert b.P1_EXPORT == -300


class TestMarkSent:
    """mark_sent() owns last_ac_mode bookkeeping; callers shouldn't have to
    reset it manually."""

    def test_target_zero_clears_last_ac_mode(self):
        # After a charge, brain knows the relay is in charge mode.
        b = PermissionFSM()
        b.mark_sent(800, t=10)
        assert b.last_ac_mode == PermissionFSM.AC_CHARGE

        # Standby comes next; relay sits between modes. The next non-zero
        # send must trigger a mode-switch command, which only happens if
        # last_ac_mode is not equal to the new direction.
        b.mark_sent(0, t=20)
        assert b.last_ac_mode is None, (
            "After target=0 (standby), brain should forget the relay's "
            "previous direction so the next charge/discharge re-asserts "
            "the mode-switch command."
        )

    def test_explicit_ac_mode_overrides_sign(self):
        b = PermissionFSM()
        b.mark_sent(0, t=10, ac_mode=PermissionFSM.AC_DISCHARGE)
        assert b.last_ac_mode == PermissionFSM.AC_DISCHARGE
