"""Packaging / shipping invariants.

Catches drift between the version strings the project ships in different
places. The HA Supervisor reads `battery-coordinator/config.yaml`; the
PyPI/uv tooling reads `pyproject.toml`. When they disagree, users see one
version on the Add-on dashboard and a different one in any tooling output —
the changelog can only describe one of them.
"""

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _read_pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert m, "pyproject.toml has no top-level version"
    return m.group(1)


def _read_config_yaml_version() -> str:
    text = (ROOT / "battery-coordinator" / "config.yaml").read_text()
    m = re.search(r'^version:\s*"([^"]+)"', text, re.MULTILINE)
    assert m, "config.yaml has no top-level version"
    return m.group(1)


def _read_changelog_top_version() -> str:
    text = (ROOT / "battery-coordinator" / "CHANGELOG.md").read_text()
    m = re.search(r"^##\s+(\S+)", text, re.MULTILINE)
    assert m, "CHANGELOG.md has no top-level version heading"
    return m.group(1)


def test_pyproject_matches_config_yaml():
    pyproj = _read_pyproject_version()
    cfg = _read_config_yaml_version()
    assert pyproj == cfg, (
        f"Version drift: pyproject.toml={pyproj!r} but config.yaml={cfg!r}. "
        "HA Supervisor ships config.yaml's version; pyproject.toml drives "
        "any tooling that reads project metadata. Keep them aligned."
    )


def test_changelog_top_matches_shipped_version():
    cfg = _read_config_yaml_version()
    top = _read_changelog_top_version()
    assert top == cfg, (
        f"CHANGELOG.md's top heading is {top!r} but config.yaml ships "
        f"{cfg!r}. The user-facing changelog should describe the version "
        "the add-on actually ships."
    )
