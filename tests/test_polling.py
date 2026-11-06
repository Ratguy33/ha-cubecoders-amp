"""Configurable polling interval and fast polling that follows an action."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.cubecoders.api import (
    AmpBaseInstance,
    AmpCoordinatorData,
    AmpExtendedInstance,
)
from custom_components.cubecoders.const import (
    CONF_SCAN_INTERVAL,
    DOMAIN,
    MAX_FAST_POLL_DURATION,
    NO_CHANGE_TIMEOUT,
    SETTLE_SECONDS,
)

FAST = timedelta(seconds=1)


class FakeAMP:
    """One instance whose (running, app_state) the test scripts over time."""

    def __init__(self) -> None:
        self.running = True
        self.app_state = "ready"
        self.polls = 0
        self.light_polls = 0
        self.actions: list[str] = []

    def data(self, *, full: bool = True) -> AmpCoordinatorData:
        inst = AmpExtendedInstance(
            instance_name="PZ 04 mods",
            instance_index=0,
            amp_instance_name="PZ04mods01",
            instance_id="pz4-guid",
            active_users=1 if self.running else 0,
            players=None,
            max_active_users=8,
            cpu_usage_percentage=5,
            memory_usage_mb=900,
            app_state=self.app_state if self.running else "stopped",
            address=None,
            running=self.running,
            amp_version="2.6",
            module="GenericModule",
        )
        if full and self.running:  # only full polls fetch the live player list
            inst.player_list = ["Steve"]
            inst.players = "Steve"
        return AmpCoordinatorData(
            instances={"pz4-guid": inst},
            panel_version="2.6" if full else None,
            panel_update=None,
        )


@pytest.fixture
def amp():
    """Patch the integration's API client with a scriptable fake."""
    fake = FakeAMP()

    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def async_get_instances(self):
            return [AmpBaseInstance("PZ 04 mods", 0, "PZ04mods01", "pz4-guid")]

        async def async_get_data(self, *, full: bool = True):
            fake.polls += 1
            fake.light_polls += not full
            return fake.data(full=full)

        async def _record(self, action, name):
            fake.actions.append(f"{action} {name}")

        async def async_start_application(self, name):
            await self._record("start", name)

        async def async_stop_application(self, name):
            await self._record("stop", name)

        async def async_restart_application(self, name):
            await self._record("restart", name)

        async def async_start_instance(self, name):
            await self._record("start instance", name)

        async def async_upgrade_instance(self, name):
            await self._record("update", name)

        async def async_take_backup(self, name):
            await self._record("backup", name)

    with patch("custom_components.cubecoders.AmpApiClient", FakeClient):
        yield fake


async def _setup(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"host": "http://amp:8080", "username": "u", "password": "p"},
        options=options or {},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: int = 1) -> None:
    for _ in range(seconds):
        freezer.tick(FAST)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def _press(hass: HomeAssistant, entity_id: str) -> None:
    domain = entity_id.split(".")[0]
    service = "press" if domain == "button" else "turn_on"
    await hass.services.async_call(domain, service, {"entity_id": entity_id}, blocking=True)


def _state(hass: HomeAssistant) -> str:
    return hass.states.get("sensor.pz_04_mods_app_state").state


# ------------------------------------------------------------- options


async def test_default_interval_is_unchanged(hass: HomeAssistant, amp) -> None:
    """Without options, polling stays at the previous 60 seconds."""
    entry = await _setup(hass)
    assert entry.runtime_data.coordinator.update_interval == timedelta(seconds=60)


async def test_options_flow_sets_interval(hass: HomeAssistant, amp) -> None:
    """Configure -> interval is saved and applied after the automatic reload."""
    entry = await _setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 15}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {CONF_SCAN_INTERVAL: 15}
    assert entry.runtime_data.coordinator.update_interval == timedelta(seconds=15)


async def test_options_flow_rejects_too_fast(hass: HomeAssistant, amp) -> None:
    """Intervals below the minimum are refused."""
    entry = await _setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SCAN_INTERVAL: 5}
        )


# ------------------------------------------------------------- fast polling


async def test_restart_is_followed_until_ready(
    hass: HomeAssistant, amp, freezer: FrozenDateTimeFactory
) -> None:
    """ready -> restarting -> starting -> ready shows up second by second."""
    entry = await _setup(hass)
    coordinator = entry.runtime_data.coordinator

    await _press(hass, "button.pz_04_mods_restart_application")
    assert amp.actions == ["restart PZ04mods01"]
    assert coordinator.update_interval == FAST

    seen = []
    for state, seconds in (("restarting", 5), ("starting", 40), ("ready", 0)):
        amp.app_state = state
        await _tick(hass, freezer)
        seen.append(_state(hass))
        await _tick(hass, freezer, seconds)
    assert seen == ["restarting", "starting", "ready"]

    # Still fast just after "ready" (an autostart could follow) ...
    assert coordinator.update_interval == FAST
    # ... then back to normal once it has held "ready" for SETTLE_SECONDS.
    await _tick(hass, freezer, SETTLE_SECONDS + 1)
    assert coordinator.update_interval == timedelta(seconds=60)


