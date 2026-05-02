"""PermissionFSM: FSM-based brain using PIB permissions to prevent lockout.

State transitions are defined as data in a transition table instead of
scattered if/elif blocks; each Transition owns its own holdoff timer
and entry-action (pib_mode + pib_permissions).
"""

from dataclasses import dataclass, field
from enum import Enum

from coordinator_logic import Reading, Decision, PILOT_W, pib_max_charge


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

    # PIB saturation thresholds for stepping. Defaults live in DEFAULTS so
    # config.py and the bare PermissionFSM() constructor see the same values.
    DEFAULTS: dict = {
        "step_holdoff_s": 15,
        "flip_s": 30,
        "wake_charge_s": 10,
        "wake_discharge_s": 30,
        "help_enter_s": 15,
        "help_exit_s": 15,
        "pib_high_w": 1200,
        "pib_maxed_w": 1400,
        "pib_low_w": 200,
        "pib_taper_cap_w": 600,
        "nom_deadband_w": 10,
        "p1_export_w": -100,
        "p1_import_w": 200,
    }

    PIB_MAXED = DEFAULTS["pib_maxed_w"]
    PIB_HIGH = DEFAULTS["pib_high_w"]
    PIB_LOW = DEFAULTS["pib_low_w"]

    # Step timing
    STEP_HOLDOFF = DEFAULTS["step_holdoff_s"]
    STEP_HOLDOFF_FAST = 5   # seconds before fast step-up (PIBs maxed)
    STEP_DOWN_COOLDOWN = 30 # seconds after step-up before allowing step-down

    # Transition holdoffs
    WAKE_CHARGE_S = DEFAULTS["wake_charge_s"]
    WAKE_DISCHARGE_S = DEFAULTS["wake_discharge_s"]
    FLIP_S = DEFAULTS["flip_s"]
    HELP_ENTER_S = DEFAULTS["help_enter_s"]
    HELP_EXIT_S = DEFAULTS["help_exit_s"]

    # Heartbeats — re-assert desired state so silent drift (failed PUT,
    # external app toggle, integration refresh) self-heals within a bounded
    # window. Idempotent commands; safe to repeat.
    PIB_HEARTBEAT_S = 300   # re-send pib_mode/permissions every 5 min

    # P1 thresholds
    P1_EXPORT = DEFAULTS["p1_export_w"]
    P1_IMPORT = DEFAULTS["p1_import_w"]
    P1_OVER_DISCHARGE = -100  # P1 below this in DISCHARGE_HELP = load dropped

    # Combined-PIB-power thresholds for startup detection. Independent from
    # P1_EXPORT so tuning the export threshold doesn't accidentally also
    # change PIB direction detection — they're different signals (grid-side
    # vs battery-side) that happened to share a magnitude.
    PIB_CHARGE_DETECT = 100      # combined PIB > this on startup → adopt CHARGE
    PIB_DISCHARGE_DETECT = -100  # combined PIB < this on startup → adopt DISCHARGE

    # Zendure capacity thresholds
    ZEN_MAXED_FRAC = 0.95   # Zendure above this fraction of max → considered maxed
    ZEN_HELP_EXIT_FRAC = 0.8  # total discharge below this fraction → PIBs redundant

    # PIB taper
    PIB_TAPER_CAP = DEFAULTS["pib_taper_cap_w"]

    # NOM send deadband
    NOM_DEADBAND = DEFAULTS["nom_deadband_w"]

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

        # Desired PIB state — set on transitions, re-asserted by heartbeat.
        # None until the first transition fires (no opinion yet → no sends).
        self._pib_mode_desired: str | None = None
        self._pib_perms_desired: list[str] | None = None
        self._pib_send_t: float = -999

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

        # --- Transition table ---
        # Each state maps to a list of transitions, checked in order.
        # First matching guard wins. Transitions own their holdoff timers.
        self._transitions: dict[State, list[tuple[Transition, callable]]] = {
            State.SLEEP: [
                # Startup: detect hardware already active. 2s holdoff so a
                # single noisy reading at process start can't bypass the
                # WAKE_*_S guards below — sustained agreement only.
                (Transition(State.CHARGE, holdoff_s=2, pib_mode="zero", pib_permissions=["charge_allowed"]),
                 lambda r, _: r.zen_power > PILOT_W),
                (Transition(State.DISCHARGE, holdoff_s=2, pib_mode="standby"),
                 lambda r, _: r.zen_power < -PILOT_W),
                (Transition(State.CHARGE, holdoff_s=2, pib_mode="zero", pib_permissions=["charge_allowed"]),
                 lambda r, _: sum(r.pibs) > self.PIB_CHARGE_DETECT and r.p1 < self.P1_EXPORT * 2),
                (Transition(State.DISCHARGE, holdoff_s=2, pib_mode="standby"),
                 lambda r, _: sum(r.pibs) < self.PIB_DISCHARGE_DETECT and abs(r.zen_power) <= PILOT_W and r.zen_soc > self.zen_soc_min),
                # Restart while Zen drained + PIBs covering load → adopt
                # PIB_DISCHARGE so the heartbeat doesn't force-stop them.
                (Transition(State.PIB_DISCHARGE, holdoff_s=2, pib_mode="zero", pib_permissions=["discharge_allowed"]),
                 lambda r, _: sum(r.pibs) < self.PIB_DISCHARGE_DETECT and abs(r.zen_power) <= PILOT_W and r.zen_soc <= self.zen_soc_min),
                # Normal wake
                (Transition(State.CHARGE, holdoff_s=self.WAKE_CHARGE_S, pib_mode="zero", pib_permissions=["charge_allowed"]),
                 lambda r, _: r.p1 < self.P1_EXPORT and (r.zen_soc < self.zen_soc_max - 1 or _total_charge_cap(r) > 200)),
                (Transition(State.DISCHARGE, holdoff_s=self.WAKE_DISCHARGE_S, pib_mode="standby"),
                 lambda r, _: r.p1 > self.P1_IMPORT and (r.zen_soc > self.zen_soc_min or any(s > 1 for s in r.pib_socs))),
            ],
            State.CHARGE: [
                # Near-full + P1 not importing → sleep. Subsumes literal
                # 100/100 (which strict cap==0 required) and catches taper
                # noise (PIBs at 99% with 120W cap each) so PIBs aren't
                # left awake in zero-mode burning idle.
                (Transition(State.SLEEP, holdoff_s=self.FLIP_S, pib_mode="standby"),
                 lambda r, _: all(s >= 99 for s in r.pib_socs)
                              and r.zen_soc >= self.zen_soc_max - 1
                              and r.p1 < self.P1_IMPORT),
                # Surplus gone → discharge
                (Transition(State.DISCHARGE, holdoff_s=self.FLIP_S, pib_mode="standby"),
                 lambda r, pib_abs: pib_abs < 50 and r.p1 > abs(self.P1_EXPORT)),
            ],
            State.DISCHARGE: [
                # Solar returns → charge
                (Transition(State.CHARGE, holdoff_s=self.FLIP_S, pib_mode="zero", pib_permissions=["charge_allowed"]),
                 lambda r, _: r.p1 < self.P1_EXPORT),
                # Zendure empty, PIBs can help → PIB_DISCHARGE
                (Transition(State.PIB_DISCHARGE, holdoff_s=0, pib_mode="zero", pib_permissions=["discharge_allowed"]),
                 lambda r, _: r.zen_soc <= self.zen_soc_min and any(s > 1 for s in r.pib_socs)),
                # Zendure empty, PIBs empty → sleep with full standby.
                (Transition(State.SLEEP, holdoff_s=0, pib_mode="standby"),
                 lambda r, _: r.zen_soc <= self.zen_soc_min and all(s <= 1 for s in r.pib_socs)),
                # Zendure maxed → wake PIBs to help
                (Transition(State.DISCHARGE_HELP, holdoff_s=self.HELP_ENTER_S, pib_mode="zero", pib_permissions=["discharge_allowed"]),
                 lambda r, _: abs(int(r.zen_power - r.p1)) >= self.max_discharge * self.ZEN_MAXED_FRAC and r.p1 > abs(self.P1_EXPORT)),
            ],
            State.DISCHARGE_HELP: [
                # Solar returns → charge
                (Transition(State.CHARGE, holdoff_s=self.FLIP_S, pib_mode="zero", pib_permissions=["charge_allowed"]),
                 lambda r, _: r.p1 < self.P1_EXPORT),
                # Zendure can handle alone → back to solo
                # P1 negative = over-discharging, load dropped → back to NOM solo.
                # 3s holdoff filters the 1-2 tick PIB activation transient
                # (HW P1 controller slams 0→max in one tick on entry, briefly
                # overshooting load by ~1.6kW). Real load drops still exit,
                # just 3s later instead of instantly.
                (Transition(State.DISCHARGE, holdoff_s=3, pib_mode="standby"),
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
                 lambda r, _: r.p1 < self.P1_EXPORT),
                # PIBs empty → sleep with full standby. 3s holdoff so PIB
                # power sensor noise (briefly reading 12W when it should be
                # 0) doesn't keep us pinned out of SLEEP; we want sustained
                # near-zero power before committing.
                # Sunrise wake goes through SLEEP→CHARGE (WAKE_CHARGE_S
                # holdoff); a few seconds of leak-back trades for hours of
                # standby savings.
                (Transition(State.SLEEP, holdoff_s=3, pib_mode="standby"),
                 lambda r, _: all(abs(p) < 10 for p in r.pibs) and all(s <= 1 for s in r.pib_socs)),
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
                    jumped = self._jump_to_step(self._current_step() + abs(p1))
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
        # Reset NOM-mode tracker so a new CHARGE entry starts fresh.
        self._charge_was_nom = False
        # Reset the destination state's transition holdoff timers. Without
        # this, a `_since` set during a previous visit to this state
        # persists, and on re-entry `t - _since` may already exceed the
        # holdoff — letting a transition fire after a single fresh tick
        # instead of the documented sustained-signal window.
        for trans, _ in self._transitions.get(self.state, []):
            trans.reset()

    def _compute_target(self, r: Reading, pib_abs: float, t: float) -> int:
        """Compute Zendure target power for current state."""
        if self.state == State.SLEEP:
            return 0

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
                if not self._charge_was_nom and self._current_step() > 0:
                    target = min(target, self._current_step() + 400)
                self._charge_was_nom = True
                return target
            else:
                # Stepped: PIBs are the sensor
                self._charge_was_nom = False
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
        pib_abs = sum(abs(p) for p in r.pibs)
        self._last_zen_power = r.zen_power
        self._last_p1 = r.p1
        self._last_zen_soc = r.zen_soc

        # Check transitions
        pib_mode, pib_permissions = self._check_transitions(r, pib_abs, t)

        # Track desired PIB state across ticks so the heartbeat can re-assert
        # it. Update on transition; otherwise re-emit when PIB_HEARTBEAT_S
        # has elapsed since the last send.
        if pib_mode is not None or pib_permissions is not None:
            self._pib_mode_desired = pib_mode
            self._pib_perms_desired = pib_permissions
            self._pib_send_t = t
        elif (self._pib_mode_desired is not None
              and (t - self._pib_send_t) >= self.PIB_HEARTBEAT_S):
            pib_mode = self._pib_mode_desired
            pib_permissions = self._pib_perms_desired
            self._pib_send_t = t

        # Compute target
        target = self._compute_target(r, pib_abs, t)

        # SOC clamp
        if r.zen_soc >= self.zen_soc_max and target > 0:
            target = 0
        elif r.zen_soc <= self.zen_soc_min and target < 0:
            target = 0

        # Send logic
        target, send, _urgent = self._should_send(target, t)

        return Decision(
            target=target,
            zone=self.state.value,
            send=send,
            pib_mode=pib_mode,
            pib_permissions=pib_permissions,
        )

    def _should_send(self, target: int, t: float) -> tuple[int, bool, bool]:
        elapsed = t - self.last_send_time
        change = abs(target - (self.last_sent_target or 0))
        urgent = change > 500

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
            send = change >= self.NOM_DEADBAND or (elapsed >= 30 and target != 0)
            return target, send, urgent

        if elapsed < 5.0:
            return target, False, False

        # Heartbeat re-asserts the last command, including target=0
        # (flash-standby), so if the device drifted out of standby — silent
        # PUT failure, firmware reboot, external app — we recover within 30s.
        # `last_sent_target is not None` keeps startup quiet: brain has no
        # prior state to re-assert.
        heartbeat = elapsed >= 30 and self.last_sent_target is not None
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
