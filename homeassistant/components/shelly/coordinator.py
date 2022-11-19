"""Coordinators for the Shelly integration."""
from __future__ import annotations

from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import timedelta
from threading import Timer
from typing import Any, cast

import aioshelly
from aioshelly.block_device import BlockDevice
from aioshelly.exceptions import DeviceConnectionError, InvalidAuthError, RpcCallError
from aioshelly.rpc_device import RpcDevice

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_DEVICE_ID, CONF_HOST, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    ATTR_CHANNEL,
    ATTR_CLICK_TYPE,
    ATTR_DEVICE,
    ATTR_GENERATION,
    BATTERY_DEVICES_WITH_PERMANENT_CONNECTION,
    CONF_SLEEP_PERIOD,
    DATA_CONFIG_ENTRY,
    DOMAIN,
    DUAL_MODE_LIGHT_MODELS,
    ENTRY_RELOAD_COOLDOWN,
    EVENT_SHELLY_CLICK,
    INPUTS_EVENTS_DICT,
    LOGGER,
    MODELS_SUPPORTING_LIGHT_EFFECTS,
    REST_SENSORS_UPDATE_INTERVAL,
    RPC_INPUTS_EVENTS_TYPES,
    RPC_RECONNECT_INTERVAL,
    RPC_SENSORS_POLLING_INTERVAL,
    SHBTN_MODELS,
    SLEEP_PERIOD_MULTIPLIER,
    UPDATE_PERIOD_MULTIPLIER,
)
from .utils import (
    device_update_info,
    get_block_device_name,
    get_rpc_device_name,
    get_rpc_device_wakeup_period,
)

# Multiclick delay between clicks in seconds
MC_MULTICLICK_DELAY = 0.7

@dataclass
class ShellyEntryData:
    """Class for sharing data within a given config entry."""

    block: ShellyBlockCoordinator | None = None
    device: BlockDevice | RpcDevice | None = None
    rest: ShellyRestCoordinator | None = None
    rpc: ShellyRpcCoordinator | None = None
    rpc_poll: ShellyRpcPollingCoordinator | None = None


def get_entry_data(hass: HomeAssistant) -> dict[str, ShellyEntryData]:
    """Return Shelly entry data for a given config entry."""
    return cast(dict[str, ShellyEntryData], hass.data[DOMAIN][DATA_CONFIG_ENTRY])


