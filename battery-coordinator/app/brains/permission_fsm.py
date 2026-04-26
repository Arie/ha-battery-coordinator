"""PermissionFSM: FSM-based brain using PIB permissions to prevent lockout.

Same behavior as PermissionBrain, but transitions are defined as data
in a transition table instead of scattered if/elif blocks.
"""

from dataclasses import dataclass, field
from enum import Enum

from coordinator_logic import Reading, Decision, PILOT_W, pib_max_charge, pib_max_discharge


class _FakeZone:
    """Minimal zone holder so callers can read brain.sm.zone.value."""

    def __init__(self):
        self.value = "idle"


class _FakeSM:
    """Backwards-compatible shim — older callers (and the simulator)
    expected `brain.sm.zone.value` to look up the current state."""

    def __init__(self):
        self.zone = _FakeZone()


def _total_charge_cap(r: Reading) -> int:
    """Sum of every configured PIB's current max charge rate."""
    return sum(pib_max_charge(s) for s in r.pib_socs)


def _all_in_taper(r: Reading, threshold: int) -> bool:
    """All PIBs are in their taper zone (per-PIB charge cap below threshold)."""
    return bool(r.pib_socs) and all(pib_max_charge(s) < threshold for s in r.pib_socs)


class State(Enum):
    SLEEP = "SLEEP"
    CHARGE = "CHARGE"
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

    def reset(self):
        self._since = None


