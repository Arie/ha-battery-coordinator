"""Core types shared between the brain, device I/O, and tests."""

from dataclasses import dataclass

PILOT_W = 50


def pib_max_charge(soc: float) -> int:
    """Max charge power at given SOC, based on real observed taper data."""
    if soc < 93:
        return 800
    taper = {93: 720, 94: 600, 95: 480, 96: 480, 97: 240, 98: 180, 99: 120, 100: 0}
    return taper.get(int(soc), 120)


class Reading:
    """Snapshot of all sensor readings.

    PIB power and SOC are stored as parallel lists of arbitrary length —
    one entry per configured PIB. The brain iterates them uniformly so
    1, 2, 3, or 4 PIBs all "just work".

    The three gate booleans carry external policy into an otherwise
    purely P1-driven brain. The brain has no price model; on a dynamic
    tariff "zero the meter" stops being the right objective (a cheap
    hour spent covering an EV load is stored energy sold at the wrong
    price). Home Assistant decides, these flags tell the brain.
    """

    __slots__ = (
        "force_charge",
        "inhibit_charge",
        "inhibit_discharge",
        "p1",
        "pib_socs",
        "pibs",
        "solar",
        "zen_power",
        "zen_soc",
    )

    def __init__(
        self,
        *,
        p1: float,
        pibs: list[float] | tuple[float, ...],
        pib_socs: list[float] | tuple[float, ...],
        zen_power: float,
        zen_soc: float,
        solar: float = 0,
        inhibit_discharge: bool = False,
        inhibit_charge: bool = False,
        force_charge: bool = False,
    ):
        self.p1 = p1
        self.pibs = list(pibs)
        self.pib_socs = list(pib_socs)
        self.zen_power = zen_power
        self.zen_soc = zen_soc
        self.solar = solar
        self.inhibit_discharge = inhibit_discharge
        self.inhibit_charge = inhibit_charge
        self.force_charge = force_charge

        if len(self.pibs) != len(self.pib_socs):
            raise ValueError(
                f"Reading: pibs has {len(self.pibs)} entries but pib_socs has "
                f"{len(self.pib_socs)} — they must be the same length."
            )

    @property
    def pib_count(self) -> int:
        return len(self.pibs)

    def __repr__(self) -> str:
        return (
            f"Reading(p1={self.p1}, pibs={self.pibs}, pib_socs={self.pib_socs}, "
            f"zen_power={self.zen_power}, zen_soc={self.zen_soc}, solar={self.solar}, "
            f"inhibit_discharge={self.inhibit_discharge}, inhibit_charge={self.inhibit_charge}, "
            f"force_charge={self.force_charge})"
        )


@dataclass
class Decision:
    """What the coordinator decided this tick."""

    target: int  # Zendure target power (W). +charge / -discharge / 0 standby
    zone: str  # FSM state name (SLEEP / CHARGE / DISCHARGE / ...)
    send: bool  # whether to actually send the target to Zendure
    pib_mode: str | None = None  # "standby", "zero", "to_full", or None = no change
    pib_permissions: list[str] | None = None
    inhibited: bool = False  # target forced to standby by an inhibit gate
