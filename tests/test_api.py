"""Reading an instance's live application state."""

from types import SimpleNamespace

import pytest
from ampapi.enums import AMPInstanceState

from custom_components.cubecoders import api
from ampapi.base import Base as AmpBase

from custom_components.cubecoders.api import (
    AmpApiClient,
    app_state_name,
    is_instance_offline,
)


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
        ({"state": 10, "uptime": "00:00:00:05"}, (True, "starting")),  # sanitized keys
        ({"State": 20}, (True, "ready")),  # raw PascalCase keys
        ({"result": {"state": 75}}, (True, "updating")),  # wrapped
        ({"uptime": "x"}, (True, None)),  # answered, but no state
        (None, (True, None)),
    ],
)
async def test_probe_parses_status(monkeypatch, raw, expected) -> None:
    """An instance that answers is up; its state is read from the reply."""

    class FakeInstance:
        def __init__(self, _instance) -> None:
            pass

        async def get_status(self, format_data=None):
            assert format_data is False
            return raw

    monkeypatch.setattr(api, "AMPInstance", FakeInstance)
    assert await _client()._async_probe_instance(_instance()) == expected  # noqa: SLF001


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ConnectionError(AmpBase._instance_offline, "http://amp"), (False, None)),  # noqa: SLF001
        (ConnectionError(AmpBase._no_data), (None, None)),  # noqa: SLF001
        (TimeoutError(), (None, None)),
        (ValueError("boom"), (None, None)),
    ],
    ids=["unavailable", "no-data", "timeout", "other"],
)
async def test_probe_errors(monkeypatch, error, expected) -> None:
    """Only AMP's "Instance Unavailable" means down; other errors are inconclusive."""

    class FakeInstance:
        def __init__(self, _instance) -> None:
            pass

        async def get_status(self, format_data=None):
            raise error

    monkeypatch.setattr(api, "AMPInstance", FakeInstance)
    assert await _client()._async_probe_instance(_instance()) == expected  # noqa: SLF001


def test_is_instance_offline() -> None:
    """Recognises ampapi's offline error by its message, nothing else."""
    assert is_instance_offline(ConnectionError(AmpBase._instance_offline, "u"))  # noqa: SLF001
    assert not is_instance_offline(ConnectionError(AmpBase._no_data))  # noqa: SLF001
    assert not is_instance_offline(ConnectionError())
    assert not is_instance_offline(ValueError(AmpBase._instance_offline))  # noqa: SLF001
