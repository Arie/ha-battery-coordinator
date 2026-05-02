#!/usr/bin/env python3
"""Battery Coordinator — standalone entry point.

Talks directly to Zendure (local REST) and HomeWizard P1 meter (local HTTPS).
No Home Assistant dependency. Optional HA connection for solar sensor only.

Usage:
  python main.py              # live mode (default — controls devices)
  python main.py --live       # force live mode regardless of config
  DRY_RUN=true python main.py # observe-only via env var
"""

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime

import aiohttp

from brains.permission_fsm import PermissionFSM
from config import Config
from device_io import DeviceIO

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("coordinator")


async def main():
    parser = argparse.ArgumentParser(description="Battery Coordinator")
    parser.add_argument("--live", action="store_true", help="Force live mode (overrides DRY_RUN env / dry_run option)")
    args = parser.parse_args()

    config = Config()
    # --live forces live mode (overrides config). Otherwise trust whatever
    # the user set via add-on options or DRY_RUN env var (default: false).
    if args.live:
        config.dry_run = False

    errors = config.validate()
    if errors:
        for e in errors:
            log.error(f"Config error: {e}")
        sys.exit(1)

    log.setLevel(config.log_level.upper())

    brain = PermissionFSM(**config.brain_kwargs())

    io = DeviceIO(config)
    mode = "LIVE" if not config.dry_run else "DRY RUN"
    log.info(f"Battery Coordinator [{mode}]")
    if not config.dry_run:
        log.info("** CONTROLLING DEVICES **")

    HEARTBEAT_S = 60  # log a status line at INFO at least this often

    async with aiohttp.ClientSession() as session:
        # Read initial Zendure SN. Retry — the device may take 1–2 min
        # after a host reboot to populate it, and writes need the SN.
        zen_sn = await io.zendure.fetch_sn(session)
        log.info(f"Zendure SN: {zen_sn or '<not yet available>'}")
        if not zen_sn and not config.dry_run:
            # Writes will fail with an empty SN. read() keeps polling and
            # caches the SN whenever it shows up, so writes self-heal once
            # the device populates it.
            log.warning("Zendure SN never appeared — writes will fail until it does")

        prev_state = None
        prev_target: int | None = None
        last_info_t = 0.0

        while True:
            t = time.monotonic()
            now = datetime.now().strftime("%H:%M:%S")

            # Read all devices
            reading, zen, p1 = await io.read_all(session)

            # Brain decides
            d = brain.decide(reading, t)

            # Send Zendure command
            sent = ""
            if not config.dry_run and d.send:
                if d.target > 0:
                    mode_switch = brain.last_ac_mode != PermissionFSM.AC_CHARGE
                    ok = await io.zendure.charge(session, d.target, mode_switch)
                    brain.mark_sent(d.target, t)
                    sent = f" SENT charge {d.target}W" + (" (mode switch)" if mode_switch else "")
                elif d.target < 0:
                    mode_switch = brain.last_ac_mode != PermissionFSM.AC_DISCHARGE
                    ok = await io.zendure.discharge(session, abs(d.target), mode_switch)
                    brain.mark_sent(d.target, t)
                    sent = f" SENT discharge {abs(d.target)}W" + (" (mode switch)" if mode_switch else "")
                elif d.target == 0:
                    # First entry into target=0 (last_ac_mode still set
                    # from a prior charge/discharge) → flash standby so
                    # the device deep-sleeps across reboots. Subsequent
                    # heartbeat re-assertions → RAM-only hold_zero, to
                    # avoid wearing flash with ~2880 writes/day.
                    if brain.last_ac_mode is not None:
                        ok = await io.zendure.standby(session)
                        sent = " SENT standby"
                    else:
                        ok = await io.zendure.hold_zero(session)
                        sent = " SENT hold-zero"
                    brain.mark_sent(0, t)
                if sent and not ok:
                    sent += " FAILED!"

            # Send PIB command
            pib_sent = ""
            if not config.dry_run and (d.pib_mode or d.pib_permissions is not None):
                pib_mode = d.pib_mode or "zero"
                ok = await io.p1.set_mode(session, pib_mode, d.pib_permissions)
                if not ok:
                    # Don't wait 5 min for the heartbeat — re-emit next tick.
                    # PIB permission PUTs are the cross-charge lock; a silent
                    # failure means batteries can fight until the next
                    # heartbeat lands.
                    brain.mark_pib_send_failed()
                perms = ""
                if d.pib_permissions is not None:
                    perms = "(" + ",".join(p.replace("_allowed", "") for p in d.pib_permissions) + ")"
                pib_sent = f" PIB→{pib_mode}{perms}" + ("" if ok else " FAILED!")

            # Per-PIB breakdown: "+800/+240W (84/97%)"
            if reading.pibs:
                pib_powers_s = "/".join(f"{p:>+5.0f}" for p in reading.pibs) + "W"
                pib_socs_s = "/".join(f"{s:.0f}" for s in reading.pib_socs) + "%"
                pib_str = f"🔌 {pib_powers_s} ({pib_socs_s})"
            else:
                pib_str = f"🔌 {p1.pib_power:>+5.0f}W ({p1.pib_count}x)"

            tgt_s = f"charge {d.target}W" if d.target > 0 else f"discharge {abs(d.target)}W" if d.target < 0 else "hold"
            diff = d.target - zen.power

            # State transition → its own INFO line for easy grep.
            state_changed = d.zone != prev_state
            if state_changed:
                log.info(f"State: {prev_state or '∅'} → {d.zone}")
                prev_state = d.zone

            # Promote the per-tick line to INFO only when something interesting
            # actually happened — a send, a state change, a target shift, or
            # the 60s heartbeat. Quiet ticks stay at DEBUG.
            target_changed = prev_target is None or abs(d.target - prev_target) >= 50
            heartbeat_due = (t - last_info_t) >= HEARTBEAT_S
            interesting = bool(sent or pib_sent or state_changed or target_changed or heartbeat_due)
            line = (
                f"{now}  P1:{reading.p1:>+6.0f}W  {pib_str}  "
                f"☀️{reading.solar:>5.0f}W  🪫{zen.power:>+6.0f}W ({zen.soc:.0f}%)  "
                f"[{d.zone}] → {tgt_s} ({diff:>+.0f}){sent}{pib_sent}"
            )
            if interesting:
                log.info(line)
                last_info_t = t
            else:
                log.debug(line)
            prev_target = d.target

            await asyncio.sleep(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped.")
