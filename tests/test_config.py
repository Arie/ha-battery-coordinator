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
        "ZENDURE_IP", "HW_P1_IP", "HW_P1_TOKEN",
        "HA_URL", "HA_TOKEN", "SOLAR_ENTITY",
        "ZEN_MAX_CHARGE_W", "ZEN_MAX_DISCHARGE_W", "ZEN_SOC_MIN", "ZEN_SOC_MAX",
        "READ_TIMEOUT", "WRITE_TIMEOUT", "LOG_LEVEL", "DRY_RUN",
        "SUPERVISOR_TOKEN",
    ]:
        monkeypatch.delenv(var, raising=False)
    for key in [
        "step_holdoff_s", "flip_s", "wake_charge_s", "wake_discharge_s",
        "help_enter_s", "help_exit_s", "pib_high_w", "pib_maxed_w",
        "pib_low_w", "pib_taper_cap_w", "nom_deadband_w",
        "p1_export_w", "p1_import_w",
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
        path = self._write_options(tmp_path, {
            "zendure_ip": "192.168.1.10",
            "hw_p1_ip": "192.168.1.11",
            "hw_p1_token": "tok",
        })
        c = Config(options_path=path)
        assert c.zendure_ip == "192.168.1.10"
        assert c.hw_p1_ip == "192.168.1.11"
        assert c.hw_p1_token == "tok"
        assert c.validate() == []

    def test_brain_defaults_when_omitted(self, clean_env, tmp_path):
        path = self._write_options(tmp_path, {
            "zendure_ip": "x", "hw_p1_ip": "y", "hw_p1_token": "z",
        })
        c = Config(options_path=path)
        assert c.brain["pib_high_w"] == 1200
        assert c.brain["step_holdoff_s"] == 15
        assert c.brain["p1_export_w"] == -100

    def test_brain_overrides_in_options(self, clean_env, tmp_path):
        path = self._write_options(tmp_path, {
            "zendure_ip": "x", "hw_p1_ip": "y", "hw_p1_token": "z",
            "brain_pib_high_w": 1500,
            "brain_flip_s": 60,
            "brain_p1_export_w": -200,
        })
        c = Config(options_path=path)
        assert c.brain["pib_high_w"] == 1500
        assert c.brain["flip_s"] == 60
        assert c.brain["p1_export_w"] == -200

    def test_solar_uses_supervisor_proxy(self, clean_env, monkeypatch, tmp_path):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-secret")
        path = self._write_options(tmp_path, {
            "zendure_ip": "x", "hw_p1_ip": "y", "hw_p1_token": "z",
            "solar_entity": "sensor.solar_power",
        })
        c = Config(options_path=path)
        assert c.solar_entity == "sensor.solar_power"
        assert c.ha_url == "http://supervisor/core"
        assert c.ha_token == "supervisor-secret"

    def test_no_solar_no_ha_url(self, clean_env, tmp_path):
        path = self._write_options(tmp_path, {
            "zendure_ip": "x", "hw_p1_ip": "y", "hw_p1_token": "z",
        })
        c = Config(options_path=path)
        assert c.solar_entity == ""
        assert c.ha_url == ""

    def test_options_take_priority_over_env(self, clean_env, monkeypatch, tmp_path):
        # Even with env vars set, options.json wins when present.
        monkeypatch.setenv("ZENDURE_IP", "ENV_VALUE")
        path = self._write_options(tmp_path, {
            "zendure_ip": "OPTIONS_VALUE",
            "hw_p1_ip": "y", "hw_p1_token": "z",
        })
        c = Config(options_path=path)
        assert c.zendure_ip == "OPTIONS_VALUE"

    def test_pib_entity_lists_default_to_empty_when_omitted(self, clean_env, tmp_path):
        # The default HomeWizard entity names live in config.yaml's `options`
        # block (so HA Supervisor pre-fills the form). Config() itself does
        # NOT inject defaults — when a key is absent from options.json, the
        # list is empty.
        path = self._write_options(tmp_path, {
            "zendure_ip": "x", "hw_p1_ip": "y", "hw_p1_token": "z",
        })
        c = Config(options_path=path)
        assert c.pib_soc_entities == []
        assert c.pib_power_entities == []

    def test_pib_entity_lists_capped_at_4(self, clean_env, tmp_path):
        path = self._write_options(tmp_path, {
            "zendure_ip": "x", "hw_p1_ip": "y", "hw_p1_token": "z",
            "pib_soc_entities": [f"sensor.s{i}" for i in range(6)],
            "pib_power_entities": [f"sensor.p{i}" for i in range(5)],
        })
        c = Config(options_path=path)
        assert len(c.pib_soc_entities) == 4
        assert len(c.pib_power_entities) == 4
        assert c.pib_soc_entities[0] == "sensor.s0"

    def test_pib_entities_trigger_supervisor_proxy(self, clean_env, monkeypatch, tmp_path):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-secret")
        path = self._write_options(tmp_path, {
            "zendure_ip": "x", "hw_p1_ip": "y", "hw_p1_token": "z",
            "pib_soc_entities": ["sensor.pib_soc"],
        })
        c = Config(options_path=path)
        # PIB entities require HA proxy even without a solar entity.
        assert c.ha_url == "http://supervisor/core"
        assert c.ha_token == "supervisor-secret"

    def test_pib_entities_via_env_csv(self, clean_env, monkeypatch, tmp_path):
        monkeypatch.setenv("PIB_SOC_ENTITIES", "sensor.a, sensor.b ,  ,sensor.c")
        c = Config(options_path=_missing_options_path(tmp_path))
        assert c.pib_soc_entities == ["sensor.a", "sensor.b", "sensor.c"]

    def test_brain_kwargs_includes_everything(self, clean_env, tmp_path):
        path = self._write_options(tmp_path, {
            "zendure_ip": "x", "hw_p1_ip": "y", "hw_p1_token": "z",
            "zen_max_charge_w": 1800,
            "brain_pib_high_w": 1500,
        })
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
        path.write_text(json.dumps({
            "zendure_ip": "x", "hw_p1_ip": "y", "hw_p1_token": "z",
            "pib_soc_entities": ["sensor.a", "sensor.b"],
            "pib_power_entities": ["sensor.p"],
        }))
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
        path.write_text(json.dumps({
            "zendure_ip": "x", "hw_p1_ip": "y", "hw_p1_token": "z",
            "pib_power_entities": ["sensor.pib_power_1"],
        }))
        c = Config(options_path=str(path))
        errors = c.validate()
        assert any(
            "pib_soc_entities" in e and "pib_power_entities" in e
            for e in errors
        ), errors

    def test_pib_soc_only_rejected(self, clean_env, monkeypatch, tmp_path):
        # Mirror: SOC without power leaves the brain reading 0W per PIB
        # (or evenly-split combined power) — also misleading. Reject.
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
        path = tmp_path / "options.json"
        path.write_text(json.dumps({
            "zendure_ip": "x", "hw_p1_ip": "y", "hw_p1_token": "z",
            "pib_soc_entities": ["sensor.pib_soc_1"],
        }))
        c = Config(options_path=str(path))
        errors = c.validate()
        assert any(
            "pib_soc_entities" in e and "pib_power_entities" in e
            for e in errors
        ), errors

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
        path.write_text(json.dumps({
            "zendure_ip": "x", "hw_p1_ip": "y", "hw_p1_token": "z",
            "pib_soc_entities": ["sensor.a", "sensor.b"],
            "pib_power_entities": ["sensor.p", "sensor.q"],
        }))
        c = Config(options_path=str(path))
        assert c.validate() == []
