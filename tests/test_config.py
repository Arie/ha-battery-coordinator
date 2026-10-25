"""Config loads from /data/options.json (HA add-on) or env vars (Docker)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "battery-coordinator" / "app"))

from config import Config


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every env var Config reads so tests are deterministic."""
    for var in [
        "ZENDURE_IP",
        "HW_P1_IP",
        "HW_P1_TOKEN",
        "HA_URL",
        "HA_TOKEN",
        "SOLAR_ENTITY",
        "SETPOINT_ENTITY",
        "CURTAIL_EXPORT_ENTITY",
        "IMPORT_PAYS_ENTITY",
        "SOLAR_LIMIT_ENTITY",
        "SOLAR_RATED_W",
        "CURTAIL_TARGET_W",
        "CURTAIL_DEADBAND_W",
        "CURTAIL_STEP_PCT",
        "CURTAIL_MIN_WRITE_S",
        "CURTAIL_SETTLE_DOWN_S",
        "CURTAIL_SETTLE_UP_S",
        "ZEN_MAX_CHARGE_W",
        "ZEN_MAX_DISCHARGE_W",
        "ZEN_SOC_MIN",
        "ZEN_SOC_MAX",
        "READ_TIMEOUT",
        "WRITE_TIMEOUT",
        "LOG_LEVEL",
        "DRY_RUN",
        "SUPERVISOR_TOKEN",
    ]:
        monkeypatch.delenv(var, raising=False)
    for key in [
        "step_holdoff_s",
        "flip_s",
        "wake_charge_s",
        "wake_discharge_s",
        "help_enter_s",
        "help_exit_s",
        "pib_high_w",
        "pib_maxed_w",
        "pib_low_w",
        "pib_taper_cap_w",
        "nom_deadband_w",
        "p1_export_w",
        "p1_import_w",
    ]:
        monkeypatch.delenv(f"BRAIN_{key.upper()}", raising=False)


def _missing_options_path(tmp_path):
    """An options path that's guaranteed not to exist."""
    return str(tmp_path / "no-such-file.json")


class TestEnvVarPath:
    """When /data/options.json doesn't exist, Config reads env vars."""

    def test_required_fields_from_env(self, clean_env, monkeypatch, tmp_path):
        monkeypatch.setenv("ZENDURE_IP", "10.0.0.5")
        monkeypatch.setenv("HW_P1_IP", "10.0.0.6")
        monkeypatch.setenv("HW_P1_TOKEN", "abc")
        c = Config(options_path=_missing_options_path(tmp_path))
        assert c.zendure_ip == "10.0.0.5"
        assert c.hw_p1_ip == "10.0.0.6"
        assert c.hw_p1_token == "abc"
        assert c.validate() == []

    def test_brain_defaults_via_env(self, clean_env, tmp_path):
        c = Config(options_path=_missing_options_path(tmp_path))
        assert c.brain["pib_high_w"] == 1200
        assert c.brain["flip_s"] == 30

    def test_brain_override_via_env(self, clean_env, monkeypatch, tmp_path):
        monkeypatch.setenv("BRAIN_PIB_HIGH_W", "1500")
        monkeypatch.setenv("BRAIN_FLIP_S", "45")
        c = Config(options_path=_missing_options_path(tmp_path))
        assert c.brain["pib_high_w"] == 1500
        assert c.brain["flip_s"] == 45

    def test_validate_catches_missing(self, clean_env, tmp_path):
        c = Config(options_path=_missing_options_path(tmp_path))
        errors = c.validate()
        assert "zendure_ip is required" in errors
        assert "hw_p1_ip is required" in errors
        assert "hw_p1_token is required" in errors


