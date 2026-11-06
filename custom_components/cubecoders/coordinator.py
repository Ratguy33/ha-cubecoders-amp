"""DataUpdateCoordinator for the CubeCoders AMP integration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import AmpApiClientAuthenticationError, AmpApiClientError, AmpCoordinatorData
from .const import (
    FAST_POLL_INTERVAL,
    IN_PROGRESS_APP_STATES,
    MAX_FAST_POLL_DURATION,
    NO_CHANGE_TIMEOUT,
    SETTLE_SECONDS,
)

if TYPE_CHECKING:
    from .data import AmpData

type AMPConfigEntry = ConfigEntry[AmpData]

EVENT_PLAYER_JOINED = "cubecoders_player_joined"
EVENT_PLAYER_LEFT = "cubecoders_player_left"


# https://developers.home-assistant.io/docs/integration_fetching_data#coordinated-single-api-poll-for-data-for-all-entities
class AmpDataUpdateCoordinator(DataUpdateCoordinator[AmpCoordinatorData]):
    """Class to manage fetching data from the API."""

    config_entry: AMPConfigEntry
    # instance_id -> friendly name, as of when the entities were created.
    known_instances: dict[str, str] | None = None
    # Fast-poll burst state (see async_fast_poll).
    _normal_interval: timedelta | None = None
    _cancel_fast_poll_timer: Callable[[], None] | None = None
    _burst_started: datetime | None = None
    _settled_since: datetime | None = None
    # instance_id -> {"running": bool at action time, "changed": bool}
    _burst_targets: dict[str, dict[str, bool]] | None = None

    @property
    def fast_polling(self) -> bool:
        """True while following an action until its instance settles."""
        return self._cancel_fast_poll_timer is not None

    @staticmethod
    def _carry_forward_live_fields(
        previous: AmpCoordinatorData, new: AmpCoordinatorData
    ) -> None:
        """Keep fields a lightweight poll doesn't fetch from the last full one.

        Without this, the empty player list of a light poll would look like
        every player leaving (firing cubecoders_player_left events).
        """
        new.panel_version = previous.panel_version
        new.panel_update = previous.panel_update
        for iid, instance in new.instances.items():
            prev = previous.instances.get(iid)
            if prev is None or not instance.running:
                continue
            instance.player_list = list(prev.player_list)
            instance.players = prev.players
            instance.uptime = prev.uptime
            instance.last_backup_at = prev.last_backup_at
            instance.last_backup_name = prev.last_backup_name

    async def async_fast_poll(self, amp_instance_name: str | None = None) -> None:
        """Refresh now, then poll every second until the instance settles.

        Called after start/stop/restart/update so the dashboard follows the
        instance through starting/stopping/updating as it happens. The burst
        ends once every instance acted on has changed (gone through an
        in-progress state, or its instance started/stopped) and then held a
        settled state for SETTLE_SECONDS; or, if nothing changes within
        NO_CHANGE_TIMEOUT, once it is settled; and at the latest after
        MAX_FAST_POLL_DURATION. Repeated actions add to the same burst.
        """
        now = dt_util.utcnow()
        if self._cancel_fast_poll_timer is None:
            self._normal_interval = self.update_interval
            self._burst_targets = {}
        else:
            self._cancel_fast_poll_timer()
        self._burst_started = now
        self._settled_since = None
        if amp_instance_name is not None and self.data is not None:
            for iid, inst in self.data.instances.items():
                if inst.amp_instance_name == amp_instance_name:
                    self._burst_targets[iid] = {"running": inst.running, "changed": False}
        fast = timedelta(seconds=FAST_POLL_INTERVAL)
        if self._normal_interval is None or fast < self._normal_interval:
            self.update_interval = fast
        self._cancel_fast_poll_timer = async_call_later(
            self.hass, MAX_FAST_POLL_DURATION, self._async_end_fast_poll
        )
        await self.async_request_refresh()

    def _burst_finished(self, data: AmpCoordinatorData) -> bool:
        """Decide, after a burst poll, whether the action has played out."""
        now = dt_util.utcnow()
        targets = self._burst_targets or {}
        settled = True
        for iid, target in targets.items():
            inst = data.instances.get(iid)
            if inst is None:  # instance vanished; nothing left to follow
                target["changed"] = True
                continue
            in_progress = inst.app_state in IN_PROGRESS_APP_STATES
            if in_progress or inst.running != target["running"]:
                target["changed"] = True
            settled = settled and not in_progress
        if not settled:
            self._settled_since = None
            return False
        if self._settled_since is None:
            self._settled_since = now
        started = self._burst_started or now
        all_changed = all(t["changed"] for t in targets.values())
        if not all_changed:
            # AMP never showed a change (command ignored, or too quick to see).
            return now - started >= timedelta(seconds=NO_CHANGE_TIMEOUT)
        return now - self._settled_since >= timedelta(seconds=SETTLE_SECONDS)

    @callback
    def _async_end_fast_poll(self, _now: Any = None) -> None:
        self._cancel_fast_poll_timer = None
        self._burst_targets = None
        self._burst_started = None
        self._settled_since = None
        self.update_interval = self._normal_interval

    @callback
    def async_cancel_fast_poll(self) -> None:
        """Stop a running burst (on unload)."""
        if self._cancel_fast_poll_timer is not None:
            self._cancel_fast_poll_timer()
            self._async_end_fast_poll()

    async def _async_update_data(self) -> Any:
        """Update data via library."""
        try:
            client = self.config_entry.runtime_data.client
            if self.fast_polling and self.data is not None:
                data = await client.async_get_data(
                    full=False, live_ids=frozenset(self._burst_targets or ())
                )
                self._carry_forward_live_fields(previous=self.data, new=data)
            else:
                data = await client.async_get_data()
        except AmpApiClientAuthenticationError as exception:
            raise ConfigEntryAuthFailed(exception) from exception
        except AmpApiClientError as exception:
            raise UpdateFailed(exception) from exception

        self._process_player_transitions(previous=self.data, new=data)
        self._reload_if_instances_changed(data)
        if self.fast_polling and self._burst_finished(data):
            # Next poll is a full one at the normal interval.
            self.async_cancel_fast_poll()
        return data

    def _reload_if_instances_changed(self, data: AmpCoordinatorData) -> None:
        """Reload the entry when instances are added, removed or renamed.

        Entities are only created at setup, so a reload is what gives a new
        AMP instance its device and entities (and retires deleted ones).
        """
        current = {iid: inst.instance_name for iid, inst in data.instances.items()}
        if self.known_instances is None:
            self.known_instances = current
            return
        if current == self.known_instances:
            return
        added = sorted(set(current.values()) - set(self.known_instances.values()))
        removed = sorted(set(self.known_instances.values()) - set(current.values()))
        self.logger.info(
            "AMP instances changed (added/renamed: %s, removed/renamed: %s); reloading",
            added or "none",
            removed or "none",
        )
        self.known_instances = current
        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)

    def _process_player_transitions(
        self, previous: AmpCoordinatorData | None, new: AmpCoordinatorData
    ) -> None:
        """Fire join/leave events and carry forward state across refreshes.

        Both "how long has this instance been empty" and "when was its last
        backup" can only be refreshed while the instance is running, so a
        stopped instance keeps its previous value instead of losing it.
        Events are only fired when a previous refresh exists, so a Home
        Assistant restart doesn't announce every already-connected player.
        """
        previous_by_name = (
            {
                inst.amp_instance_name: inst
                for inst in previous.instances.values()
            }
            if previous is not None
            else {}
        )
        now = dt_util.utcnow()

        for instance in new.instances.values():
            prev = previous_by_name.get(instance.amp_instance_name)

            if instance.running and instance.active_users == 0 and not instance.player_list:
                was_empty = (
                    prev is not None
                    and prev.running
                    and prev.empty_since is not None
                )
                instance.empty_since = prev.empty_since if was_empty else now
            else:
                instance.empty_since = None

            if (
                instance.last_backup_at is None
                and prev is not None
                and prev.last_backup_at is not None
            ):
                instance.last_backup_at = prev.last_backup_at
                instance.last_backup_name = prev.last_backup_name

            if prev is None:
                continue
            previous_players = set(prev.player_list)
            current_players = set(instance.player_list)
            event_data = {
                "instance_name": instance.instance_name,
                "amp_instance_name": instance.amp_instance_name,
            }
            for player in sorted(current_players - previous_players):
                self.hass.bus.async_fire(
                    EVENT_PLAYER_JOINED, {**event_data, "player": player}
                )
            for player in sorted(previous_players - current_players):
                self.hass.bus.async_fire(
                    EVENT_PLAYER_LEFT, {**event_data, "player": player}
                )
