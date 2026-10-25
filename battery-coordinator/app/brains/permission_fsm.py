"""PermissionFSM: FSM-based brain using PIB permissions to prevent lockout.

State transitions are defined as data in a transition table instead of
scattered if/elif blocks; each Transition owns its own holdoff timer
and entry-action (pib_mode + pib_permissions).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar

from coordinator_logic import PILOT_W, Decision, Reading, pib_max_charge


def _total_charge_cap(r: Reading) -> int:
    """Sum of every configured PIB's current max charge rate."""
    return sum(pib_max_charge(s) for s in r.pib_socs)


def _p1_without_pibs(r: Reading) -> float:
    """What P1 would read if the PIBs went to standby right now.

    PIB power is negative while discharging, so removing their share adds
    it back to the grid reading. In DISCHARGE_HELP the PIBs run the HW
    zero-mode controller, whose whole job is to drive raw P1 to ~0 — raw
    P1 therefore says nothing about whether they are still needed, and
    reading it as "load dropped" switches off the very thing holding the
    grid at zero. This is the signal that answers the question.
    """
    return r.p1 - sum(r.pibs)


def _all_in_taper(r: Reading, threshold: int) -> bool:
    """All PIBs are in their taper zone (per-PIB charge cap below threshold)."""
    return bool(r.pib_socs) and all(pib_max_charge(s) < threshold for s in r.pib_socs)


class State(Enum):
    SLEEP = "SLEEP"
    CHARGE = "CHARGE"
    GRID_CHARGE = "GRID_CHARGE"
    FORCE_DISCHARGE = "FORCE_DISCHARGE"
    SETPOINT = "SETPOINT"
    DISCHARGE = "DISCHARGE"
    DISCHARGE_HELP = "DISCHARGE_HELP"
    PIB_DISCHARGE = "PIB_DISCHARGE"


@dataclass
class Transition:
    """A possible state transition with guard, holdoff, and entry actions."""

    to: State
    holdoff_s: float  # 0 = immediate
    pib_mode: str | None = None  # "standby", "zero", or None
    pib_permissions: list[str] | None = None

    # Set by the FSM at runtime — not part of the definition
    _since: float | None = field(default=None, init=False, repr=False)

    def reset(self) -> None:
        self._since = None