async def test_long_update_keeps_fast_polling(
    hass: HomeAssistant, amp, freezer: FrozenDateTimeFactory
) -> None:
    """A multi-minute game update is followed for its whole duration."""
    entry = await _setup(hass)
    coordinator = entry.runtime_data.coordinator
    amp.app_state = "stopped"

    await _press(hass, "button.pz_04_mods_update_game_server")
    amp.app_state = "updating"
    await _tick(hass, freezer, 300)
    assert _state(hass) == "updating"
    assert coordinator.update_interval == FAST

    amp.app_state = "stopped"
    await _tick(hass, freezer, SETTLE_SECONDS + 2)
    assert _state(hass) == "stopped"
    assert coordinator.update_interval == timedelta(seconds=60)


async def test_instance_boot_then_autostart_is_followed(
    hass: HomeAssistant, amp, freezer: FrozenDateTimeFactory
) -> None:
    """Instance comes up, sits at 'stopped' briefly, then auto-starts the game."""
    amp.running, amp.app_state = False, "stopped"
    entry = await _setup(hass)
    coordinator = entry.runtime_data.coordinator

    await _press(hass, "switch.pz_04_mods_instance")
    await _tick(hass, freezer, 4)  # instance still booting
    amp.running = True  # up, game not started yet
    await _tick(hass, freezer, 3)
    assert coordinator.update_interval == FAST  # must not stop here
    amp.app_state = "starting"
    await _tick(hass, freezer, 20)
    amp.app_state = "ready"
    await _tick(hass, freezer)
    assert _state(hass) == "ready"
    await _tick(hass, freezer, SETTLE_SECONDS + 1)
    assert coordinator.update_interval == timedelta(seconds=60)


async def test_ignored_command_stops_after_timeout(
    hass: HomeAssistant, amp, freezer: FrozenDateTimeFactory
) -> None:
    """If AMP never shows a change, fast polling gives up after the timeout."""
    amp.app_state = "stopped"
    entry = await _setup(hass)
    coordinator = entry.runtime_data.coordinator

    await _press(hass, "switch.pz_04_mods_application")
    await _tick(hass, freezer, NO_CHANGE_TIMEOUT - 5)
    assert coordinator.update_interval == FAST
    await _tick(hass, freezer, 10)
    assert coordinator.update_interval == timedelta(seconds=60)


async def test_hard_cap(hass: HomeAssistant, amp, freezer: FrozenDateTimeFactory) -> None:
    """A state stuck in progress stops fast polling at the cap."""
    entry = await _setup(hass)
    coordinator = entry.runtime_data.coordinator
    await _press(hass, "button.pz_04_mods_restart_application")
    amp.app_state = "starting"
    await _tick(hass, freezer, MAX_FAST_POLL_DURATION + 2)
    assert coordinator.update_interval == timedelta(seconds=60)


async def test_backup_does_not_fast_poll(hass: HomeAssistant, amp) -> None:
    """Taking a backup doesn't change state, so there is nothing to follow."""
    entry = await _setup(hass)
    await _press(hass, "button.pz_04_mods_take_backup")
    assert amp.actions == ["backup PZ04mods01"]
    assert entry.runtime_data.coordinator.update_interval == timedelta(seconds=60)


async def test_burst_polls_are_light_and_keep_players(
    hass: HomeAssistant, amp, freezer: FrozenDateTimeFactory
) -> None:
    """Fast polls make one call each, keep player data, and fire no events."""
    entry = await _setup(hass)
    coordinator = entry.runtime_data.coordinator
    left = []
    hass.bus.async_listen("cubecoders_player_left", left.append)

    polls_before, light_before = amp.polls, amp.light_polls
    await _press(hass, "button.pz_04_mods_restart_application")
    amp.app_state = "restarting"
    await _tick(hass, freezer, 5)

    assert amp.polls - polls_before == amp.light_polls - light_before >= 5
    assert coordinator.data.instances["pz4-guid"].player_list == ["Steve"]
    assert coordinator.data.panel_version == "2.6"
    assert left == []


async def test_unload_during_burst_restores_interval(hass: HomeAssistant, amp) -> None:
    """Unloading mid-burst cancels the timer cleanly."""
    entry = await _setup(hass, {CONF_SCAN_INTERVAL: 30})
    coordinator = entry.runtime_data.coordinator
    await coordinator.async_fast_poll("PZ04mods01")
    assert coordinator.update_interval == FAST
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert coordinator.update_interval == timedelta(seconds=30)
