"""Centralized configuration.

Two load paths:
  - HA add-on: read from /data/options.json (written by Supervisor from
    the user's add-on Configuration tab).
  - Docker / standalone: read from environment variables.

Field names are deliberately the same in both paths so the rest of the
code never has to know which mode it's running in.
"""

import json
import os
from pathlib import Path

from brains.permission_fsm import PermissionFSM


# HA Supervisor writes the add-on's options here inside the container.
ADDON_OPTIONS_PATH = "/data/options.json"


def _split_csv(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def _safe_int(value, *, field: str, default: int) -> int:
    """Parse an int, returning default with a clear error if the value
    is malformed. Used in both options-file and env-var paths so a typo
    in user config produces a useful message via validate() instead of
    a TypeError at startup."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise _ConfigParseError(f"{field}: cannot parse {value!r} as int")


class _ConfigParseError(ValueError):
    """Internal error raised by _safe_int / _safe_float and caught by Config
    so validate() can surface a coherent list of problems instead of crashing
    on the first bad field."""

# Single source of truth for brain defaults — defined on PermissionFSM so
# the bare PermissionFSM() constructor and the Config() path can't drift.
_BRAIN_DEFAULTS = PermissionFSM.DEFAULTS


class Config:
    """Battery coordinator configuration."""

    def __init__(self, options_path: str = ADDON_OPTIONS_PATH):
        # Errors collected during parsing — surfaced through validate()
        # instead of raising, so the caller can list every problem at once.
        self._parse_errors: list[str] = []
        # Pre-set every attribute the rest of the code reads, so a parse
        # error halfway through a load path doesn't leave validate() with
        # missing attrs.
        self._set_defaults()
        try:
            if Path(options_path).is_file():
                with open(options_path) as f:
                    self._load_from_options(json.load(f))
            else:
                self._load_from_env()
        except _ConfigParseError as e:
            self._parse_errors.append(str(e))

    def _set_defaults(self) -> None:
        self.zendure_ip = ""
        self.hw_p1_ip = ""
        self.hw_p1_token = ""
        self.ha_url = ""
        self.ha_token = ""
        self.solar_entity = ""
        self.pib_soc_entities: list[str] = []
        self.pib_power_entities: list[str] = []
        self.zen_max_charge_w = 2400
        self.zen_max_discharge_w = 2400
        self.zen_soc_min = 10
        self.zen_soc_max = 100
        self.brain = dict(_BRAIN_DEFAULTS)
        self.read_timeout = 3.0
        self.write_timeout = 5.0
        self.log_level = "INFO"
        self.dry_run = False

    # --- Path 1: HA add-on (Supervisor-managed) ---

    def _load_from_options(self, o: dict) -> None:
        self.zendure_ip = o.get("zendure_ip", "")
        self.hw_p1_ip = o.get("hw_p1_ip", "")
        self.hw_p1_token = o.get("hw_p1_token", "")

        # Per-PIB SOC and power come from HA entities (HW P1 /api/batteries
        # only gives combined power and no SOC at all). Cap at 4 PIBs.
        self.pib_soc_entities = list(o.get("pib_soc_entities", []) or [])[:4]
        self.pib_power_entities = list(o.get("pib_power_entities", []) or [])[:4]

        # Solar via HA, plus the PIB entity reads, both go through the
        # supervisor proxy with the token Supervisor injects.
        self.solar_entity = o.get("solar_entity", "") or ""
        needs_ha = bool(self.solar_entity or self.pib_soc_entities or self.pib_power_entities)
        if needs_ha:
            self.ha_url = "http://supervisor/core"
            self.ha_token = os.getenv("SUPERVISOR_TOKEN", "")
        else:
            self.ha_url = ""
            self.ha_token = ""

        self.zen_max_charge_w = _safe_int(o.get("zen_max_charge_w"), field="zen_max_charge_w", default=2400)
        self.zen_max_discharge_w = _safe_int(o.get("zen_max_discharge_w"), field="zen_max_discharge_w", default=2400)
        self.zen_soc_min = _safe_int(o.get("zen_soc_min"), field="zen_soc_min", default=10)
        self.zen_soc_max = _safe_int(o.get("zen_soc_max"), field="zen_soc_max", default=100)

        self.brain = {
            key: _safe_int(o.get(f"brain_{key}"), field=f"brain_{key}", default=default)
            for key, default in _BRAIN_DEFAULTS.items()
        }

        self.log_level = str(o.get("log_level", "info")).upper()
        self.dry_run = bool(o.get("dry_run", False))

        self.read_timeout = 3.0
        self.write_timeout = 5.0

    # --- Path 2: Docker / standalone (env vars) ---

    def _load_from_env(self) -> None:
        self.zendure_ip = os.getenv("ZENDURE_IP", "")
        self.hw_p1_ip = os.getenv("HW_P1_IP", "")
        self.hw_p1_token = os.getenv("HW_P1_TOKEN", "")

        self.solar_entity = os.getenv("SOLAR_ENTITY", "")
        # Comma-separated env vars in standalone mode, e.g.
        # PIB_SOC_ENTITIES="sensor.pib_soc,sensor.pib_soc_2"
        self.pib_soc_entities = _split_csv(os.getenv("PIB_SOC_ENTITIES", ""))[:4]
        self.pib_power_entities = _split_csv(os.getenv("PIB_POWER_ENTITIES", ""))[:4]

        self.ha_url = os.getenv("HA_URL", "")
        self.ha_token = os.getenv("HA_TOKEN", "")

        self.zen_max_charge_w = _safe_int(os.getenv("ZEN_MAX_CHARGE_W"), field="ZEN_MAX_CHARGE_W", default=2400)
        self.zen_max_discharge_w = _safe_int(os.getenv("ZEN_MAX_DISCHARGE_W"), field="ZEN_MAX_DISCHARGE_W", default=2400)
        self.zen_soc_min = _safe_int(os.getenv("ZEN_SOC_MIN"), field="ZEN_SOC_MIN", default=10)
        self.zen_soc_max = _safe_int(os.getenv("ZEN_SOC_MAX"), field="ZEN_SOC_MAX", default=100)

        self.brain = {
            key: _safe_int(os.getenv(f"BRAIN_{key.upper()}"), field=f"BRAIN_{key.upper()}", default=default)
            for key, default in _BRAIN_DEFAULTS.items()
        }

        self.read_timeout = float(os.getenv("READ_TIMEOUT", "3"))
        self.write_timeout = float(os.getenv("WRITE_TIMEOUT", "5"))

        self.log_level = os.getenv("LOG_LEVEL", "INFO").upper()
        self.dry_run = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

    # --- Public API ---

    def validate(self) -> list[str]:
        """Return list of validation errors, empty if config is valid."""
        errors = list(self._parse_errors)
        if not self.zendure_ip:
            errors.append("zendure_ip is required")
        if not self.hw_p1_ip:
            errors.append("hw_p1_ip is required")
        if not self.hw_p1_token:
            errors.append("hw_p1_token is required")
        # Any HA-backed entity needs the HA proxy. Same check for solar,
        # pib_soc, and pib_power so a user setting one without the others
        # still gets a clear error.
        needs_ha = bool(self.solar_entity or self.pib_soc_entities or self.pib_power_entities)
        if needs_ha and not self.ha_url:
            errors.append("ha_url is required when solar_entity or pib_*_entities are set")
        if self.ha_url and not self.ha_token:
            errors.append("ha_token is required when ha_url is set")
        if self.zen_soc_min >= self.zen_soc_max:
            errors.append("zen_soc_min must be less than zen_soc_max")
        # Bounds. The addon's config.yaml schema enforces these; the
        # env-var path bypasses the schema, so duplicate the check here.
        if self.zen_max_charge_w <= 0:
            errors.append(f"zen_max_charge_w must be > 0 (got {self.zen_max_charge_w})")
        if self.zen_max_discharge_w <= 0:
            errors.append(f"zen_max_discharge_w must be > 0 (got {self.zen_max_discharge_w})")
        if not (0 <= self.zen_soc_min <= 100):
            errors.append(f"zen_soc_min must be 0–100 (got {self.zen_soc_min})")
        if not (0 <= self.zen_soc_max <= 100):
            errors.append(f"zen_soc_max must be 0–100 (got {self.zen_soc_max})")
        # Both lists must be configured together. Setting only one makes
        # device_io pad the other with zeros, which the brain reads as
        # ghost 0%-SOC PIBs (or 0W phantom power) and over-corrects on.
        soc_n = len(self.pib_soc_entities)
        power_n = len(self.pib_power_entities)
        if (soc_n > 0) != (power_n > 0):
            errors.append(
                "pib_soc_entities and pib_power_entities must both be set "
                "or both empty — configuring only one creates ghost PIBs "
                "in the brain"
            )
        elif soc_n != power_n:
            errors.append(
                f"pib_soc_entities ({soc_n}) and "
                f"pib_power_entities ({power_n}) must "
                f"have the same length — the brain pairs them by index"
            )
        return errors

    def brain_kwargs(self) -> dict:
        """Kwargs for PermissionFSM(...)."""
        return {
            "max_charge_w": self.zen_max_charge_w,
            "max_discharge_w": self.zen_max_discharge_w,
            "zen_soc_max": self.zen_soc_max,
            "zen_soc_min": self.zen_soc_min,
            **self.brain,
        }