class TestAddonOptionsPath:
    """When /data/options.json exists, Config reads it (env vars ignored)."""

    def _write_options(self, tmp_path, options):
        p = tmp_path / "options.json"
        p.write_text(json.dumps(options))
        return str(p)

    def test_basic_options_load(self, clean_env, tmp_path):
        path = self._write_options(
            tmp_path,
            {
                "zendure_ip": "192.168.1.10",
                "hw_p1_ip": "192.168.1.11",
                "hw_p1_token": "tok",
            },
        )
        c = Config(options_path=path)
        assert c.zendure_ip == "192.168.1.10"
        assert c.hw_p1_ip == "192.168.1.11"
        assert c.hw_p1_token == "tok"
        assert c.validate() == []

    def test_brain_defaults_when_omitted(self, clean_env, tmp_path):
        path = self._write_options(
            tmp_path,
            {
                "zendure_ip": "x",
                "hw_p1_ip": "y",
                "hw_p1_token": "z",
            },
        )
        c = Config(options_path=path)
        assert c.brain["pib_high_w"] == 1200
        assert c.brain["step_holdoff_s"] == 15
        assert c.brain["p1_export_w"] == -100

    def test_brain_overrides_in_options(self, clean_env, tmp_path):
        path = self._write_options(
            tmp_path,
            {
                "zendure_ip": "x",
                "hw_p1_ip": "y",
                "hw_p1_token": "z",
                "brain_pib_high_w": 1500,
                "brain_flip_s": 60,
                "brain_p1_export_w": -200,
            },
        )
        c = Config(options_path=path)
        assert c.brain["pib_high_w"] == 1500
        assert c.brain["flip_s"] == 60
        assert c.brain["p1_export_w"] == -200

    def test_solar_uses_supervisor_proxy(self, clean_env, monkeypatch, tmp_path):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-secret")
        path = self._write_options(
            tmp_path,
            {
                "zendure_ip": "x",
                "hw_p1_ip": "y",
                "hw_p1_token": "z",
                "solar_entity": "sensor.solar_power",
            },
        )
        c = Config(options_path=path)
        assert c.solar_entity == "sensor.solar_power"
        assert c.ha_url == "http://supervisor/core"
        assert c.ha_token == "supervisor-secret"

    def test_no_solar_no_ha_url(self, clean_env, tmp_path):
        path = self._write_options(
            tmp_path,
            {
                "zendure_ip": "x",
                "hw_p1_ip": "y",
                "hw_p1_token": "z",
            },
        )
        c = Config(options_path=path)
        assert c.solar_entity == ""
        assert c.ha_url == ""

    def test_options_take_priority_over_env(self, clean_env, monkeypatch, tmp_path):
        # Even with env vars set, options.json wins when present.
        monkeypatch.setenv("ZENDURE_IP", "ENV_VALUE")
        path = self._write_options(
            tmp_path,
            {
                "zendure_ip": "OPTIONS_VALUE",
                "hw_p1_ip": "y",
                "hw_p1_token": "z",
            },
        )
        c = Config(options_path=path)
        assert c.zendure_ip == "OPTIONS_VALUE"

    def test_pib_entity_lists_default_to_empty_when_omitted(self, clean_env, tmp_path):
        # The default HomeWizard entity names live in config.yaml's `options`
        # block (so HA Supervisor pre-fills the form). Config() itself does
        # NOT inject defaults — when a key is absent from options.json, the
        # list is empty.
        path = self._write_options(
            tmp_path,
            {
                "zendure_ip": "x",
                "hw_p1_ip": "y",
                "hw_p1_token": "z",
            },
        )
        c = Config(options_path=path)
        assert c.pib_soc_entities == []
        assert c.pib_power_entities == []

    def test_pib_entity_lists_capped_at_4(self, clean_env, tmp_path):
        path = self._write_options(
            tmp_path,
            {
                "zendure_ip": "x",
                "hw_p1_ip": "y",
                "hw_p1_token": "z",
                "pib_soc_entities": [f"sensor.s{i}" for i in range(6)],
                "pib_power_entities": [f"sensor.p{i}" for i in range(5)],
            },
        )
        c = Config(options_path=path)
        assert len(c.pib_soc_entities) == 4
        assert len(c.pib_power_entities) == 4
        assert c.pib_soc_entities[0] == "sensor.s0"

    def test_pib_entities_trigger_supervisor_proxy(self, clean_env, monkeypatch, tmp_path):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-secret")
        path = self._write_options(
            tmp_path,
            {
                "zendure_ip": "x",
                "hw_p1_ip": "y",
                "hw_p1_token": "z",
                "pib_soc_entities": ["sensor.pib_soc"],
            },
        )
        c = Config(options_path=path)
        # PIB entities require HA proxy even without a solar entity.
        assert c.ha_url == "http://supervisor/core"
        assert c.ha_token == "supervisor-secret"

    def test_pib_entities_via_env_csv(self, clean_env, monkeypatch, tmp_path):
        monkeypatch.setenv("PIB_SOC_ENTITIES", "sensor.a, sensor.b ,  ,sensor.c")
        c = Config(options_path=_missing_options_path(tmp_path))
        assert c.pib_soc_entities == ["sensor.a", "sensor.b", "sensor.c"]

    def test_brain_kwargs_includes_everything(self, clean_env, tmp_path):
        path = self._write_options(
            tmp_path,
            {
                "zendure_ip": "x",
                "hw_p1_ip": "y",
                "hw_p1_token": "z",
                "zen_max_charge_w": 1800,
                "brain_pib_high_w": 1500,
            },
        )
        c = Config(options_path=path)
        kw = c.brain_kwargs()
        assert kw["max_charge_w"] == 1800
        assert kw["pib_high_w"] == 1500
        assert kw["zen_soc_max"] == 100
        assert kw["flip_s"] == 30  # default


