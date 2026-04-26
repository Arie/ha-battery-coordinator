#!/usr/bin/env python3
"""Battery Coordinator — standalone entry point.

Talks directly to Zendure (local REST) and HomeWizard P1 meter (local HTTPS).
No Home Assistant dependency. Optional HA connection for solar sensor only.

Usage:
  python main.py              # dry run (observe only)
  python main.py --live       # control devices
  DRY_RUN=true python main.py # dry run via env var
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
    parser.add_argument("--live", action="store_true", help="Control devices (default: dry run)")
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

    tick = 0
    async with aiohttp.ClientSession() as session:
        # Read initial Zendure SN
        zen_status = await io.zendure.read(session)
        log.info(f"Zendure SN: {zen_status.sn}")

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
                    ok = await io.zendure.standby(session)
                    brain.mark_sent(0, t)
                    brain.last_ac_mode = None
                    sent = " SENT standby"
                if sent and not ok:
                    sent += " FAILED!"

            # Send PIB command
            pib_sent = ""
            if not config.dry_run and (d.pib_mode or d.pib_permissions is not None):
                pib_mode = d.pib_mode or "zero"
                ok = await io.p1.set_mode(session, pib_mode, d.pib_permissions)
                perms = ""
                if d.pib_permissions is not None:
                    perms = "(" + ",".join(p.replace("_allowed", "") for p in d.pib_permissions) + ")"
                pib_sent = f" PIB→{pib_mode}{perms}" + ("" if ok else " FAILED!")

            # Log
            tgt_s = f"charge {d.target}W" if d.target > 0 else f"discharge {abs(d.target)}W" if d.target < 0 else "hold"
            diff = d.target - zen.power

            tick += 1
            if tick == 1 or tick % 30 == 0:
                log.info(
                    f"{'Time':<9} {'P1':>7} {'PIB':>6} {'Solar':>5} {'Zen':>6} "
                    f"{'Zone':<5} {'Target':<20} {'Diff':>6}"
                )

            # Per-PIB breakdown: "+800/+240W (84/97%)"
            if reading.pibs:
                pib_powers_s = "/".join(f"{p:>+5.0f}" for p in reading.pibs) + "W"
                pib_socs_s = "/".join(f"{s:.0f}" for s in reading.pib_socs) + "%"
                pib_str = f"🔌 {pib_powers_s} ({pib_socs_s})"
            else:
                pib_str = f"🔌 {p1.pib_power:>+5.0f}W ({p1.pib_count}x)"

            log.info(
                f"{now}  P1:{reading.p1:>+6.0f}W  "
                f"{pib_str}  "
                f"☀️{reading.solar:>5.0f}W  "
                f"🪫{zen.power:>+6.0f}W ({zen.soc:.0f}%)  "
                f"[{d.zone:<5}] → {tgt_s} ({diff:>+.0f}){sent}{pib_sent}"
            )

            await asyncio.sleep(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped.")
