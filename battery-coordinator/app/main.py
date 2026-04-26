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

    SUMMARY_INTERVAL_S = 60

    async with aiohttp.ClientSession() as session:
        # Read initial Zendure SN
        zen_status = await io.zendure.read(session)
        log.info(f"Zendure SN: {zen_status.sn}")

        prev_state = None
        sends_in_window = 0
        p1_samples: list[float] = []
        last_summary_t = time.monotonic()

        while True:
            t = time.monotonic()
            now = datetime.now().strftime("%H:%M:%S")

            # Read all devices
            reading, zen, p1 = await io.read_all(session)
            p1_samples.append(reading.p1)

            # Brain decides
            d = brain.decide(reading, t)

            # Send Zendure command
            sent = ""
            if not config.dry_run and d.send:
                if d.target > 0:
                    mode_switch = brain.last_ac_mode != PermissionFSM.AC_CHARGE
                    ok = await io.zendure.charge(session, d.target, mode_switch)
                    brain.mark_sent(d.target, t)
                    sent = f"SENT charge {d.target}W" + (" (mode switch)" if mode_switch else "")
                elif d.target < 0:
                    mode_switch = brain.last_ac_mode != PermissionFSM.AC_DISCHARGE
                    ok = await io.zendure.discharge(session, abs(d.target), mode_switch)
                    brain.mark_sent(d.target, t)
                    sent = f"SENT discharge {abs(d.target)}W" + (" (mode switch)" if mode_switch else "")
                elif d.target == 0:
                    ok = await io.zendure.standby(session)
                    brain.mark_sent(0, t)
                    brain.last_ac_mode = None
                    sent = "SENT standby"
                if not ok:
                    sent += " FAILED!"
                sends_in_window += 1

            # Send PIB command
            pib_sent = ""
            if not config.dry_run and (d.pib_mode or d.pib_permissions is not None):
                pib_mode = d.pib_mode or "zero"
                ok = await io.p1.set_mode(session, pib_mode, d.pib_permissions)
                perms = ""
                if d.pib_permissions is not None:
                    perms = "(" + ",".join(p.replace("_allowed", "") for p in d.pib_permissions) + ")"
                pib_sent = f"PIB→{pib_mode}{perms}" + ("" if ok else " FAILED!")

            # Per-PIB breakdown: "+800/+240W (84/97%)"
            if reading.pibs:
                pib_powers_s = "/".join(f"{p:>+5.0f}" for p in reading.pibs) + "W"
                pib_socs_s = "/".join(f"{s:.0f}" for s in reading.pib_socs) + "%"
                pib_str = f"🔌 {pib_powers_s} ({pib_socs_s})"
            else:
                pib_str = f"🔌 {p1.pib_power:>+5.0f}W ({p1.pib_count}x)"

            tgt_s = (
                f"charge {d.target}W" if d.target > 0
                else f"discharge {abs(d.target)}W" if d.target < 0
                else "hold"
            )
            diff = d.target - zen.power

            # Per-tick line — DEBUG by default, INFO only with log_level=debug.
            extras = " ".join(s for s in (sent, pib_sent) if s)
            log.debug(
                f"P1:{reading.p1:>+6.0f}W  {pib_str}  "
                f"☀️{reading.solar:>5.0f}W  🪫{zen.power:>+6.0f}W ({zen.soc:.0f}%)  "
                f"[{d.zone}] → {tgt_s} ({diff:>+.0f}) {extras}".rstrip()
            )

            # State transition → INFO once.
            if d.zone != prev_state:
                log.info(f"State: {prev_state or '∅'} → {d.zone}")
                prev_state = d.zone

            # Sends → INFO immediately (these are the actions that matter).
            if sent:
                log.info(sent)
            if pib_sent:
                log.info(pib_sent)

            # 60-second operational summary.
            if t - last_summary_t >= SUMMARY_INTERVAL_S:
                avg_p1 = sum(p1_samples) / len(p1_samples) if p1_samples else 0
                log.info(
                    f"60s: state={d.zone} avg P1={avg_p1:+.0f}W "
                    f"Zen={zen.power:+.0f}W ({zen.soc:.0f}%) "
                    f"PIBs={pib_str.replace('🔌 ', '')} "
                    f"sends={sends_in_window}"
                )
                p1_samples.clear()
                sends_in_window = 0
                last_summary_t = t

            await asyncio.sleep(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped.")
