"""SN fetch retries until HA returns a usable value.

Production bug 2026-04-25 09:59: after a host reboot, the Zendure-HA
integration took ~80 seconds to populate the SN sensor. The coordinator
fetched it once at startup, got an empty string, and locked itself into
observe-only mode. ~1300W of solar surplus bled to grid for 12 minutes.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from coordinator_cli import fetch_zen_sn


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self):
        return self._payload


class _FakeSession:
    """Returns a queue of payloads, one per get() call."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = 0

    def get(self, url, headers=None):
        self.calls += 1
        payload = self._payloads.pop(0) if self._payloads else self._payloads[-1]
        return _FakeResponse(payload)


@pytest.mark.asyncio
async def test_returns_sn_immediately_when_available():
    session = _FakeSession([{"state": "HEC4NENCN512642"}])
    sn = await fetch_zen_sn(session, max_attempts=3, delay_s=0)
    assert sn == "HEC4NENCN512642"
    assert session.calls == 1


@pytest.mark.asyncio
async def test_retries_past_unavailable_state():
    session = _FakeSession([
        {"state": "unavailable"},
        {"state": "unknown"},
        {"state": ""},
        {"state": "HEC4NENCN512642"},
    ])
    sn = await fetch_zen_sn(session, max_attempts=10, delay_s=0)
    assert sn == "HEC4NENCN512642"
    assert session.calls == 4


@pytest.mark.asyncio
async def test_returns_empty_after_max_attempts():
    session = _FakeSession([{"state": "unavailable"}])
    sn = await fetch_zen_sn(session, max_attempts=3, delay_s=0)
    assert sn == ""
    assert session.calls == 3


@pytest.mark.asyncio
async def test_survives_request_exceptions():
    class _Boom:
        def get(self, *_a, **_kw):
            raise ConnectionError("network down")
    sn = await fetch_zen_sn(_Boom(), max_attempts=2, delay_s=0)
    assert sn == ""