class TestValidate:
    def test_solar_without_ha_url_is_error_via_env(self, clean_env, monkeypatch, tmp_path):
        monkeypatch.setenv("ZENDURE_IP", "x")
        monkeypatch.setenv("HW_P1_IP", "y")
        monkeypatch.setenv("HW_P1_TOKEN", "z")
        monkeypatch.setenv("SOLAR_ENTITY", "sensor.solar")
        c = Config(options_path=_missing_options_path(tmp_path))
        errors = c.validate()
        assert any("ha_url is required" in e for e in errors), errors

    def test_soc_min_must_be_less_than_max(self, clean_env, monkeypatch, tmp_path):
        monkeypatch.setenv("ZENDURE_IP", "x")
        monkeypatch.setenv("HW_P1_IP", "y")
        monkeypatch.setenv("HW_P1_TOKEN", "z")
        monkeypatch.setenv("ZEN_SOC_MIN", "80")
        monkeypatch.setenv("ZEN_SOC_MAX", "60")
        c = Config(options_path=_missing_options_path(tmp_path))
        assert "zen_soc_min must be less than zen_soc_max" in c.validate()

    def test_pib_lists_must_have_same_length(self, clean_env, tmp_path):
        path = tmp_path / "options.json"
        path.write_text(
            json.dumps(
                {
                    "zendure_ip": "x",
                    "hw_p1_ip": "y",
                    "hw_p1_token": "z",
                    "pib_soc_entities": ["sensor.a", "sensor.b"],
                    "pib_power_entities": ["sensor.p"],
                }
            )
        )
        c = Config(options_path=str(path))
        errors = c.validate()
        assert any("must have the same length" in e for e in errors), errors

    def test_malformed_int_surfaces_through_validate(self, clean_env, monkeypatch, tmp_path):
        # A user typo in env-var mode shouldn't crash startup with a
        # ValueError — it should produce a clear validation error.
        monkeypatch.setenv("ZENDURE_IP", "x")
        monkeypatch.setenv("HW_P1_IP", "y")
        monkeypatch.setenv("HW_P1_TOKEN", "z")
        monkeypatch.setenv("ZEN_MAX_CHARGE_W", "not-a-number")
        c = Config(options_path=_missing_options_path(tmp_path))
        errors = c.validate()
        assert any("ZEN_MAX_CHARGE_W" in e for e in errors), errors

    def test_negative_zen_max_charge_w_rejected(self, clean_env, monkeypatch, tmp_path):
        monkeypatch.setenv("ZENDURE_IP", "x")
        monkeypatch.setenv("HW_P1_IP", "y")
        monkeypatch.setenv("HW_P1_TOKEN", "z")
        monkeypatch.setenv("ZEN_MAX_CHARGE_W", "-100")
        c = Config(options_path=_missing_options_path(tmp_path))
        errors = c.validate()
        assert any("zen_max_charge_w must be > 0" in e for e in errors), errors

    def test_pib_entities_via_env_require_ha_url(self, clean_env, monkeypatch, tmp_path):
        # Setting PIB_SOC_ENTITIES without HA_URL is a misconfiguration —
        # those reads silently return 0 forever. validate() should catch it.
        monkeypatch.setenv("ZENDURE_IP", "x")
        monkeypatch.setenv("HW_P1_IP", "y")
        monkeypatch.setenv("HW_P1_TOKEN", "z")
        monkeypatch.setenv("PIB_SOC_ENTITIES", "sensor.a")
        c = Config(options_path=_missing_options_path(tmp_path))
        errors = c.validate()
        assert any("ha_url is required" in e for e in errors), errors

    def test_pib_power_only_rejected(self, clean_env, monkeypatch, tmp_path):
        # Configuring pib_power_entities without pib_soc_entities makes
        # device_io pad pib_socs with zeros — brain then sees ghost
        # 0%-SOC PIBs and over/under-corrects. Reject the asymmetry up
        # front instead of silently producing a broken Reading.
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
        path = tmp_path / "options.json"
        path.write_text(
            json.dumps(
                {
                    "zendure_ip": "x",
                    "hw_p1_ip": "y",
                    "hw_p1_token": "z",
                    "pib_power_entities": ["sensor.pib_power_1"],
                }
            )
        )
        c = Config(options_path=str(path))
        errors = c.validate()
        assert any("pib_soc_entities" in e and "pib_power_entities" in e for e in errors), errors

    def test_pib_soc_only_rejected(self, clean_env, monkeypatch, tmp_path):
        # Mirror: SOC without power leaves the brain reading 0W per PIB
        # (or evenly-split combined power) — also misleading. Reject.
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
        path = tmp_path / "options.json"
        path.write_text(
            json.dumps(
                {
                    "zendure_ip": "x",
                    "hw_p1_ip": "y",
                    "hw_p1_token": "z",
                    "pib_soc_entities": ["sensor.pib_soc_1"],
                }
            )
        )
        c = Config(options_path=str(path))
        errors = c.validate()
        assert any("pib_soc_entities" in e and "pib_power_entities" in e for e in errors), errors

    def test_malformed_float_timeout_surfaces_through_validate(self, clean_env, monkeypatch, tmp_path):
        # A user typo in READ_TIMEOUT used to crash startup with a bare
        # ValueError from float() — bypassing the rest of validate(). It
        # should produce a clean validation error like the int fields do.
        monkeypatch.setenv("ZENDURE_IP", "x")
        monkeypatch.setenv("HW_P1_IP", "y")
        monkeypatch.setenv("HW_P1_TOKEN", "z")
        monkeypatch.setenv("READ_TIMEOUT", "fast")
        c = Config(options_path=_missing_options_path(tmp_path))
        errors = c.validate()
        assert any("READ_TIMEOUT" in e for e in errors), errors

    def test_invalid_log_level_surfaces_through_validate(self, clean_env, monkeypatch, tmp_path):
        # A typo in LOG_LEVEL used to crash startup with a bare ValueError
        # from log.setLevel("VERBOSE") — bypassing the rest of validate().
        # The addon-options path is schema-checked; the env-var path is not,
        # so duplicate the check inside validate().
        monkeypatch.setenv("ZENDURE_IP", "x")
        monkeypatch.setenv("HW_P1_IP", "y")
        monkeypatch.setenv("HW_P1_TOKEN", "z")
        monkeypatch.setenv("LOG_LEVEL", "verbose")
        c = Config(options_path=_missing_options_path(tmp_path))
        errors = c.validate()
        assert any("log_level" in e.lower() for e in errors), errors

    def test_valid_log_level_lowercase_passes(self, clean_env, monkeypatch, tmp_path):
        # The existing addon path stores `info` from options.json; the env
        # path stores upper-cased — both should pass.
        monkeypatch.setenv("ZENDURE_IP", "x")
        monkeypatch.setenv("HW_P1_IP", "y")
        monkeypatch.setenv("HW_P1_TOKEN", "z")
        monkeypatch.setenv("LOG_LEVEL", "debug")
        c = Config(options_path=_missing_options_path(tmp_path))
        assert c.validate() == []

    def test_pib_lists_same_length_passes(self, clean_env, monkeypatch, tmp_path):
        # Same-length PIB lists shouldn't add their own validation errors.
        # Need SUPERVISOR_TOKEN since PIB entities trigger HA proxy mode.
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
        path = tmp_path / "options.json"
        path.write_text(
            json.dumps(
                {
                    "zendure_ip": "x",
                    "hw_p1_ip": "y",
                    "hw_p1_token": "z",
                    "pib_soc_entities": ["sensor.a", "sensor.b"],
                    "pib_power_entities": ["sensor.p", "sensor.q"],
                }
            )
        )
        c = Config(options_path=str(path))
        assert c.validate() == []