class ShellyBlockCoordinator(DataUpdateCoordinator):
    """Coordinator for a Shelly block based device."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, device: BlockDevice
    ) -> None:
        """Initialize the Shelly block device coordinator."""
        self.device_id: str | None = None

        if sleep_period := entry.data[CONF_SLEEP_PERIOD]:
            update_interval = SLEEP_PERIOD_MULTIPLIER * sleep_period
        else:
            update_interval = (
                UPDATE_PERIOD_MULTIPLIER * device.settings["coiot"]["update_period"]
            )

        device_name = (
            get_block_device_name(device) if device.initialized else entry.title
        )
        super().__init__(
            hass,
            LOGGER,
            name=device_name,
            update_interval=timedelta(seconds=update_interval),
        )
        self.hass = hass
        self.entry = entry
        self.device = device

        self._debounced_reload: Debouncer[Coroutine[Any, Any, None]] = Debouncer(
            hass,
            LOGGER,
            cooldown=ENTRY_RELOAD_COOLDOWN,
            immediate=False,
            function=self._async_reload_entry,
        )
        entry.async_on_unload(self._debounced_reload.async_cancel)
        self._last_cfg_changed: int | None = None
        self._last_mode: str | None = None
        self._last_effect: int | None = None

        entry.async_on_unload(
            self.async_add_listener(self._async_device_updates_handler)
        )
        self._last_input_events_count: dict = {}
        self._mc_device_name = device_name
        self._mc_last_events: dict = {}
        self._mc_last_state: dict = {}
        self._mc_click_count: dict = {}
        self._mc_click_timer: dict = {}

        entry.async_on_unload(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._handle_ha_stop)
        )

    async def _async_reload_entry(self) -> None:
        """Reload entry."""
        LOGGER.debug("Reloading entry %s", self.name)
        await self.hass.config_entries.async_reload(self.entry.entry_id)

    def _mc_send_event(
        self, channel, delete, momentary_button, click_count, click_events
    ):
        if momentary_button:
            self.hass.bus.async_fire(
                "shelly.multiclick",
                {
                    ATTR_DEVICE_ID: self.device_id,
                    ATTR_DEVICE: self.device.settings["device"]["hostname"],
                    "device_name": self._mc_device_name,
                    ATTR_CHANNEL: channel,
                    "click_count" : 0,
                    "click_events" : click_events
                },
            )
        else:
            self.hass.bus.async_fire(
                "shelly.multiclick",
                {
                    ATTR_DEVICE_ID: self.device_id,
                    ATTR_DEVICE: self.device.settings["device"]["hostname"],
                    "device_name": self._mc_device_name,
                    ATTR_CHANNEL: channel,
                    "click_count" : click_count,
                    "click_events" : ""
                },
            )

        if delete:
            self._mc_click_count[channel] = 0
            self._mc_last_events[channel] = ""

    @callback
    def _async_device_updates_handler(self) -> None:
        """Handle device updates."""
        if not self.device.initialized:
            return

        assert self.device.blocks

        # For buttons which are battery powered - set initial value for last_event_count
        if self.model in SHBTN_MODELS and self._last_input_events_count.get(1) is None:
            for block in self.device.blocks:
                if block.type != "device":
                    continue

                if len(block.wakeupEvent) == 1 and block.wakeupEvent[0] == "button":
                    self._last_input_events_count[1] = -1

                break

        mc_momentary_button = True
        if "longpush_time" in self.device.settings:
            if self.device.settings["longpush_time"] <= 10:
                mc_momentary_button = False

        # Check for input events and config change
        cfg_changed = 0
        for block in self.device.blocks:
            if block.type == "device":
                cfg_changed = block.cfgChanged

            # For dual mode bulbs ignore change if it is due to mode/effect change
            if self.model in DUAL_MODE_LIGHT_MODELS:
                if "mode" in block.sensor_ids:
                    if self._last_mode != block.mode:
                        self._last_cfg_changed = None
                    self._last_mode = block.mode

            if self.model in MODELS_SUPPORTING_LIGHT_EFFECTS:
                if "effect" in block.sensor_ids:
                    if self._last_effect != block.effect:
                        self._last_cfg_changed = None
                    self._last_effect = block.effect

            if (
                "inputEvent" not in block.sensor_ids
                or "inputEventCnt" not in block.sensor_ids
            ):
                continue

            channel = int(block.channel or 0) + 1
            event_type = block.inputEvent
            last_event_count = self._last_input_events_count.get(channel)
            self._last_input_events_count[channel] = block.inputEventCnt

            mc_curr_event = block.inputEvent
            if block.inputEventCnt is not None:
                if self._mc_click_count.get(channel) is None:
                    self._mc_click_count[channel] = 0
                    self._mc_last_events[channel] = ""
                if (
                    last_event_count is not None 
                    and self._mc_last_state.get(channel) is not None
                ):
                    if block.inputEventCnt != last_event_count:
                        if self._mc_click_timer.get(channel) is not None:
                            self._mc_click_timer[channel].cancel()
                            self._mc_click_timer[channel] = None

            mc_curr_event = block.inputEvent
            if block.inputEventCnt is not None:
                if self._mc_click_count.get(channel) is None:
                    self._mc_click_count[channel] = 0
                    self._mc_last_events[channel] = ""
                if (
                    last_event_count is not None 
                    and self._mc_last_state.get(channel) is not None
                ):
                    if block.inputEventCnt != last_event_count:
                        if self._mc_click_timer.get(channel) is not None:
                            self._mc_click_timer[channel].cancel()
                            self._mc_click_timer[channel] = None

                        if mc_curr_event == "S":
                            if self._mc_last_state.get(channel) == 1:
                                self._mc_click_count[channel] += 1
                            else:
                                self._mc_click_count[channel] += 2
                            if block.input == 1:
                                self._mc_click_count[channel] += 1

                            if block.inputEventCnt - last_event_count > 1:
                                self._mc_click_count[channel] += (block.inputEventCnt - last_event_count - 1) * 2
                                mc_curr_event = "S" * (block.inputEventCnt - last_event_count)

                        if mc_curr_event == "L":
                            if mc_momentary_button:
                                if block.input != self._mc_last_state.get(channel):
                                    self._mc_click_count[channel] += 1
                                self._mc_send_event(
                                    channel, 
                                    False, 
                                    mc_momentary_button, 
                                    0, 
                                    self._mc_last_events[channel] + "LSTART",
                                )
                            else:
                                if self._mc_last_state.get(channel) == 0:
                                    self._mc_click_count[channel] += 1
                                else:
                                    self._mc_click_count[channel] += 2

                                if block.input == 0:
                                    self._mc_click_count[channel] += 1

                                if block.inputEventCnt - last_event_count > 1:
                                    self._mc_click_count[channel] += (
                                        block.inputEventCnt - last_event_count - 1
                                    ) * 2

                        self._mc_last_events[channel] += mc_curr_event

                        if not mc_momentary_button or block.input == 0:
                            self._mc_click_timer[channel] = Timer(
                                MC_MULTICLICK_DELAY, 
                                self._mc_send_event, 
                                args=(
                                    channel, 
                                    True, 
                                    mc_momentary_button, 
                                    self._mc_click_count[channel], 
                                    self._mc_last_events[channel],
                                ),
                            )

                            self._mc_click_timer[channel].start()

                    if block.inputEventCnt == last_event_count and block.input != self._mc_last_state.get(channel):
                        if self._mc_click_timer.get(channel) is not None:
                            self._mc_click_timer[channel].cancel()
                            self._mc_click_timer[channel] = None

                        self._mc_click_count[channel] += 1

                        if (
                            mc_momentary_button 
                            and block.input == 0 
                            and len(self._mc_last_events[channel]) > 0
                        ):
                            if self._mc_last_events[channel][-1] == "L":
                                self._mc_send_event(
                                    channel, 
                                    False, 
                                    mc_momentary_button, 
                                    0, 
                                    self._mc_last_events[channel] + "STOP",
                                )

                        if not mc_momentary_button or block.input == 0:
                            self._mc_click_timer[channel] = Timer(
                                MC_MULTICLICK_DELAY, 
                                self._mc_send_event, 
                                args=(
                                    channel, 
                                    True,
                                    mc_momentary_button, 
                                    self._mc_click_count[channel], 
                                    self._mc_last_events[channel],
                                ),
                            )
                            self._mc_click_timer[channel].start()
                self._mc_last_state[channel] = block.input

            if (
                last_event_count is None
                or last_event_count == block.inputEventCnt
                or event_type == ""
            ):
                continue

            if event_type in INPUTS_EVENTS_DICT:
                self.hass.bus.async_fire(
                    EVENT_SHELLY_CLICK,
                    {
                        ATTR_DEVICE_ID: self.device_id,
                        ATTR_DEVICE: self.device.settings["device"]["hostname"],
                        ATTR_CHANNEL: channel,
                        ATTR_CLICK_TYPE: INPUTS_EVENTS_DICT[event_type],
                        ATTR_GENERATION: 1,
                    },
                )
            else:
                LOGGER.warning(
                    "Shelly input event %s for device %s is not supported, please open issue",
                    event_type,
                    self.name,
                )

        if self._last_cfg_changed is not None and cfg_changed > self._last_cfg_changed:
            LOGGER.info(
                "Config for %s changed, reloading entry in %s seconds",
                self.name,
                ENTRY_RELOAD_COOLDOWN,
            )
            self.hass.async_create_task(self._debounced_reload.async_call())
        self._last_cfg_changed = cfg_changed

    async def _async_update_data(self) -> None:
        """Fetch data."""
        if sleep_period := self.entry.data.get(CONF_SLEEP_PERIOD):
            # Sleeping device, no point polling it, just mark it unavailable
            raise UpdateFailed(
                f"Sleeping device did not update within {sleep_period} seconds interval"
            )

        LOGGER.debug("Polling Shelly Block Device - %s", self.name)
        try:
            await self.device.update()
        except DeviceConnectionError as err:
            raise UpdateFailed(f"Error fetching data: {repr(err)}") from err
        except InvalidAuthError:
            self.entry.async_start_reauth(self.hass)
        else:
            device_update_info(self.hass, self.device, self.entry)

    @property
    def model(self) -> str:
        """Model of the device."""
        return cast(str, self.entry.data["model"])

    @property
    def mac(self) -> str:
        """Mac address of the device."""
        return cast(str, self.entry.unique_id)

    @property
    def sw_version(self) -> str:
        """Firmware version of the device."""
        return self.device.firmware_version if self.device.initialized else ""

    def async_setup(self) -> None:
        """Set up the coordinator."""
        dev_reg = device_registry.async_get(self.hass)
        entry = dev_reg.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            name=self.name,
            connections={(device_registry.CONNECTION_NETWORK_MAC, self.mac)},
            manufacturer="Shelly",
            model=aioshelly.const.MODEL_NAMES.get(self.model, self.model),
            sw_version=self.sw_version,
            hw_version=f"gen{self.device.gen} ({self.model})",
            configuration_url=f"http://{self.entry.data[CONF_HOST]}",
        )
        self.device_id = entry.id
        self.device.subscribe_updates(self.async_set_updated_data)

    def shutdown(self) -> None:
        """Shutdown the coordinator."""
        self.device.shutdown()

    @callback
    def _handle_ha_stop(self, _event: Event) -> None:
        """Handle Home Assistant stopping."""
        LOGGER.debug("Stopping block device coordinator for %s", self.name)
        self.shutdown()


class ShellyRestCoordinator(DataUpdateCoordinator):
    """Coordinator for a Shelly REST device."""

    def __init__(
        self, hass: HomeAssistant, device: BlockDevice, entry: ConfigEntry
    ) -> None:
        """Initialize the Shelly REST device coordinator."""
        if (
            device.settings["device"]["type"]
            in BATTERY_DEVICES_WITH_PERMANENT_CONNECTION
        ):
            update_interval = (
                SLEEP_PERIOD_MULTIPLIER * device.settings["coiot"]["update_period"]
            )
        else:
            update_interval = REST_SENSORS_UPDATE_INTERVAL

        super().__init__(
            hass,
            LOGGER,
            name=get_block_device_name(device),
            update_interval=timedelta(seconds=update_interval),
        )
        self.device = device
        self.entry = entry

    async def _async_update_data(self) -> None:
        """Fetch data."""
        LOGGER.debug("REST update for %s", self.name)
        try:
            await self.device.update_status()

            if self.device.status["uptime"] > 2 * REST_SENSORS_UPDATE_INTERVAL:
                return
            old_firmware = self.device.firmware_version
            await self.device.update_shelly()
            if self.device.firmware_version == old_firmware:
                return
        except DeviceConnectionError as err:
            raise UpdateFailed(f"Error fetching data: {repr(err)}") from err
        except InvalidAuthError:
            self.entry.async_start_reauth(self.hass)
        else:
            device_update_info(self.hass, self.device, self.entry)

    @property
    def mac(self) -> str:
        """Mac address of the device."""
        return cast(str, self.device.settings["device"]["mac"])


class ShellyRpcCoordinator(DataUpdateCoordinator):
    """Coordinator for a Shelly RPC based device."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, device: RpcDevice
    ) -> None:
        """Initialize the Shelly RPC device coordinator."""
        self.device_id: str | None = None

        if sleep_period := entry.data[CONF_SLEEP_PERIOD]:
            update_interval = SLEEP_PERIOD_MULTIPLIER * sleep_period
        else:
            update_interval = RPC_RECONNECT_INTERVAL
        device_name = get_rpc_device_name(device) if device.initialized else entry.title
        super().__init__(
            hass,
            LOGGER,
            name=device_name,
            update_interval=timedelta(seconds=update_interval),
        )
        self.entry = entry
        self.device = device

        self._debounced_reload: Debouncer[Coroutine[Any, Any, None]] = Debouncer(
            hass,
            LOGGER,
            cooldown=ENTRY_RELOAD_COOLDOWN,
            immediate=False,
            function=self._async_reload_entry,
        )
        entry.async_on_unload(self._debounced_reload.async_cancel)

        entry.async_on_unload(
            self.async_add_listener(self._async_device_updates_handler)
        )
        self._last_event: dict[str, Any] | None = None

        entry.async_on_unload(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._handle_ha_stop)
        )

    async def _async_reload_entry(self) -> None:
        """Reload entry."""
        LOGGER.debug("Reloading entry %s", self.name)
        await self.hass.config_entries.async_reload(self.entry.entry_id)

    def update_sleep_period(self) -> bool:
        """Check device sleep period & update if changed."""
        if (
            not self.device.initialized
            or not (wakeup_period := get_rpc_device_wakeup_period(self.device.status))
            or wakeup_period == self.entry.data.get(CONF_SLEEP_PERIOD)
        ):
            return False

        data = {**self.entry.data}
        data[CONF_SLEEP_PERIOD] = wakeup_period
        self.hass.config_entries.async_update_entry(self.entry, data=data)

        update_interval = SLEEP_PERIOD_MULTIPLIER * wakeup_period
        self.update_interval = timedelta(seconds=update_interval)

        return True

    @callback
    def _async_device_updates_handler(self) -> None:
        """Handle device updates."""
        if (
            not self.device.initialized
            or not self.device.event
            or self.device.event == self._last_event
        ):
            return

        self.update_sleep_period()

        self._last_event = self.device.event

        for event in self.device.event["events"]:
            event_type = event.get("event")
            if event_type is None:
                continue

            if event_type == "config_changed":
                LOGGER.info(
                    "Config for %s changed, reloading entry in %s seconds",
                    self.name,
                    ENTRY_RELOAD_COOLDOWN,
                )
                self.hass.async_create_task(self._debounced_reload.async_call())
            elif event_type in RPC_INPUTS_EVENTS_TYPES:
                self.hass.bus.async_fire(
                    EVENT_SHELLY_CLICK,
                    {
                        ATTR_DEVICE_ID: self.device_id,
                        ATTR_DEVICE: self.device.hostname,
                        ATTR_CHANNEL: event["id"] + 1,
                        ATTR_CLICK_TYPE: event["event"],
                        ATTR_GENERATION: 2,
                    },
                )

    async def _async_update_data(self) -> None:
        """Fetch data."""
        if self.update_sleep_period():
            return

        if sleep_period := self.entry.data.get(CONF_SLEEP_PERIOD):
            # Sleeping device, no point polling it, just mark it unavailable
            raise UpdateFailed(
                f"Sleeping device did not update within {sleep_period} seconds interval"
            )
        if self.device.connected:
            return

        LOGGER.debug("Reconnecting to Shelly RPC Device - %s", self.name)
        try:
            await self.device.initialize()
            device_update_info(self.hass, self.device, self.entry)
        except DeviceConnectionError as err:
            raise UpdateFailed(f"Device disconnected: {repr(err)}") from err
        except InvalidAuthError:
            self.entry.async_start_reauth(self.hass)

    @property
    def model(self) -> str:
        """Model of the device."""
        return cast(str, self.entry.data["model"])

    @property
    def mac(self) -> str:
        """Mac address of the device."""
        return cast(str, self.entry.unique_id)

    @property
    def sw_version(self) -> str:
        """Firmware version of the device."""
        return self.device.firmware_version if self.device.initialized else ""

    def async_setup(self) -> None:
        """Set up the coordinator."""
        dev_reg = device_registry.async_get(self.hass)
        entry = dev_reg.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            name=self.name,
            connections={(device_registry.CONNECTION_NETWORK_MAC, self.mac)},
            manufacturer="Shelly",
            model=aioshelly.const.MODEL_NAMES.get(self.model, self.model),
            sw_version=self.sw_version,
            hw_version=f"gen{self.device.gen} ({self.model})",
            configuration_url=f"http://{self.entry.data[CONF_HOST]}",
        )
        self.device_id = entry.id
        self.device.subscribe_updates(self.async_set_updated_data)

    async def shutdown(self) -> None:
        """Shutdown the coordinator."""
        await self.device.shutdown()

    async def _handle_ha_stop(self, _event: Event) -> None:
        """Handle Home Assistant stopping."""
        LOGGER.debug("Stopping RPC device coordinator for %s", self.name)
        await self.shutdown()


