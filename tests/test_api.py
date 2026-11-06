"""Reading an instance's live application state."""

from types import SimpleNamespace

import pytest
from ampapi.enums import AMPInstanceState

from custom_components.cubecoders import api
from custom_components.cubecoders.api import AmpApiClient, app_state_name


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (AMPInstanceState.starting, "starting"),
        (20, "ready"),
        ("40", "stopping"),
        ("Updating", "updating"),
        ("pre_start", "pre_start"),
        (None, None),
        ("nonsense", None),
        (12345, None),
    ],
)
def test_app_state_name(value, expected) -> None:
    """Enum members, int values and names all map to the enum name."""
    assert app_state_name(value) == expected


def _client() -> AmpApiClient:
    return AmpApiClient(username="u", password="p", host="http://amp:8080")


def _instance() -> SimpleNamespace:
    return SimpleNamespace(friendly_name="PZ 04 mods", instance_id="pz4-guid")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"state": 10, "uptime": "00:00:00:05"}, "starting"),  # sanitized keys
        ({"State": 20}, "ready"),  # raw PascalCase keys
        ({"result": {"state": 75}}, "updating"),  # wrapped
        ({"uptime": "x"}, None),  # no state at all
        (None, None),
    ],
)
async def test_live_app_state_parsing(monkeypatch, raw, expected) -> None:
    """Core/GetStatus responses in the shapes AMP/ampapi produce are understood."""

    class FakeInstance:
        def __init__(self, _instance) -> None:
            pass

        async def get_status(self, format_data=None):
            assert format_data is False
            return raw

    monkeypatch.setattr(api, "AMPInstance", FakeInstance)
    assert await _client()._async_get_live_app_state(_instance()) == expected  # noqa: SLF001


async def test_live_app_state_unreachable_instance(monkeypatch) -> None:
    """A booting instance that can't answer falls back (None), never raises."""

    class FakeInstance:
        def __init__(self, _instance) -> None:
            pass

        async def get_status(self, format_data=None):
            raise ConnectionError("Instance Unavailable")

    monkeypatch.setattr(api, "AMPInstance", FakeInstance)
    assert await _client()._async_get_live_app_state(_instance()) is None  # noqa: SLF001
