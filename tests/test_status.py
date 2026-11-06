"""The simplified Status sensor: offline / idle / running / in-between."""

from unittest.mock import patch

import pytest
from ampapi.enums import AMPInstanceState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cubecoders.api import (
    AmpBaseInstance,
    AmpCoordinatorData,
    AmpExtendedInstance,
)
from custom_components.cubecoders.const import (
    DOMAIN,
    STATUS_OPTIONS,
    instance_status,
)


@pytest.mark.parametrize(
    ("running", "app_state", "expected"),
    [
        (False, "stopped", "offline"),
        (False, "ready", "offline"),  # instance off wins over a stale app state
        (True, "stopped", "idle"),
        (True, "ready", "running"),
        (True, "pre_start", "starting"),
        (True, "starting", "starting"),
        (True, "stopping", "stopping"),
        (True, "restarting", "restarting"),
        (True, "updating", "updating"),
        (True, "failed", "failed"),
        (True, "undefined", "unknown"),
        (True, None, "unknown"),
    ],
)
def test_instance_status(running, app_state, expected) -> None:
    """The mapping from instance/app state to status."""
    assert instance_status(running=running, app_state=app_state) == expected


def test_every_amp_state_maps_to_an_option() -> None:
    """No AMP state can produce a value the enum sensor would reject."""
    for state in AMPInstanceState:
        assert instance_status(running=True, app_state=state.name) in STATUS_OPTIONS


SERVERS = {  # name: (id, running, app_state)
    "Offline One": ("off", False, "stopped"),
    "Idle One": ("idle", True, "stopped"),
    "Running One": ("run", True, "ready"),
    "Starting One": ("start", True, "pre_start"),
}


class FakeClient:
    def __init__(self, **_kwargs) -> None:
        pass

    async def async_get_instances(self):
        return [
            AmpBaseInstance(name, i, name.replace(" ", ""), iid)
            for i, (name, (iid, _r, _s)) in enumerate(SERVERS.items())
        ]

    async def async_get_data(self, **_kwargs):
        instances = {}
        for i, (name, (iid, running, state)) in enumerate(SERVERS.items()):
            instances[iid] = AmpExtendedInstance(
                instance_name=name,
                instance_index=i,
                amp_instance_name=name.replace(" ", ""),
                instance_id=iid,
                active_users=0,
                players=None,
                max_active_users=8,
                cpu_usage_percentage=0,
                memory_usage_mb=0,
                app_state=state if running else "stopped",
                address=None,
                running=running,
                amp_version="2.6",
                module="GenericModule",
            )
        return AmpCoordinatorData(
            instances=instances, panel_version="2.6", panel_update=None
        )


async def test_status_sensor_and_labels(hass: HomeAssistant) -> None:
    """Each server gets a Status sensor; every option has a display label."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"host": "http://amp:8080", "username": "u", "password": "p"},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.cubecoders.AmpApiClient", FakeClient):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    states = {
        eid: hass.states.get(eid)
        for eid in (
            "sensor.offline_one_status",
            "sensor.idle_one_status",
            "sensor.running_one_status",
            "sensor.starting_one_status",
        )
    }
    assert {eid: s.state for eid, s in states.items()} == {
        "sensor.offline_one_status": "offline",
        "sensor.idle_one_status": "idle",
        "sensor.running_one_status": "running",
        "sensor.starting_one_status": "starting",
    }
    running = states["sensor.running_one_status"]
    assert running.attributes["options"] == STATUS_OPTIONS
    assert running.attributes["icon"] == "mdi:gamepad-variant"
    assert states["sensor.offline_one_status"].attributes["icon"] == "mdi:server-off"

    labels = await async_get_translations(hass, "en", "entity", {DOMAIN})
    for option in STATUS_OPTIONS:
        key = f"component.{DOMAIN}.entity.sensor.status.state.{option}"
        assert labels.get(key), f"missing label for {option}"
    assert labels[f"component.{DOMAIN}.entity.sensor.status.state.idle"] == "Idle"