class TestTariffGateOptions:
    """Gate entities + the grid-charge opt-in load from both config paths."""

    def test_gate_entities_default_empty(self, monkeypatch):
        monkeypatch.setattr(Config, "_options_path", "/nonexistent", raising=False)
        c = Config()
        c._set_defaults()
        assert c.inhibit_discharge_entity == ""
        assert c.inhibit_charge_entity == ""
        assert c.force_charge_entity == ""
        assert c.allow_grid_charge is False

    def test_gate_entities_from_options(self):
        c = Config()
        c._set_defaults()
        c._load_from_options(
            {
                "zendure_ip": "1.2.3.4",
                "hw_p1_ip": "1.2.3.5",
                "hw_p1_token": "tok",
                "inhibit_discharge_entity": "input_boolean.hold_soc",
                "inhibit_charge_entity": "input_boolean.no_charge",
                "force_charge_entity": "input_boolean.cheap_power",
                "allow_grid_charge": True,
            }
        )
        assert c.inhibit_discharge_entity == "input_boolean.hold_soc"
        assert c.allow_grid_charge is True

    def test_gate_entities_require_ha_proxy(self):
        """A gate entity alone must still pull in the HA connection."""
        c = Config()
        c._set_defaults()
        c._load_from_options(
            {
                "zendure_ip": "1.2.3.4",
                "hw_p1_ip": "1.2.3.5",
                "hw_p1_token": "tok",
                "inhibit_discharge_entity": "input_boolean.hold_soc",
            }
        )
        assert c.ha_url == "http://supervisor/core"

    def test_allow_grid_charge_without_trigger_is_an_error(self):
        """Opting out of a safety rule with no way to trigger it is a typo."""
        c = Config()
        c._set_defaults()
        c.zendure_ip = "1.2.3.4"
        c.hw_p1_ip = "1.2.3.5"
        c.hw_p1_token = "tok"
        c.allow_grid_charge = True
        c.force_charge_entity = ""
        errors = c.validate()
        assert any("force_charge_entity" in e for e in errors)

    def test_allow_grid_charge_with_trigger_validates(self):
        c = Config()
        c._set_defaults()
        c.zendure_ip = "1.2.3.4"
        c.hw_p1_ip = "1.2.3.5"
        c.hw_p1_token = "tok"
        c.ha_url = "http://supervisor/core"
        c.ha_token = "tok"
        c.allow_grid_charge = True
        c.force_charge_entity = "input_boolean.cheap_power"
        assert c.validate() == []

    def test_env_path_reads_gates(self, monkeypatch):
        monkeypatch.setenv("INHIBIT_DISCHARGE_ENTITY", "binary_sensor.cheap")
        monkeypatch.setenv("FORCE_CHARGE_ENTITY", "input_boolean.fill")
        monkeypatch.setenv("ALLOW_GRID_CHARGE", "true")
        c = Config()
        c._set_defaults()
        c._load_from_env()
        assert c.inhibit_discharge_entity == "binary_sensor.cheap"
        assert c.force_charge_entity == "input_boolean.fill"
        assert c.allow_grid_charge is True

    def test_force_discharge_options_from_options(self):
        c = Config()
        c._set_defaults()
        c._load_from_options(
            {
                "zendure_ip": "1.2.3.4",
                "hw_p1_ip": "1.2.3.5",
                "hw_p1_token": "tok",
                "force_discharge_entity": "input_boolean.dump_now",
                "allow_grid_export": True,
            }
        )
        assert c.force_discharge_entity == "input_boolean.dump_now"
        assert c.allow_grid_export is True
        assert c.ha_url == "http://supervisor/core"

    def test_allow_grid_export_without_trigger_is_an_error(self):
        """Opting out of the no-export rule with no way to trigger it is a typo."""
        c = Config()
        c._set_defaults()
        c.zendure_ip, c.hw_p1_ip, c.hw_p1_token = "1.2.3.4", "1.2.3.5", "tok"
        c.allow_grid_export = True
        c.force_discharge_entity = ""
        assert any("force_discharge_entity" in e for e in c.validate())

    def test_export_and_charge_flags_are_independent(self):
        """allow_grid_charge must not imply allow_grid_export or vice versa."""
        c = Config()
        c._set_defaults()
        c.zendure_ip, c.hw_p1_ip, c.hw_p1_token = "1.2.3.4", "1.2.3.5", "tok"
        c.ha_url, c.ha_token = "http://supervisor/core", "tok"
        c.allow_grid_charge = True
        c.force_charge_entity = "input_boolean.cheap"
        assert c.allow_grid_export is False
        assert c.validate() == []

    def test_env_path_reads_export_flags(self, monkeypatch):
        monkeypatch.setenv("FORCE_DISCHARGE_ENTITY", "input_boolean.dump")
        monkeypatch.setenv("ALLOW_GRID_EXPORT", "true")
        c = Config()
        c._set_defaults()
        c._load_from_env()
        assert c.force_discharge_entity == "input_boolean.dump"
        assert c.allow_grid_export is True


