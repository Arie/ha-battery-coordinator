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
from typing import Any

from brains.permission_fsm import PermissionFSM

# HA Supervisor writes the add-on's options here inside the container.
ADDON_OPTIONS_PATH = "/data/options.json"


def _split_csv(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def _safe_int(value: Any, *, field: str, default: int) -> int:
    """Parse an int, returning default with a clear error if the value
    is malformed. Used in both options-file and env-var paths so a typo
    in user config produces a useful message via validate() instead of
    a TypeError at startup."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise _ConfigParseError(f"{field}: cannot parse {value!r} as int") from None


def _safe_float(value: Any, *, field: str, default: float) -> float:
    """Float counterpart of _safe_int — same purpose, same error path."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        raise _ConfigParseError(f"{field}: cannot parse {value!r} as float") from None


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
        self.inhibit_discharge_entity = ""
        self.inhibit_charge_entity = ""
        self.force_charge_entity = ""
        self.force_discharge_entity = ""
        self.setpoint_entity = ""
        self.allow_grid_charge = False
        self.allow_grid_export = False
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

    def _load_from_options(self, o: dict[str, Any]) -> None:
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
        self.inhibit_discharge_entity = o.get("inhibit_discharge_entity", "") or ""
        self.inhibit_charge_entity = o.get("inhibit_charge_entity", "") or ""
        self.force_charge_entity = o.get("force_charge_entity", "") or ""
        self.force_discharge_entity = o.get("force_discharge_entity", "") or ""
        self.setpoint_entity = o.get("setpoint_entity", "") or ""
        self.allow_grid_charge = bool(o.get("allow_grid_charge", False))
        self.allow_grid_export = bool(o.get("allow_grid_export", False))
        needs_ha = bool(self.solar_entity or self.pib_soc_entities or self.pib_power_entities or self._gate_entities())
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
        self.inhibit_discharge_entity = os.getenv("INHIBIT_DISCHARGE_ENTITY", "")
        self.inhibit_charge_entity = os.getenv("INHIBIT_CHARGE_ENTITY", "")
        self.force_charge_entity = os.getenv("FORCE_CHARGE_ENTITY", "")
        self.force_discharge_entity = os.getenv("FORCE_DISCHARGE_ENTITY", "")
        self.setpoint_entity = os.getenv("SETPOINT_ENTITY", "")
        self.allow_grid_charge = os.getenv("ALLOW_GRID_CHARGE", "").lower() in ("1", "true", "yes")
        self.allow_grid_export = os.getenv("ALLOW_GRID_EXPORT", "").lower() in ("1", "true", "yes")
        # Comma-separated env vars in standalone mode, e.g.
        # PIB_SOC_ENTITIES="sensor.pib_soc,sensor.pib_soc_2"
        self.pib_soc_entities = _split_csv(os.getenv("PIB_SOC_ENTITIES", ""))[:4]
        self.pib_power_entities = _split_csv(os.getenv("PIB_POWER_ENTITIES", ""))[:4]

        self.ha_url = os.getenv("HA_URL", "")
        self.ha_token = os.getenv("HA_TOKEN", "")

        self.zen_max_charge_w = _safe_int(os.getenv("ZEN_MAX_CHARGE_W"), field="ZEN_MAX_CHARGE_W", default=2400)
        self.zen_max_discharge_w = _safe_int(
            os.getenv("ZEN_MAX_DISCHARGE_W"), field="ZEN_MAX_DISCHARGE_W", default=2400
        )
        self.zen_soc_min = _safe_int(os.getenv("ZEN_SOC_MIN"), field="ZEN_SOC_MIN", default=10)
        self.zen_soc_max = _safe_int(os.getenv("ZEN_SOC_MAX"), field="ZEN_SOC_MAX", default=100)

        self.brain = {
            key: _safe_int(os.getenv(f"BRAIN_{key.upper()}"), field=f"BRAIN_{key.upper()}", default=default)
            for key, default in _BRAIN_DEFAULTS.items()
        }

        self.read_timeout = _safe_float(os.getenv("READ_TIMEOUT"), field="READ_TIMEOUT", default=3.0)
        self.write_timeout = _safe_float(os.getenv("WRITE_TIMEOUT"), field="WRITE_TIMEOUT", default=5.0)

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
        needs_ha = bool(self.solar_entity or self.pib_soc_entities or self.pib_power_entities or self._gate_entities())
        if needs_ha and not self.ha_url:
            errors.append("ha_url is required when solar_entity, pib_*_entities, or gate entities are set")
        # allow_grid_charge without a force_charge_entity is inert — nothing
        # can ever assert the signal. Flag it rather than silently doing
        # nothing, since the user explicitly opted out of a safety rule and
        # will otherwise assume it took effect.
        # A setpoint entity is the other thing that can trigger both, so
        # it satisfies either opt-in on its own.
        if self.allow_grid_export and not (self.force_discharge_entity or self.setpoint_entity):
            errors.append(
                "allow_grid_export is enabled but force_discharge_entity is empty — nothing can trigger an export"
            )
        if self.allow_grid_charge and not (self.force_charge_entity or self.setpoint_entity):
            errors.append(
                "allow_grid_charge is enabled but force_charge_entity is empty — nothing can trigger a grid charge"
            )
        # An optimiser buys and sells by nature, so a setpoint with only
        # one opt-in would silently do half a job. Refuse rather than guess.
        if self.setpoint_entity and not (self.allow_grid_charge and self.allow_grid_export):
            errors.append(
                "setpoint_entity is set but allow_grid_charge and allow_grid_export must both be enabled — "
                "an optimiser's setpoint imports and exports"
            )
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
        # log_level is schema-validated for the addon path, but the env-var
        # path bypasses the schema. A typo (e.g. LOG_LEVEL=verbose) would
        # crash startup at log.setLevel() with a bare ValueError, after
        # validate() had already declared the config OK. Catch it here.
        if self.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            errors.append(f"log_level must be one of debug/info/warning/error (got {self.log_level!r})")
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

    def _gate_entities(self) -> list[str]:
        """The configured tariff-gate entities, if any."""
        return [
            e
            for e in (
                self.inhibit_discharge_entity,
                self.inhibit_charge_entity,
                self.force_charge_entity,
                self.force_discharge_entity,
                self.setpoint_entity,
            )
            if e
        ]

    def brain_kwargs(self) -> dict[str, int]:
        """Kwargs for PermissionFSM(...)."""
        return {
            "max_charge_w": self.zen_max_charge_w,
            "max_discharge_w": self.zen_max_discharge_w,
            "zen_soc_max": self.zen_soc_max,
            "zen_soc_min": self.zen_soc_min,
            **self.brain,
        }