class PermissionFSM:
    """FSM brain with explicit transition table.

    States:
      SLEEP          – all standby
      CHARGE         – PIBs zero (charge-only), Zendure at fixed step or NOM
      GRID_CHARGE    – deliberate grid import (opt-in), PIBs to_full
      FORCE_DISCHARGE– deliberate max discharge into export (opt-in)
      SETPOINT       – Zendure follows an external wattage (opt-in), PIBs
                       permissioned by its sign so they cannot fight it
      DISCHARGE      – Zendure NOM-tracks P1 solo, PIBs standby
      DISCHARGE_HELP – Zendure maxed, PIBs helping (discharge-only)
      PIB_DISCHARGE  – Zendure off, PIBs discharge alone
    """

    AC_CHARGE = 1
    AC_DISCHARGE = 2

    # Zendure stepped power levels (charge mode)
    ZEN_STEPS: ClassVar[list[int]] = [PILOT_W, 200, 400, 800, 1200, 1600, 2000, 2400]

    # PIB saturation thresholds for stepping. Defaults live in DEFAULTS so
    # config.py and the bare PermissionFSM() constructor see the same values.
    DEFAULTS: ClassVar[dict[str, int]] = {
        "step_holdoff_s": 15,
        "flip_s": 30,
        "wake_charge_s": 10,
        "wake_discharge_s": 30,
        "help_enter_s": 15,
        "help_exit_s": 15,
        "pib_high_w": 1200,
        "pib_maxed_w": 1400,
        "pib_low_w": 200,
        "pib_taper_cap_w": 300,
        "nom_deadband_w": 10,
        "p1_export_w": -100,
        "p1_import_w": 200,
    }

    PIB_MAXED = DEFAULTS["pib_maxed_w"]
    PIB_HIGH = DEFAULTS["pib_high_w"]
    PIB_LOW = DEFAULTS["pib_low_w"]

    # Step timing
    STEP_HOLDOFF = DEFAULTS["step_holdoff_s"]
    STEP_HOLDOFF_FAST = 5  # seconds before fast step-up (PIBs maxed)
    STEP_DOWN_COOLDOWN = 30  # seconds after step-up before allowing step-down

    # Transition holdoffs
    WAKE_CHARGE_S = DEFAULTS["wake_charge_s"]
    WAKE_DISCHARGE_S = DEFAULTS["wake_discharge_s"]
    FLIP_S = DEFAULTS["flip_s"]
    HELP_ENTER_S = DEFAULTS["help_enter_s"]
    HELP_EXIT_S = DEFAULTS["help_exit_s"]

    # Heartbeats — re-assert desired state so silent drift (failed PUT,
    # external app toggle, integration refresh) self-heals within a bounded
    # window. Idempotent commands; safe to repeat.
    PIB_HEARTBEAT_S = 300  # re-send pib_mode/permissions every 5 min
    ZEN_HEARTBEAT_S = 30  # re-send Zen target every 30s (active states)
    ZEN_IDLE_HEARTBEAT_S = 300  # SLEEP/PIB_DISCHARGE: device is in flash
    # standby which persists across reboots — no need to poke it every 30s

    # P1 thresholds
    P1_EXPORT = DEFAULTS["p1_export_w"]
    P1_IMPORT = DEFAULTS["p1_import_w"]
    P1_OVER_DISCHARGE = -100  # P1 below this in DISCHARGE_HELP = load dropped

    # Combined-PIB-power thresholds for startup detection. Independent from
    # P1_EXPORT so tuning the export threshold doesn't accidentally also
    # change PIB direction detection — they're different signals (grid-side
    # vs battery-side) that happened to share a magnitude.
    PIB_CHARGE_DETECT = 100  # combined PIB > this on startup → adopt CHARGE
    PIB_DISCHARGE_DETECT = -100  # combined PIB < this on startup → adopt DISCHARGE

    # Zendure capacity thresholds
    ZEN_MAXED_FRAC = 0.95  # Zendure above this fraction of max → considered maxed
    ZEN_HELP_EXIT_FRAC = 0.8  # total discharge below this fraction → PIBs redundant

    # PIB taper
    PIB_TAPER_CAP = DEFAULTS["pib_taper_cap_w"]

    # NOM send deadband
    NOM_DEADBAND = DEFAULTS["nom_deadband_w"]

    # Stuck detection — Zen accepts commands but produces no output
    ZEN_STUCK_S = 60

    # Hysteresis on the discharge hold-off. Demand has to climb this far
    # above PILOT_W before discharging resumes, so a load hovering at the
    # floor doesn't flip the target every tick.
    EXPORT_HYST_W = 25

    def __init__(
        self,
        max_charge_w: int = 2400,
        max_discharge_w: int = 2400,
        zen_soc_max: int = 100,
        zen_soc_min: int = 10,
        # Opt-in escape hatch from the "never charge from the grid" rule.
        # That rule is correct under net metering, where a grid-charged
        # kWh can only ever be resold at a loss. On a dynamic tariff a
        # cheap hour is worth banking against an expensive one, so the
        # rule becomes a policy choice — but it stays off by default and
        # still requires an explicit per-tick force_charge signal.
        allow_grid_charge: bool = False,
        # Opt-in escape hatch from the "never discharge into export" rule.
        # Distinct from allow_grid_charge, which gates import — these are two
        # different safety rules and conflating them would let one opt-out
        # silently enable the other.
        allow_grid_export: bool = False,
        # Brain tuning — exposed via add-on options. Defaults match the
        # production-tested class constants above; instance attributes
        # below shadow those constants so per-instance overrides work
        # without touching any of the existing self.X references.
        step_holdoff_s: int | None = None,
        flip_s: int | None = None,
        wake_charge_s: int | None = None,
        wake_discharge_s: int | None = None,
        help_enter_s: int | None = None,
        help_exit_s: int | None = None,
        pib_high_w: int | None = None,
        pib_maxed_w: int | None = None,
        pib_low_w: int | None = None,
        pib_taper_cap_w: int | None = None,
        nom_deadband_w: int | None = None,
        p1_export_w: int | None = None,
        p1_import_w: int | None = None,
    ):
        self.max_charge = max_charge_w
        self.max_discharge = max_discharge_w
        self.zen_soc_max = zen_soc_max
        self.zen_soc_min = zen_soc_min
        self.allow_grid_charge = allow_grid_charge
        self.allow_grid_export = allow_grid_export
        # The wattage SETPOINT is currently following, already clamped.
        self._setpoint_w: int = 0
        # The PIB (mode, permissions) SETPOINT last emitted, so a change of
        # band re-issues them and an unchanged band stays quiet.
        self._setpoint_pib: tuple[str, list[str] | None] | None = None
        # One-shot: the next _should_send must go out regardless of deltas.
        self._force_send: bool = False

        # Per-instance overrides for the tunables. None = use class default.
        if step_holdoff_s is not None:
            self.STEP_HOLDOFF = step_holdoff_s
        if flip_s is not None:
            self.FLIP_S = flip_s
        if wake_charge_s is not None:
            self.WAKE_CHARGE_S = wake_charge_s
        if wake_discharge_s is not None:
            self.WAKE_DISCHARGE_S = wake_discharge_s
        if help_enter_s is not None:
            self.HELP_ENTER_S = help_enter_s
        if help_exit_s is not None:
            self.HELP_EXIT_S = help_exit_s
        if pib_high_w is not None:
            self.PIB_HIGH = pib_high_w
        if pib_maxed_w is not None:
            self.PIB_MAXED = pib_maxed_w
        if pib_low_w is not None:
            self.PIB_LOW = pib_low_w
        if pib_taper_cap_w is not None:
            self.PIB_TAPER_CAP = pib_taper_cap_w
        if nom_deadband_w is not None:
            self.NOM_DEADBAND = nom_deadband_w
        if p1_export_w is not None:
            self.P1_EXPORT = p1_export_w
        if p1_import_w is not None:
            self.P1_IMPORT = p1_import_w

        self.last_sent_target: int | None = None
        self.last_send_time: float = -999
        self.last_ac_mode: int | None = None

        # Desired PIB state — set on transitions, re-asserted by heartbeat.
        # None until the first transition fires (no opinion yet → no sends).
        self._pib_mode_desired: str | None = None
        self._pib_perms_desired: list[str] | None = None
        self._pib_send_t: float = -999
        # import_pays was overriding the PIB action last tick, so its end
        # can re-issue the state's own action once.
        self._import_pays_active: bool = False

        self.state = State.SLEEP
        self._zen_step_idx = 0
        self._pib_high_since: float | None = None
        self._pib_low_since: float | None = None
        self._p1_contradict_since: float | None = None
        self._last_step_up_t: float = -999
        self._last_zen_power: float = 0
        self._last_p1: float = 0
        self._last_zen_soc: float = 0
        # Tracks whether the previous CHARGE-state tick was in NOM mode,
        # so the stepped→NOM boundary can ramp instead of jumping.
        self._charge_was_nom: bool = False
        # Stuck detection: Zen accepts commands but produces no output.
        # Tracked across states so DISCHARGE↔DISCHARGE_HELP bouncing
        # doesn't reset the timer.
        self._zen_stuck_since: float | None = None
        self._zen_is_stuck: bool = False
        # DISCHARGE is holding at zero because demand is below the pilot
        # floor. Latched so the resume threshold can sit higher than the
        # stop threshold.
        self._discharge_held_off: bool = False

        # --- Transition table ---
        # Each state maps to a list of transitions, checked in order.
        # First matching guard wins. Transitions own their holdoff timers.
        Guard = Callable[[Reading, float], bool]
        self._transitions: dict[State, list[tuple[Transition, Guard]]] = {
            State.SLEEP: [
                # Startup: detect hardware already active. 2s holdoff so a
                # single noisy reading at process start can't bypass the
                # WAKE_*_S guards below — sustained agreement only.
                (
                    Transition(State.CHARGE, holdoff_s=2, pib_mode="zero", pib_permissions=["charge_allowed"]),
                    lambda r, _: r.zen_power > PILOT_W,
                ),
                (Transition(State.DISCHARGE, holdoff_s=2, pib_mode="standby"), lambda r, _: r.zen_power < -PILOT_W),
                (
                    Transition(State.CHARGE, holdoff_s=2, pib_mode="zero", pib_permissions=["charge_allowed"]),
                    lambda r, _: sum(r.pibs) > self.PIB_CHARGE_DETECT and r.p1 < self.P1_EXPORT * 2,
                ),
                (
                    Transition(State.DISCHARGE, holdoff_s=2, pib_mode="standby"),
                    lambda r, _: (
                        sum(r.pibs) < self.PIB_DISCHARGE_DETECT
                        and abs(r.zen_power) <= PILOT_W
                        and r.zen_soc > self.zen_soc_min
                    ),
                ),
                # Restart while Zen drained + PIBs covering load → adopt
                # PIB_DISCHARGE so the heartbeat doesn't force-stop them.
                (
                    Transition(
                        State.PIB_DISCHARGE, holdoff_s=2, pib_mode="zero", pib_permissions=["discharge_allowed"]
                    ),
                    lambda r, _: (
                        sum(r.pibs) < self.PIB_DISCHARGE_DETECT
                        and abs(r.zen_power) <= PILOT_W
                        and r.zen_soc <= self.zen_soc_min
                    ),
                ),
                # Normal wake
                (
                    Transition(
                        State.CHARGE, holdoff_s=self.WAKE_CHARGE_S, pib_mode="zero", pib_permissions=["charge_allowed"]
                    ),
                    lambda r, _: (
                        r.p1 < self.P1_EXPORT and (r.zen_soc < self.zen_soc_max - 1 or _total_charge_cap(r) > 200)
                    ),
                ),
                (
                    Transition(State.DISCHARGE, holdoff_s=self.WAKE_DISCHARGE_S, pib_mode="standby"),
                    lambda r, _: (
                        r.p1 > self.P1_IMPORT and (r.zen_soc > self.zen_soc_min or any(s > 1 for s in r.pib_socs))
                    ),
                ),
            ],
            State.CHARGE: [
                # Near-full + P1 not importing → sleep. Subsumes literal
                # 100/100 (which strict cap==0 required) and catches taper
                # noise (PIBs at 99% with 120W cap each) so PIBs aren't
                # left awake in zero-mode burning idle.
                (
                    Transition(State.SLEEP, holdoff_s=self.FLIP_S, pib_mode="standby"),
                    lambda r, _: (
                        all(s >= 99 for s in r.pib_socs) and r.zen_soc >= self.zen_soc_max - 1 and r.p1 < self.P1_IMPORT
                    ),
                ),
                # Surplus gone → discharge. In stepped mode, suppressed while
                # step > 0 (Zen charge is causing the import — step down
                # instead of relay-click). In NOM mode at the floor
                # (_charge_was_nom), step_idx is stale from before NOM took
                # over — bypass both pib_abs and step_idx checks; only
                # sustained P1 import matters.
                (
                    Transition(State.DISCHARGE, holdoff_s=self.FLIP_S, pib_mode="standby"),
                    lambda r, pib_abs: (
                        (pib_abs < 50 or self._charge_was_nom)
                        and r.p1 > abs(self.P1_EXPORT)
                        and (self._zen_step_idx == 0 or self._charge_was_nom)
                    ),
                ),
            ],
            State.DISCHARGE: [
                # Solar returns → charge
                (
                    Transition(
                        State.CHARGE, holdoff_s=self.FLIP_S, pib_mode="zero", pib_permissions=["charge_allowed"]
                    ),
                    lambda r, _: r.p1 < self.P1_EXPORT,
                ),
                # Zendure empty, PIBs can help → PIB_DISCHARGE
                (
                    Transition(
                        State.PIB_DISCHARGE, holdoff_s=0, pib_mode="zero", pib_permissions=["discharge_allowed"]
                    ),
                    lambda r, _: r.zen_soc <= self.zen_soc_min and any(s > 1 for s in r.pib_socs),
                ),
                # Zendure empty, PIBs empty → sleep with full standby.
                (
                    Transition(State.SLEEP, holdoff_s=0, pib_mode="standby"),
                    lambda r, _: r.zen_soc <= self.zen_soc_min and all(s <= 1 for s in r.pib_socs),
                ),
                # Zendure unresponsive — commands landing but no output.
                # Uses _zen_is_stuck (tracked outside transition table so
                # DISCHARGE↔DISCHARGE_HELP bouncing doesn't reset the timer).
                (
                    Transition(
                        State.PIB_DISCHARGE, holdoff_s=0, pib_mode="zero", pib_permissions=["discharge_allowed"]
                    ),
                    lambda r, _: self._zen_is_stuck and any(s > 1 for s in r.pib_socs),
                ),
                # Zendure maxed → wake PIBs to help
                (
                    Transition(
                        State.DISCHARGE_HELP,
                        holdoff_s=self.HELP_ENTER_S,
                        pib_mode="zero",
                        pib_permissions=["discharge_allowed"],
                    ),
                    lambda r, _: (
                        abs(int(r.zen_power - r.p1)) >= self.max_discharge * self.ZEN_MAXED_FRAC
                        and r.p1 > abs(self.P1_EXPORT)
                    ),
                ),
            ],
            State.DISCHARGE_HELP: [
                # Every exit from HELP is judged on _p1_without_pibs, never
                # raw P1. The PIBs are actively regulating raw P1 to zero
                # here, so raw P1 measures their success, not the load.
                # Solar returns → charge
                (
                    Transition(
                        State.CHARGE, holdoff_s=self.FLIP_S, pib_mode="zero", pib_permissions=["charge_allowed"]
                    ),
                    lambda r, _: _p1_without_pibs(r) < self.P1_EXPORT,
                ),
                # Zendure can handle alone → back to solo
                # Over-discharging, load dropped → back to NOM solo.
                # 3s holdoff filters the 1-2 tick PIB activation transient
                # (HW P1 controller slams 0→max in one tick on entry, briefly
                # overshooting load by ~1.6kW). Real load drops still exit,
                # just 3s later instead of instantly.
                (
                    Transition(State.DISCHARGE, holdoff_s=3, pib_mode="standby"),
                    lambda r, _: _p1_without_pibs(r) < self.P1_OVER_DISCHARGE,
                ),
                # Slower exit: total household demand has fallen far enough
                # that the Zendure covers it alone with headroom. Demand is
                # what the Zendure is supplying plus what the grid would
                # still have to deliver without the PIBs — NOT combined
                # battery output, which counts the PIBs' own over-discharge
                # as if it were load and so never fired while they helped.
                (
                    Transition(State.DISCHARGE, holdoff_s=self.HELP_EXIT_S, pib_mode="standby"),
                    lambda r, _: abs(r.zen_power) + _p1_without_pibs(r) < self.max_discharge * self.ZEN_HELP_EXIT_FRAC,
                ),
                # Zendure empty → PIBs take over
                (
                    Transition(
                        State.PIB_DISCHARGE, holdoff_s=0, pib_mode="zero", pib_permissions=["discharge_allowed"]
                    ),
                    lambda r, _: r.zen_soc <= self.zen_soc_min,
                ),
                # Zendure unresponsive (same as DISCHARGE above)
                (
                    Transition(
                        State.PIB_DISCHARGE, holdoff_s=0, pib_mode="zero", pib_permissions=["discharge_allowed"]
                    ),
                    lambda r, _: self._zen_is_stuck and any(s > 1 for s in r.pib_socs),
                ),
            ],
            # GRID_CHARGE has no table entries: both its entry and its exit
            # are driven by the force_charge gate in decide(), which runs
            # ahead of the table. Leaving it empty keeps the two mechanisms
            # from disagreeing about who owns the state.
            State.GRID_CHARGE: [],
            # Same reasoning as GRID_CHARGE: gate-owned, so no table entries.
            # Keeping it empty is also what exempts it from the pilot-floor
            # hold-off and the DISCHARGE->CHARGE flip, both of which exist to
            # stop exactly the export this state is for.
            State.FORCE_DISCHARGE: [],
            State.SETPOINT: [],
            State.PIB_DISCHARGE: [
                # Solar returns → charge
                (
                    Transition(
                        State.CHARGE, holdoff_s=self.FLIP_S, pib_mode="zero", pib_permissions=["charge_allowed"]
                    ),
                    lambda r, _: r.p1 < self.P1_EXPORT,
                ),
                # PIBs empty → sleep with full standby. 3s holdoff so PIB
                # power sensor noise (briefly reading 12W when it should be
                # 0) doesn't keep us pinned out of SLEEP; we want sustained
                # near-zero power before committing.
                # Sunrise wake goes through SLEEP→CHARGE (WAKE_CHARGE_S
                # holdoff); a few seconds of leak-back trades for hours of
                # standby savings.
                (
                    Transition(State.SLEEP, holdoff_s=3, pib_mode="standby"),
                    lambda r, _: all(abs(p) < 10 for p in r.pibs) and all(s <= 1 for s in r.pib_socs),
                ),
            ],
        }

    # --- Step logic ---

    def _current_step(self) -> int:
        return self.ZEN_STEPS[self._zen_step_idx]

    def _step_up(self) -> bool:
        if self._zen_step_idx < len(self.ZEN_STEPS) - 1:
            self._zen_step_idx += 1
            self._pib_high_since = None
            self._pib_low_since = None
            return True
        return False

    def _step_down(self) -> bool:
        if self._zen_step_idx > 0:
            self._zen_step_idx -= 1
            self._pib_high_since = None
            self._pib_low_since = None
            return True
        return False

    def _jump_to_step(self, target_w: int) -> bool:
        target_w = abs(target_w)
        old_idx = self._zen_step_idx
        for i in range(len(self.ZEN_STEPS) - 1, -1, -1):
            if self.ZEN_STEPS[i] <= target_w:
                self._zen_step_idx = i
                break
        return self._zen_step_idx != old_idx

    def _update_step(self, pib_abs: float, t: float, p1: float = 0, pib_cap_now: float = 0) -> None:
        # PIBs saturated if absolute power exceeds fixed threshold OR they're
        # near their current capacity (which shrinks hard in taper zones).
        # Without the ratio check, one PIB at 98% taper + the other maxed
        # reads ~1040W total — below PIB_HIGH — so step-up never fires even
        # while P1 exports heavily.
        cap_saturated = pib_cap_now > 200 and pib_abs >= pib_cap_now * 0.85
        if pib_abs > self.PIB_HIGH or cap_saturated:
            holdoff = self.STEP_HOLDOFF_FAST if pib_abs > self.PIB_MAXED else self.STEP_HOLDOFF
            if self._pib_high_since is None:
                self._pib_high_since = t
            elif t - self._pib_high_since >= holdoff:
                if pib_abs > self.PIB_MAXED and abs(p1) > 200:
                    jumped = self._jump_to_step(self._current_step() + int(abs(p1)))
                else:
                    jumped = self._step_up()
                if jumped:
                    self._last_step_up_t = t
                    self._pib_high_since = None
                    self._pib_low_since = None
        else:
            self._pib_high_since = None

        # Debounce P1 contradiction. A 1-tick spike (e.g. brief load-on
        # during charge) is sensor noise, not a real signal — without
        # debouncing, it shortens the step-down holdoff from 15s to 5s
        # and triggers spurious fast step-down. Require 3s of sustained
        # contradiction before treating it as real.
        p1_contradicts_now = (self.state == State.DISCHARGE and p1 < -200) or (self.state == State.CHARGE and p1 > 200)
        if p1_contradicts_now:
            if self._p1_contradict_since is None:
                self._p1_contradict_since = t
            p1_contradicts = (t - self._p1_contradict_since) >= 3
        else:
            self._p1_contradict_since = None
            p1_contradicts = False

        cooldown_ok = p1_contradicts or (t - self._last_step_up_t) >= self.STEP_DOWN_COOLDOWN
        if pib_abs < self.PIB_LOW and cooldown_ok:
            if self._pib_low_since is None:
                self._pib_low_since = t
            elif t - self._pib_low_since >= (self.STEP_HOLDOFF_FAST if p1_contradicts else self.STEP_HOLDOFF):
                if p1_contradicts and p1 > 200:
                    self._jump_to_step(max(PILOT_W, self._current_step() - int(p1)))
                    self._pib_low_since = None
                    self._pib_high_since = None
                else:
                    self._step_down()
        else:
            self._pib_low_since = None

    # --- Tariff gates ---

    # Which inhibit flag, if any, forbids each state. States absent from
    # this map (SLEEP) are never inhibited — standby is always allowed.
    _INHIBITED_BY: ClassVar[dict[State, str]] = {
        State.CHARGE: "inhibit_charge",
        State.GRID_CHARGE: "inhibit_charge",
        State.FORCE_DISCHARGE: "inhibit_discharge",
        State.DISCHARGE: "inhibit_discharge",
        State.DISCHARGE_HELP: "inhibit_discharge",
        State.PIB_DISCHARGE: "inhibit_discharge",
    }

    _DISCHARGE_STATES: ClassVar[frozenset[State]] = frozenset(
        {State.DISCHARGE, State.DISCHARGE_HELP, State.PIB_DISCHARGE}
    )

    # The PIB (mode, permissions) each state's entry transitions emit, so
    # the action can be re-issued when the import-pays override lifts.
    # SETPOINT is absent: its PIB action depends on the setpoint band.
    _STATE_PIB: ClassVar[dict[State, tuple[str, list[str] | None]]] = {
        State.SLEEP: ("standby", None),
        State.CHARGE: ("zero", ["charge_allowed"]),
        State.DISCHARGE: ("standby", None),
        State.DISCHARGE_HELP: ("zero", ["discharge_allowed"]),
        State.PIB_DISCHARGE: ("zero", ["discharge_allowed"]),
        State.GRID_CHARGE: ("to_full", ["charge_allowed"]),
        State.FORCE_DISCHARGE: ("zero", ["discharge_allowed"]),
    }

    def _is_inhibited(self, state: State, r: Reading) -> bool:
        """True if an external gate forbids this state on this reading."""
        flag = self._INHIBITED_BY.get(state)
        if flag is not None and bool(getattr(r, flag)):
            return True
        # While exporting costs money (or buying pays), discharging into
        # the house only pushes more solar out to the grid. Kept off
        # inhibit_discharge on purpose: that flag cancels a DAO setpoint in
        # _setpoint_for, and this guard must leave a setpoint alone.
        return state in self._DISCHARGE_STATES and r.setpoint is None and (r.curtail_export or r.import_pays)

    def batteries_saturated(self, r: Reading) -> bool:
        """True when neither battery system can soak more surplus right now.

        The curtailment controller throttles the panels only once this is
        True: an export the batteries could still absorb is free storage,
        not a cost. Under a setpoint the Zendure follows the optimiser and
        cannot take more, so only the PIBs count.
        """
        zen_full = (
            self.state == State.SETPOINT
            or r.zen_soc >= self.zen_soc_max
            or r.zen_power >= self.max_charge * self.ZEN_MAXED_FRAC
        )
        pibs_full = not r.pib_socs or _all_in_taper(r, self.PIB_TAPER_CAP)
        return zen_full and pibs_full

    def _zendure_exports(self, r: Reading) -> bool:
        """The Zendure is, or is about to be, discharging."""
        return (
            self.state == State.FORCE_DISCHARGE
            or (self.state == State.SETPOINT and self._setpoint_w < 0)
            or r.zen_power < -PILOT_W
        )

    def _import_pays_pib(self, r: Reading) -> tuple[str, list[str] | None]:
        """PIB action while buying pays: fill from the grid, never from us.

        to_full charges regardless of P1, so it would charge from the
        Zendure's own export too; standby is the cross-charge lock here.
        """
        if self._zendure_exports(r):
            return "standby", None
        return "to_full", ["charge_allowed"]

    def _wants_grid_charge(self, r: Reading) -> bool:
        """Deliberate grid import requested, permitted, and still useful.

        Three independent conditions, all required: the operator opted in
        at config time, HA is asserting the signal right now, and there is
        somewhere to put the energy. inhibit_charge outranks force_charge —
        contradictory gates resolve to the conservative one so a stuck-on
        automation can never out-argue a stop request.
        """
        if not (self.allow_grid_charge and r.force_charge):
            return False
        if r.inhibit_charge:
            return False
        room = r.zen_soc < self.zen_soc_max or any(s < 99 for s in r.pib_socs)
        return room

    def _wants_force_discharge(self, r: Reading) -> bool:
        """Deliberate max-rate discharge requested, permitted, and possible.

        inhibit_discharge outranks it, mirroring inhibit_charge vs
        force_charge: a stop request always beats a go request. The SOC floor
        is checked here as well as in decide()'s clamp so the state is never
        entered with nothing left to give.
        """
        if not (self.allow_grid_export and r.force_discharge):
            return False
        if r.inhibit_discharge:
            return False
        return r.zen_soc > self.zen_soc_min

    def _setpoint_for(self, r: Reading) -> int | None:
        """The external setpoint, clamped, or None if the brain is on its own.

        Both opt-ins are required: an optimiser buys and sells by nature,
        so a setpoint with only one of them is a misconfiguration (config
        validation refuses it) and is ignored here as belt and braces.
        Either inhibit gate outranks the setpoint entirely: while a stop
        request is up the brain behaves as if no optimiser were speaking,
        so the existing inhibit handling applies to the whole fleet.
        """
        if r.setpoint is None:
            return None
        if not (self.allow_grid_charge and self.allow_grid_export):
            return None
        if r.inhibit_charge or r.inhibit_discharge:
            return None
        sp = int(r.setpoint)
        # Below the Zendure's pilot floor there is nothing it can do but
        # idle, so treat it as a hold rather than sending an unreachable
        # trickle.
        if abs(sp) < PILOT_W:
            return 0
        return max(-self.max_discharge, min(self.max_charge, sp))

    def _setpoint_pib_for(self, sp: int, zen_power: float, curtail: bool = False) -> tuple[str, list[str] | None]:
        """PIB (mode, permissions) that follow a setpoint as closely as they can.

        The PIBs have no watt setpoint, so they approximate the optimiser's
        intent per direction:
          0                 -> zero, discharge only: the optimiser plans the
                               Zendure alone, so its hold is the Zendure's.
                               The PIBs keep covering the house but never
                               charge. Held in standby until the Zendure has
                               actually stopped.
          0 .. partial, while curtailing
                            -> zero, charge only: exporting costs money, so
                               the PIBs soak surplus the optimiser did not
                               plan for. Held in standby while the Zendure
                               is still discharging, or they would charge
                               from its ramp-down.
          full-rate charge  -> to_full: the optimiser wants energy in now.
          partial charge    -> zero, charge only: soak surplus, never fight.
          discharge         -> zero, discharge only: cover load, never fight.
        Withholding the opposite permission is the cross-charge lock: with
        discharge_allowed the PIBs would read the Zendure's grid import as
        load and feed it; with charge_allowed they would read its export as
        surplus and charge from it.
        """
        if sp >= self.max_charge:
            return "to_full", ["charge_allowed"]
        if curtail and sp >= 0:
            if zen_power < -PILOT_W:
                return "standby", None
            return "zero", ["charge_allowed"]
        if sp > 0:
            return "zero", ["charge_allowed"]
        if sp < 0 or abs(zen_power) < PILOT_W:
            return "zero", ["discharge_allowed"]
        return "standby", None

    def _apply_gates(self, r: Reading, t: float) -> tuple[str | None, list[str] | None, bool] | None:
        """Run the external gates ahead of the transition table.

        Returns (pib_mode, pib_permissions, inhibited) when a gate takes
        control of this tick, or None to let the FSM proceed normally.
        Gates are checked before transitions because they must be able to
        override any state, including ones whose own table would keep
        them there (DISCHARGE has no "stop discharging" guard — covering
        load is exactly its job).
        """
        # Grid charge outranks everything except inhibit_charge, which
        # _wants_grid_charge already folds in.
        if self._wants_grid_charge(r):
            if self.state != State.GRID_CHARGE:
                self.state = State.GRID_CHARGE
                self._init_step_for_new_state()
                return "to_full", ["charge_allowed"], False
            return None, None, False

        # Leaving GRID_CHARGE: the signal dropped or the batteries filled.
        # Drop to SLEEP rather than guessing — the normal wake guards pick
        # the right successor from the next few seconds of P1.
        if self.state == State.GRID_CHARGE:
            self.state = State.SLEEP
            self._init_step_for_new_state()
            # If a stop request is what ended it, the target=0 must reach
            # the device this tick: the SLEEP carve-outs in _should_send
            # would otherwise read "Zen charging while P1 exports" as a
            # startup grace and leave it running against the gate.
            return "standby", None, r.inhibit_charge

        # Force discharge is checked after grid charge, so force_charge wins
        # if both are somehow asserted — charging is the safer resolution of a
        # contradiction than slamming the relay the other way.
        if self._wants_force_discharge(r):
            if self.state != State.FORCE_DISCHARGE:
                self.state = State.FORCE_DISCHARGE
                self._init_step_for_new_state()
                # discharge_allowed ONLY. In zero mode the PIBs read the
                # negative P1 the Zendure is creating as surplus and would
                # charge from our own export; withholding charge_allowed is
                # the hardware lock that prevents it.
                return "zero", ["discharge_allowed"], False
            return None, None, False

        if self.state == State.FORCE_DISCHARGE:
            self.state = State.SLEEP
            self._init_step_for_new_state()
            return "standby", None, r.inhibit_discharge

        # The setpoint ranks below the manual force gates (an explicit
        # human toggle beats an optimiser) and below the inhibit gates
        # (folded into _setpoint_for), but above the transition table.
        sp = self._setpoint_for(r)
        if sp is not None:
            pib = self._setpoint_pib_for(sp, r.zen_power, r.curtail_export or r.import_pays)
            self._setpoint_w = sp
            if self.state != State.SETPOINT or pib != self._setpoint_pib:
                if self.state != State.SETPOINT:
                    self.state = State.SETPOINT
                    self._init_step_for_new_state()
                self._setpoint_pib = pib
                # The PIB permissions go out this tick; the Zendure target
                # must too, or the two disagree for up to a heartbeat. The
                # restart case makes this concrete: Zen still charging from
                # before, optimiser now says 0, PIBs just got both
                # permissions — without a sent hold they discharge into it.
                self._force_send = True
                return pib[0], pib[1], False
            return None, None, False

        if self.state == State.SETPOINT:
            self.state = State.SLEEP
            self._setpoint_pib = None
            self._init_step_for_new_state()
            return "standby", None, (r.inhibit_charge or r.inhibit_discharge)

        if self._is_inhibited(self.state, r):
            self.state = State.SLEEP
            self._init_step_for_new_state()
            return "standby", None, True

        return None

    # --- FSM core ---

    def _check_transitions(self, r: Reading, pib_abs: float, t: float) -> tuple[str | None, list[str] | None]:
        """Check transitions for current state. Returns (pib_mode, pib_permissions) if transition fires."""
        transitions = self._transitions.get(self.state, [])
        for trans, guard in transitions:
            # A gated-off destination is treated as a failed guard, timer
            # and all. Vetoing only at the moment the holdoff expires
            # would let a transition sit fully armed for the whole
            # inhibited window and fire on the first tick after release,
            # skipping the debounce that holdoff exists to provide.
            if self._is_inhibited(trans.to, r):
                trans.reset()
                continue
            if not guard(r, pib_abs):
                trans.reset()
                continue

            # Guard passed — check holdoff
            if trans.holdoff_s == 0:
                # Immediate transition
                self.state = trans.to
                self._init_step_for_new_state()
                return trans.pib_mode, trans.pib_permissions

            # Timed holdoff
            if trans._since is None:
                trans._since = t
            elif t - trans._since >= trans.holdoff_s:
                trans.reset()
                self.state = trans.to
                self._init_step_for_new_state()
                return trans.pib_mode, trans.pib_permissions
            # else: still counting, don't reset

        return None, None

    def _init_step_for_new_state(self) -> None:
        """Initialise the Zendure step on entering a new state. Default 0."""
        self._zen_step_idx = 0
        self._charge_was_nom = False
        self._discharge_held_off = False
        # Preserve the stuck timer across DISCHARGE↔DISCHARGE_HELP bounces
        # so it accumulates to 60s despite the state ping-pong.
        if self.state not in (State.DISCHARGE, State.DISCHARGE_HELP):
            self._zen_stuck_since = None
            self._zen_is_stuck = False
        for trans, _ in self._transitions.get(self.state, []):
            trans.reset()

    def _compute_target(self, r: Reading, pib_abs: float, t: float) -> int:
        """Compute Zendure target power for current state."""
        if self.state == State.SLEEP:
            return 0

        if self.state == State.FORCE_DISCHARGE:
            # Mirror of GRID_CHARGE: exporting is the deliberate intent, so
            # P1 carries no signal worth tracking. Flat max rate; the SOC
            # clamp in decide() is what stops it.
            return -self.max_discharge

        if self.state == State.GRID_CHARGE:
            # No NOM, no stepping: importing is the point, so P1 carries no
            # signal worth tracking. Flat max rate, SOC-clamped downstream.
            return self.max_charge

        if self.state == State.SETPOINT:
            # The optimiser already decided against P1; follow it flat.
            # SOC-clamped downstream like the other gate-owned states.
            return self._setpoint_w

        if self.state == State.CHARGE:
            pib_charge_cap = _total_charge_cap(r)
            pibs_taper = _all_in_taper(r, self.PIB_TAPER_CAP)

            if (pib_charge_cap == 0 or pibs_taper) and r.zen_soc < self.zen_soc_max:
                # NOM: PIBs at limit, Zendure absorbs remaining.
                nom = int(r.zen_power - r.p1)
                target = max(PILOT_W, min(self.max_charge, nom))
                # Smooth the stepped→NOM boundary. On the first NOM tick
                # after a meaningful stepped baseline, the target can leap
                # ~1.4kW (e.g. step 800 → full NOM 2400) which exceeds the
                # urgent-send threshold and causes Zen overshoot. Clamp to
                # one step above the prior stepped target on that first NOM
                # tick; subsequent ticks use the full NOM value.
                # Skip the clamp when current_step==0 (nothing to ramp from):
                # the brain hasn't sent anything meaningful yet.
                if not self._charge_was_nom and self._zen_step_idx > 0:
                    target = min(target, self._current_step() + 400)
                self._charge_was_nom = True
                return target
            else:
                # Stepped: PIBs are the sensor
                self._charge_was_nom = False
                self._update_step(pib_abs, t, r.p1, pib_charge_cap)
                return self._current_step()

        if self.state == State.DISCHARGE:
            # nom is exactly -(house load): the zen_power term cancels
            # whatever the Zendure is currently pushing out, so it reads
            # demand directly and is stable regardless of the setpoint.
            # That is what makes it safe to threshold on.
            nom = int(r.zen_power - r.p1)

            # Holding the -PILOT_W floor when demand is below it feeds the
            # difference back into the grid. P1 then sits around -50W,
            # inside the dead zone above the DISCHARGE->CHARGE guard
            # (-100W), so no transition can see it and the brain parks
            # there indefinitely — bleeding the pack into the meter at
            # ~1.2kWh/day. Stop instead of holding the floor.
            if self._discharge_held_off:
                if nom > -(PILOT_W + self.EXPORT_HYST_W):
                    return 0
                self._discharge_held_off = False
            elif nom > -PILOT_W:
                self._discharge_held_off = True
                return 0

            return max(-self.max_discharge, min(-PILOT_W, nom))

        if self.state == State.DISCHARGE_HELP:
            # Zendure holds at max, PIBs track P1 residual. No NOM — one controller only.
            return -self.max_discharge

        if self.state == State.PIB_DISCHARGE:
            return 0

        return 0

    def decide(self, r: Reading, t: float) -> Decision:
        pib_abs = sum(abs(p) for p in r.pibs)
        self._last_zen_power = r.zen_power
        self._last_p1 = r.p1
        self._last_zen_soc = r.zen_soc

        # Stuck detection: Zen commanded to discharge but producing nothing.
        # Tracked outside the transition table so DISCHARGE↔DISCHARGE_HELP
        # bouncing doesn't reset the timer on each state entry.
        zen_should_output = (
            self.state in (State.DISCHARGE, State.DISCHARGE_HELP)
            and self.last_sent_target is not None
            and self.last_sent_target < -PILOT_W
        )
        if zen_should_output and abs(r.zen_power) < PILOT_W:
            if self._zen_stuck_since is None:
                self._zen_stuck_since = t
            self._zen_is_stuck = (t - self._zen_stuck_since) >= self.ZEN_STUCK_S
        else:
            self._zen_stuck_since = None
            self._zen_is_stuck = False

        # The import-pays override applies only while inhibit_charge is
        # off: a stop request beats a go request, exactly as inhibit_charge
        # outranks force_charge. When the override lifts in SETPOINT, forget
        # the band SETPOINT last emitted so the setpoint branch re-issues it
        # (with a forced send) instead of staying quiet because its band did
        # not change.
        import_pays_on = r.import_pays and not r.inhibit_charge
        import_pays_ended = self._import_pays_active and not import_pays_on
        if import_pays_ended and self.state == State.SETPOINT:
            self._setpoint_pib = None

        # External gates run first — they can override any state, and a
        # state they force must not then be re-evaluated by that state's
        # own table on the same tick.
        inhibited = False
        gated = self._apply_gates(r, t)
        if gated is not None:
            pib_mode, pib_permissions, inhibited = gated
        else:
            pib_mode, pib_permissions = self._check_transitions(r, pib_abs, t)

        # Import-pays overrides whatever PIB action the gates or the table
        # chose. It feeds the same desired-state bookkeeping below, so the
        # heartbeat re-asserts the override rather than the state's action.
        if import_pays_on:
            self._import_pays_active = True
            override = self._import_pays_pib(r)
            if override != (self._pib_mode_desired, self._pib_perms_desired):
                pib_mode, pib_permissions = override
            else:
                pib_mode, pib_permissions = None, None
        elif import_pays_ended:
            self._import_pays_active = False
            if pib_mode is None and pib_permissions is None and self.state in self._STATE_PIB:
                pib_mode, pib_permissions = self._STATE_PIB[self.state]

        # Track desired PIB state across ticks so the heartbeat can re-assert
        # it. Update on transition; otherwise re-emit when PIB_HEARTBEAT_S
        # has elapsed since the last send.
        if pib_mode is not None or pib_permissions is not None:
            self._pib_mode_desired = pib_mode
            self._pib_perms_desired = pib_permissions
            self._pib_send_t = t
        elif self._pib_mode_desired is not None and (t - self._pib_send_t) >= self.PIB_HEARTBEAT_S:
            pib_mode = self._pib_mode_desired
            pib_permissions = self._pib_perms_desired
            self._pib_send_t = t

        # Compute target
        target = self._compute_target(r, pib_abs, t)

        # SOC clamp
        if (r.zen_soc >= self.zen_soc_max and target > 0) or (r.zen_soc <= self.zen_soc_min and target < 0):
            target = 0

        # Send logic
        target, send, _urgent = self._should_send(target, t, inhibited)

        return Decision(
            target=target,
            zone=self.state.value,
            send=send,
            pib_mode=pib_mode,
            pib_permissions=pib_permissions,
            inhibited=inhibited,
        )

    def _should_send(self, target: int, t: float, inhibited: bool = False) -> tuple[int, bool, bool]:
        elapsed = t - self.last_send_time
        change = abs(target - (self.last_sent_target or 0))
        urgent = change > 500

        # An inhibit-forced target=0 is a genuine stop request and must
        # reach the device this tick. Both carve-outs below suppress
        # target=0 on readings that are indistinguishable from the ones a
        # gate fires on — "Zen discharging while P1 imports" is precisely
        # the EV-charging case inhibit_discharge exists to stop. Without
        # this bypass the gate would flip the state and change nothing.
        if inhibited:
            return target, True, True
        if self._force_send:
            self._force_send = False
            return target, True, True

        # Don't kill a Zendure that's already charging from genuine surplus.
        # Two cases:
        #   - Stepped CHARGE mode starts at step 0 → target=0; the deadband
        #     would say "send standby!" while Zen is still ramping up.
        #   - During SLEEP startup-detection holdoff (2s), the brain hasn't
        #     yet adopted CHARGE; sending standby in those 2s would kill a
        #     Zen that's actively charging from surplus.
        # In both cases: if Zen is meaningfully charging AND grid is
        # exporting, leave it alone and let the FSM/step-up logic catch up.
        # Carve-out: at the SOC ceiling, target=0 IS a real stop request
        # (battery full), not a stepped-baseline 0. Don't suppress it —
        # otherwise the brain stops talking to a saturated Zen until the
        # CHARGE→SLEEP transition fires (up to FLIP_S=30s later).
        if (
            target == 0
            and self.state in (State.CHARGE, State.SLEEP)
            and self._last_zen_power > PILOT_W
            and self._last_p1 < self.P1_EXPORT
            and self._last_zen_soc < self.zen_soc_max
        ):
            return target, False, False
        # Symmetric: don't kill a Zendure that's actively discharging into
        # demand during the startup holdoff. Same SOC carve-out — at the
        # floor, target=0 is a real stop, not a startup-grace suppression.
        if (
            target == 0
            and self.state == State.SLEEP
            and self._last_zen_power < -PILOT_W
            and self._last_p1 > self.P1_IMPORT
            and self._last_zen_soc > self.zen_soc_min
        ):
            return target, False, False

        # Don't send while Zendure is still ramping to the last target.
        # Overwriting in-flight ramps causes overshoot oscillation.
        # Timeout after 5s in case Zendure can't reach target (SOC limits, BMS throttle).
        if self.last_sent_target is not None and elapsed < 5.0:
            still_ramping = abs(self._last_zen_power - self.last_sent_target) > 50
            if still_ramping and not urgent:
                return target, False, False

        if self.state in (State.DISCHARGE, State.DISCHARGE_HELP):
            # A held-off zero is a real hold command, not "nothing to say",
            # so it heartbeats like any other target. main.py maps target=0
            # in DISCHARGE to hold_zero() — a RAM write, no relay click and
            # no flash wear — so re-asserting it every 30s is cheap.
            holding_off = target == 0 and self._discharge_held_off
            send = change >= self.NOM_DEADBAND or (elapsed >= 30 and (target != 0 or holding_off))
            return target, send, urgent

        if elapsed < 5.0:
            return target, False, False

        # Heartbeat re-asserts the last command so drift self-heals.
        # In SLEEP/PIB_DISCHARGE at target=0 the device is in flash standby
        # (persists across reboots) — use a longer interval so we don't
        # wake it from deep sleep every 30s.
        # `last_sent_target is not None` keeps startup quiet: brain has no
        # prior state to re-assert.
        zen_idle = self.state in (State.SLEEP, State.PIB_DISCHARGE, State.SETPOINT) and target == 0
        heartbeat_s = self.ZEN_IDLE_HEARTBEAT_S if zen_idle else self.ZEN_HEARTBEAT_S
        heartbeat = elapsed >= heartbeat_s and self.last_sent_target is not None
        send = heartbeat or change >= 50
        return target, send, urgent

    def mark_pib_send_failed(self) -> None:
        """Force the next decide() to re-emit pib_mode/permissions immediately,
        bypassing the heartbeat throttle. Call this from the I/O layer after
        a set_mode PUT fails so we recover within 1s instead of waiting up
        to PIB_HEARTBEAT_S (5 min) for the next scheduled re-assertion."""
        self._pib_send_t = -999

    def mark_sent(self, target: int, t: float, ac_mode: int | None = None) -> None:
        self.last_sent_target = target
        self.last_send_time = t
        if ac_mode is not None:
            self.last_ac_mode = ac_mode
        elif target > 0:
            self.last_ac_mode = self.AC_CHARGE
        elif target < 0:
            self.last_ac_mode = self.AC_DISCHARGE
        else:
            # target=0 (standby) → relay is between modes. The next
            # non-zero send needs to re-issue the mode-switch command,
            # which happens iff last_ac_mode != new direction.
            self.last_ac_mode = None