class TestSetpointEntity:
    """An external optimiser's wattage entity: needs both grid opt-ins."""

    def _write_options(self, tmp_path, options):
        p = tmp_path / "options.json"
        p.write_text(json.dumps(options))
        return str(p)

    def _base(self, **extra):
        return {"zendure_ip": "x", "hw_p1_ip": "y", "hw_p1_token": "z", **extra}

    def test_setpoint_entity_loads_from_options(self, clean_env, monkeypatch, tmp_path):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
        c = Config(
            options_path=self._write_options(
                tmp_path,
                self._base(setpoint_entity="sensor.battery_setpoint", allow_grid_charge=True, allow_grid_export=True),
            )
        )
        assert c.setpoint_entity == "sensor.battery_setpoint"
        assert c.ha_url  # it is an HA-backed entity like the gates
        assert c.validate() == []

    def test_setpoint_entity_alone_satisfies_both_opt_ins(self, clean_env, monkeypatch, tmp_path):
        """No force entities needed: the setpoint is what triggers import and export."""
        monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
        c = Config(
            options_path=self._write_options(
                tmp_path,
                self._base(setpoint_entity="sensor.sp", allow_grid_charge=True, allow_grid_export=True),
            )
        )
        assert c.force_charge_entity == "" and c.force_discharge_entity == ""
        assert c.validate() == []

    def test_setpoint_entity_requires_both_opt_ins(self, clean_env, monkeypatch, tmp_path):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
        for flags in ({}, {"allow_grid_charge": True}, {"allow_grid_export": True}):
            c = Config(options_path=self._write_options(tmp_path, self._base(setpoint_entity="sensor.sp", **flags)))
            errs = [e for e in c.validate() if "setpoint_entity" in e]
            assert errs, flags

    def test_setpoint_entity_from_env(self, clean_env, monkeypatch, tmp_path):
        monkeypatch.setenv("ZENDURE_IP", "x")
        monkeypatch.setenv("HW_P1_IP", "y")
        monkeypatch.setenv("HW_P1_TOKEN", "z")
        monkeypatch.setenv("HA_URL", "http://ha")
        monkeypatch.setenv("HA_TOKEN", "t")
        monkeypatch.setenv("SETPOINT_ENTITY", "input_number.sp")
        monkeypatch.setenv("ALLOW_GRID_CHARGE", "true")
        monkeypatch.setenv("ALLOW_GRID_EXPORT", "true")
        c = Config(options_path=_missing_options_path(tmp_path))
        assert c.setpoint_entity == "input_number.sp"
        assert c.validate() == []