class ShellyRpcPollingCoordinator(DataUpdateCoordinator):
    """Polling coordinator for a Shelly RPC based device."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, device: RpcDevice
    ) -> None:
        """Initialize the RPC polling coordinator."""
        self.device_id: str | None = None

        device_name = get_rpc_device_name(device) if device.initialized else entry.title
        super().__init__(
            hass,
            LOGGER,
            name=device_name,
            update_interval=timedelta(seconds=RPC_SENSORS_POLLING_INTERVAL),
        )
        self.entry = entry
        self.device = device

    async def _async_update_data(self) -> None:
        """Fetch data."""
        if not self.device.connected:
            raise UpdateFailed("Device disconnected")

        LOGGER.debug("Polling Shelly RPC Device - %s", self.name)
        try:
            await self.device.update_status()
        except (DeviceConnectionError, RpcCallError) as err:
            raise UpdateFailed(f"Device disconnected: {repr(err)}") from err
        except InvalidAuthError:
            self.entry.async_start_reauth(self.hass)

    @property
    def model(self) -> str:
        """Model of the device."""
        return cast(str, self.entry.data["model"])

    @property
    def mac(self) -> str:
        """Mac address of the device."""
        return cast(str, self.entry.unique_id)


def get_block_coordinator_by_device_id(
    hass: HomeAssistant, device_id: str
) -> ShellyBlockCoordinator | None:
    """Get a Shelly block device coordinator for the given device id."""
    if not hass.data.get(DOMAIN):
        return None

    dev_reg = device_registry.async_get(hass)
    if device := dev_reg.async_get(device_id):
        for config_entry in device.config_entries:
            if not (entry_data := get_entry_data(hass).get(config_entry)):
                continue

            if coordinator := entry_data.block:
                return coordinator

    return None


def get_rpc_coordinator_by_device_id(
    hass: HomeAssistant, device_id: str
) -> ShellyRpcCoordinator | None:
    """Get a Shelly RPC device coordinator for the given device id."""
    if not hass.data.get(DOMAIN):
        return None

    dev_reg = device_registry.async_get(hass)
    if device := dev_reg.async_get(device_id):
        for config_entry in device.config_entries:
            if not (entry_data := get_entry_data(hass).get(config_entry)):
                continue

            if coordinator := entry_data.rpc:
                return coordinator

    return None