class PermissionFSM:
    """FSM brain with explicit transition table.

    States:
      SLEEP          – all standby
      CHARGE         – PIBs zero (charge-only), Zendure at fixed step or NOM
      DISCHARGE      – Zendure NOM-tracks P1 solo, PIBs standby
      DISCHARGE_HELP – Zendure maxed, PIBs helping (discharge-only)
      PIB_DISCHARGE  – Zendure off, PIBs discharge alone
    """

    AC_CHARGE = 1
    AC_DISCHARGE = 2

    # Zendure stepped power levels (charge mode)
    ZEN_STEPS = [0, 200, 400, 800, 1200, 1600, 2000, 2400]

    # PIB saturation thresholds for stepping
    PIB_MAXED = 1400        # near hardware max → fast step-up
    PIB_HIGH = 1200         # busy → normal step-up
    PIB_LOW = 200           # barely working → step-down

    # Step timing
    STEP_HOLDOFF = 15       # seconds before normal step change
    STEP_HOLDOFF_FAST = 5   # seconds before fast step-up (PIBs maxed)
    STEP_DOWN_COOLDOWN = 30 # seconds after step-up before allowing step-down

    # Transition holdoffs
    WAKE_CHARGE_S = 10      # sustained export before SLEEP → CHARGE
    WAKE_DISCHARGE_S = 30   # sustained import before SLEEP → DISCHARGE
    FLIP_S = 30             # sustained signal before charge ↔ discharge flip
    HELP_ENTER_S = 15       # sustained Zen maxed before waking PIBs
    HELP_EXIT_S = 15        # sustained low load before standbying PIBs

    # P1 thresholds
    P1_EXPORT = -100        # P1 below this = exporting (surplus)
    P1_IMPORT = 200         # P1 above this = importing (deficit)
    P1_OVER_DISCHARGE = -100  # P1 below this in DISCHARGE_HELP = load dropped

    # Zendure capacity thresholds
    ZEN_MAXED_FRAC = 0.95   # Zendure above this fraction of max → considered maxed
    ZEN_HELP_EXIT_FRAC = 0.8  # total discharge below this fraction → PIBs redundant

    # PIB taper
    PIB_TAPER_CAP = 600     # per-PIB max charge below this = in taper zone

    # NOM send deadband
    NOM_DEADBAND = 10       # minimum change before sending in NOM mode

    def __init__(
        self,
        max_charge_w: int = 2400,
        max_discharge_w: int = 2400,
        zen_soc_max: int = 100,
        zen_soc_min: int = 10,
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

        self.state = State.SLEEP
        self._zen_step_idx = 0
        self._pib_high_since: float | None = None
        self._pib_low_since: float | None = None
        self._last_step_up_t: float = -999
        self._last_zen_power: float = 0
        self._last_p1: float = 0

        self.sm = _FakeSM()

        # --- Transition table ---
        # Each state maps to a list of transitions, checked in order.
        # First matching guard wins. Transitions own their holdoff timers.
        self._transitions: dict[State, list[tuple[Transition, callable]]] = {
            State.SLEEP: [
                # Startup: detect hardware already active
                (Transition(State.CHARGE, holdoff_s=0, pib_mode="zero", pib_permissions=["charge_allowed"]),
                 lambda r, _: r.zen_power > PILOT_W),
                (Transition(State.DISCHARGE, holdoff_s=0, pib_mode="standby"),
                 lambda r, _: r.zen_power < -PILOT_W),
                (Transition(State.CHARGE, holdoff_s=0, pib_mode="zero", pib_permissions=["charge_allowed"]),
                 lambda r, _: sum(r.pibs) > abs(self.P1_EXPORT) and r.p1 < self.P1_EXPORT * 2),
                (Transition(State.DISCHARGE, holdoff_s=0, pib_mode="standby"),
                 lambda r, _: sum(r.pibs) < self.P1_EXPORT and abs(r.zen_power) <= PILOT_W and r.zen_soc > self.zen_soc_min),
                # Normal wake
                (Transition(State.CHARGE, holdoff_s=self.WAKE_CHARGE_S, pib_mode="zero", pib_permissions=["charge_allowed"]),
                 lambda r, _: r.p1 < self.P1_EXPORT and (r.zen_soc < self.zen_soc_max - 1 or _total_charge_cap(r) > 200)),
                (Transition(State.DISCHARGE, holdoff_s=self.WAKE_DISCHARGE_S, pib_mode="standby"),
                 lambda r, _: r.p1 > self.P1_IMPORT and (r.zen_soc > self.zen_soc_min or any(s > 1 for s in r.pib_socs))),
            ],
            State.CHARGE: [
                # Everything full → sleep
                (Transition(State.SLEEP, holdoff_s=0, pib_mode="standby"),
                 lambda r, _: _total_charge_cap(r) == 0 and r.zen_soc >= self.zen_soc_max),
                # Surplus gone → discharge
                (Transition(State.DISCHARGE, holdoff_s=self.FLIP_S, pib_mode="standby"),
                 lambda r, pib_abs: pib_abs < 50 and r.p1 > abs(self.P1_EXPORT)),
            ],
            State.DISCHARGE: [
                # Solar returns → charge
                (Transition(State.CHARGE, holdoff_s=self.FLIP_S, pib_mode="zero", pib_permissions=["charge_allowed"]),
                 lambda r, _: r.p1 < self.P1_EXPORT and r.solar > 50),
                # Zendure empty, PIBs can help → PIB_DISCHARGE
                (Transition(State.PIB_DISCHARGE, holdoff_s=0, pib_mode="zero", pib_permissions=["discharge_allowed"]),
                 lambda r, _: r.zen_soc <= self.zen_soc_min and any(s > 1 for s in r.pib_socs)),
                # Zendure empty, PIBs empty → sleep with charge-only PIBs ready
                (Transition(State.SLEEP, holdoff_s=0, pib_mode="zero", pib_permissions=["charge_allowed"]),
                 lambda r, _: r.zen_soc <= self.zen_soc_min and all(s <= 1 for s in r.pib_socs)),
                # Zendure maxed → wake PIBs to help
                (Transition(State.DISCHARGE_HELP, holdoff_s=self.HELP_ENTER_S, pib_mode="zero", pib_permissions=["discharge_allowed"]),
                 lambda r, _: abs(int(r.zen_power - r.p1)) >= self.max_discharge * self.ZEN_MAXED_FRAC and r.p1 > abs(self.P1_EXPORT)),
            ],
            State.DISCHARGE_HELP: [
                # Solar returns → charge
                (Transition(State.CHARGE, holdoff_s=self.FLIP_S, pib_mode="zero", pib_permissions=["charge_allowed"]),
                 lambda r, _: r.p1 < self.P1_EXPORT and r.solar > 50),
                # Zendure can handle alone → back to solo
                # P1 negative = over-discharging, load dropped → back to NOM solo
                (Transition(State.DISCHARGE, holdoff_s=0, pib_mode="standby"),
                 lambda r, _: r.p1 < self.P1_OVER_DISCHARGE),
                (Transition(State.DISCHARGE, holdoff_s=self.HELP_EXIT_S, pib_mode="standby"),
                 lambda r, _: abs(r.zen_power) + max(0, -sum(r.pibs)) < self.max_discharge * self.ZEN_HELP_EXIT_FRAC),
                # Zendure empty → PIBs take over
                (Transition(State.PIB_DISCHARGE, holdoff_s=0, pib_mode="zero", pib_permissions=["discharge_allowed"]),
                 lambda r, _: r.zen_soc <= self.zen_soc_min),
            ],
            State.PIB_DISCHARGE: [
                # Solar returns → charge
                (Transition(State.CHARGE, holdoff_s=self.FLIP_S, pib_mode="zero", pib_permissions=["charge_allowed"]),
                 lambda r, _: r.p1 < self.P1_EXPORT and r.solar > 50),
                # PIBs empty → sleep. Set charge-only so they auto-capture
                # the first watt of solar surplus without waiting for CHARGE wake.
                (Transition(State.SLEEP, holdoff_s=0, pib_mode="zero", pib_permissions=["charge_allowed"]),
                 lambda r, _: all(abs(p) < 10 for p in r.pibs) and all(s <= 1 for s in r.pib_socs)),
            ],
        }

    # --- Step logic (same as PermissionBrain) ---

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

    def _jump_to_step_at_least(self, target_w: int) -> None:
        """Set step to the lowest step >= target_w (rounded up)."""
        target_w = abs(target_w)
        for i, step in enumerate(self.ZEN_STEPS):
            if step >= target_w:
                self._zen_step_idx = i
                return
        self._zen_step_idx = len(self.ZEN_STEPS) - 1

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
                    jumped = self._jump_to_step(self._current_step() + abs(p1))
                else:
                    jumped = self._step_up()
                if jumped:
                    self._last_step_up_t = t
                    self._pib_high_since = None
                    self._pib_low_since = None
        else:
            self._pib_high_since = None

        p1_contradicts = (self.state == State.DISCHARGE and p1 < -200) or (self.state == State.CHARGE and p1 > 200)
        cooldown_ok = p1_contradicts or (t - self._last_step_up_t) >= self.STEP_DOWN_COOLDOWN
        if pib_abs < self.PIB_LOW and cooldown_ok:
            if self._pib_low_since is None:
                self._pib_low_since = t
            elif t - self._pib_low_since >= (self.STEP_HOLDOFF_FAST if p1_contradicts else self.STEP_HOLDOFF):
                self._step_down()
        else:
            self._pib_low_since = None

    # --- FSM core ---

    def _check_transitions(self, r: Reading, pib_abs: float, t: float) -> tuple[str | None, list[str] | None]:
        """Check transitions for current state. Returns (pib_mode, pib_permissions) if transition fires."""
        transitions = self._transitions.get(self.state, [])
        for trans, guard in transitions:
            if not guard(r, pib_abs):
                trans.reset()
                continue

            # Guard passed — check holdoff
            if trans.holdoff_s == 0:
                # Immediate transition
                self.state = trans.to
                self._init_step_for_new_state(r)
                return trans.pib_mode, trans.pib_permissions

            # Timed holdoff
            if trans._since is None:
                trans._since = t
            elif t - trans._since >= trans.holdoff_s:
                trans.reset()
                self.state = trans.to
                self._init_step_for_new_state(r)
                return trans.pib_mode, trans.pib_permissions
            # else: still counting, don't reset

        return None, None

    def _init_step_for_new_state(self, r: Reading) -> None:
        """Initialise the Zendure step on entering a new state. Default 0."""
        self._zen_step_idx = 0

    def _compute_target(self, r: Reading, pib_abs: float, t: float) -> int:
        """Compute Zendure target power for current state."""
        if self.state == State.SLEEP:
            return 0

        if self.state == State.CHARGE:
            pib_charge_cap = _total_charge_cap(r)
            pibs_taper = _all_in_taper(r, self.PIB_TAPER_CAP)

            if (pib_charge_cap == 0 or pibs_taper) and r.zen_soc < self.zen_soc_max:
                # NOM: PIBs at limit, Zendure absorbs remaining
                nom = int(r.zen_power - r.p1)
                return max(PILOT_W, min(self.max_charge, nom))
            else:
                # Stepped: PIBs are the sensor
                self._update_step(pib_abs, t, r.p1, pib_charge_cap)
                return self._current_step()

        if self.state == State.DISCHARGE:
            nom = int(r.zen_power - r.p1)
            return max(-self.max_discharge, min(-PILOT_W, nom))

        if self.state == State.DISCHARGE_HELP:
            # Zendure holds at max, PIBs track P1 residual. No NOM — one controller only.
            return -self.max_discharge

        if self.state == State.PIB_DISCHARGE:
            return 0

        return 0

    def decide(self, r: Reading, t: float) -> Decision:
        combined_pib = sum(r.pibs)
        pib_abs = sum(abs(p) for p in r.pibs)
        pib_dir = "charging" if combined_pib > 50 else ("discharging" if combined_pib < -50 else "idle")
        self._last_zen_power = r.zen_power
        self._last_p1 = r.p1

        prev_state = self.state

        # Check transitions
        pib_mode, pib_permissions = self._check_transitions(r, pib_abs, t)

        # Compute target
        target = self._compute_target(r, pib_abs, t)

        # SOC clamp
        if r.zen_soc >= self.zen_soc_max and target > 0:
            target = 0
        elif r.zen_soc <= self.zen_soc_min and target < 0:
            target = 0

        # Send logic
        target, send, urgent = self._should_send(target, t)

        self.sm.zone.value = self.state.value.lower()
        direction = "charging" if target > 0 else ("discharging" if target < 0 else "idle")

        return Decision(
            target=target,
            zone=self.state.value,
            hunting_dir=direction,
            confirmed_dir=direction,
            pib_dir=pib_dir,
            both_maxed=False,
            send=send,
            urgent=urgent,
            pib_mode=pib_mode,
            pib_permissions=pib_permissions,
        )

    def _should_send(self, target: int, t: float) -> tuple[int, bool, bool]:
        elapsed = t - self.last_send_time
        change = abs(target - (self.last_sent_target or 0))
        urgent = change > 500

        # Don't kill a Zendure that's already charging from genuine surplus.
        # Stepped CHARGE mode starts at step 0 → target=0; if we've ever
        # sent a non-zero command, the deadband says "send standby!" — but
        # if Zen is at meaningful charge AND grid is exporting, the right
        # action is to leave it alone and let the step-up logic catch up.
        if (
            target == 0
            and self.state == State.CHARGE
            and self._last_zen_power > PILOT_W
            and self._last_p1 < self.P1_EXPORT
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
            send = change >= self.NOM_DEADBAND or (elapsed >= 30 and target != 0)
            return target, send, urgent

        if elapsed < 5.0:
            return target, False, False

        heartbeat = elapsed >= 30 and target != 0
        send = heartbeat or change >= 50
        return target, send, urgent

    def mark_sent(self, target: int, t: float, ac_mode: int | None = None) -> None:
        self.last_sent_target = target
        self.last_send_time = t
        if ac_mode is not None:
            self.last_ac_mode = ac_mode
        elif target > 0:
            self.last_ac_mode = self.AC_CHARGE
        elif target < 0:
            self.last_ac_mode = self.AC_DISCHARGE