class TestCurtailmentOptions:
    """Solar curtailment: gate entities, limit entity, and tuning knobs."""

    def _write_options(self, tmp_path, options):
        p = tmp_path / "options.json"
        p.write_text(json.dumps(options))
        return str(p)

    def _base(self, **extra):
        return {"zendure_ip": "x", "hw_p1_ip": "y", "hw_p1_token": "z", **extra}

    def _cfg(self, tmp_path, **extra):
        return Config(options_path=self._write_options(tmp_path, self._base(**extra)))

    def test_curtail_defaults(self, clean_env, tmp_path):
        c = self._cfg(tmp_path)
        assert (
            c.solar_rated_w,
            c.curtail_target_w,
            c.curtail_deadband_w,
            c.curtail_step_pct,
            c.curtail_min_write_s,
            c.curtail_settle_down_s,
            c.curtail_settle_up_s,
        ) == (6000, -30, 30, 1, 3, 20, 5)
        assert c.curtail_export_entity == c.import_pays_entity == c.solar_limit_entity == ""

    def test_gate_requires_limit_and_solar_entity(self, clean_env, monkeypatch, tmp_path):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
        for gate in ("curtail_export_entity", "import_pays_entity"):
            c = self._cfg(tmp_path, **{gate: "binary_sensor.g"})
            assert any("solar_limit_entity" in e for e in c.validate()), gate
            c = self._cfg(tmp_path, **{gate: "binary_sensor.g", "solar_limit_entity": "number.lim"})
            assert any("solar_entity" in e for e in c.validate()), gate
            c = self._cfg(
                tmp_path,
                **{gate: "binary_sensor.g", "solar_limit_entity": "number.lim", "solar_entity": "sensor.pv"},
            )
            assert c.validate() == [], gate

    def test_limit_entity_alone_is_valid(self, clean_env, monkeypatch, tmp_path):
        """Without a gate it only ever restores 100%."""
        monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
        c = self._cfg(tmp_path, solar_limit_entity="number.lim")
        assert c.validate() == []

    def test_curtail_entities_use_ha_proxy(self, clean_env, monkeypatch, tmp_path):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
        for key in ("curtail_export_entity", "import_pays_entity", "solar_limit_entity"):
            c = self._cfg(tmp_path, **{key: "x.y"})
            assert c.ha_url == "http://supervisor/core", key

    def test_curtail_bounds(self, clean_env, monkeypatch, tmp_path):
        monkeypatch.setenv("ZENDURE_IP", "x")
        monkeypatch.setenv("HW_P1_IP", "y")
        monkeypatch.setenv("HW_P1_TOKEN", "z")
        for var, val in [
            ("SOLAR_RATED_W", "0"),
            ("CURTAIL_STEP_PCT", "0"),
            ("CURTAIL_STEP_PCT", "101"),
            ("CURTAIL_MIN_WRITE_S", "0"),
            ("CURTAIL_DEADBAND_W", "-1"),
            ("CURTAIL_SETTLE_DOWN_S", "-1"),
            ("CURTAIL_SETTLE_UP_S", "-1"),
        ]:
            monkeypatch.setenv(var, val)
            c = Config(options_path=_missing_options_path(tmp_path))
            assert c.validate(), (var, val)
            monkeypatch.delenv(var)

    def test_curtailer_kwargs(self, clean_env, tmp_path):
        c = self._cfg(tmp_path)
        assert c.curtailer_kwargs() == {
            "rated_w": 6000,
            "target_w": -30,
            "deadband_w": 30,
            "step_pct": 1,
            "min_write_s": 3,
            "settle_down_s": 20,
            "settle_up_s": 5,
        }

    def test_curtail_options_from_env(self, clean_env, monkeypatch, tmp_path):
        monkeypatch.setenv("CURTAIL_EXPORT_ENTITY", "binary_sensor.a")
        monkeypatch.setenv("IMPORT_PAYS_ENTITY", "binary_sensor.b")
        monkeypatch.setenv("SOLAR_LIMIT_ENTITY", "number.lim")
        monkeypatch.setenv("SOLAR_RATED_W", "8000")
        c = Config(options_path=_missing_options_path(tmp_path))
        assert c.curtail_export_entity == "binary_sensor.a"
        assert c.import_pays_entity == "binary_sensor.b"
        assert c.solar_limit_entity == "number.lim"
        assert c.solar_rated_w == 8000
        assert c.curtailer_kwargs()["rated_w"] == 8000
