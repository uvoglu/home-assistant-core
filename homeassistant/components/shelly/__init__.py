"""The Shelly integration."""
from __future__ import annotations

from typing import Any, Final

import aioshelly
from aioshelly.block_device import BlockDevice
from aioshelly.exceptions import DeviceConnectionError, InvalidAuthError
from aioshelly.rpc_device import RpcDevice
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import aiohttp_client, device_registry
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_COAP_PORT,
    CONF_SLEEP_PERIOD,
    DATA_CONFIG_ENTRY,
    DEFAULT_COAP_PORT,
    DOMAIN,
    LOGGER,
)
from .coordinator import (
    ShellyBlockCoordinator,
    ShellyEntryData,
    ShellyRestCoordinator,
    ShellyRpcCoordinator,
    ShellyRpcPollingCoordinator,
    get_entry_data,
)
from .utils import (
    get_block_device_sleep_period,
    get_coap_context,
    get_device_entry_gen,
    get_rpc_device_sleep_period,
    get_ws_context,
)

BLOCK_PLATFORMS: Final = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.COVER,
    Platform.LIGHT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
]
BLOCK_SLEEPING_PLATFORMS: Final = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SENSOR,
]
RPC_PLATFORMS: Final = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.COVER,
    Platform.LIGHT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
]
RPC_SLEEPING_PLATFORMS: Final = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
]

# Multiclick delay between clicks in seconds
MC_MULTICLICK_DELAY = 0.5

COAP_SCHEMA: Final = vol.Schema(
    {
        vol.Optional(CONF_COAP_PORT, default=DEFAULT_COAP_PORT): cv.port,
    }
)
CONFIG_SCHEMA: Final = vol.Schema({DOMAIN: COAP_SCHEMA}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Shelly component."""
    hass.data[DOMAIN] = {DATA_CONFIG_ENTRY: {}}

    if (conf := config.get(DOMAIN)) is not None:
        hass.data[DOMAIN][CONF_COAP_PORT] = conf[CONF_COAP_PORT]

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Shelly from a config entry."""
    # The custom component for Shelly devices uses shelly domain as well as core
    # integration. If the user removes the custom component but doesn't remove the
    # config entry, core integration will try to configure that config entry with an
    # error. The config entry data for this custom component doesn't contain host
    # value, so if host isn't present, config entry will not be configured.
    if not entry.data.get(CONF_HOST):
        LOGGER.warning(
            "The config entry %s probably comes from a custom integration, please remove it if you want to use core Shelly integration",
            entry.title,
        )
        return False

    get_entry_data(hass)[entry.entry_id] = ShellyEntryData()

    if get_device_entry_gen(entry) == 2:
        return await _async_setup_rpc_entry(hass, entry)

    return await _async_setup_block_entry(hass, entry)


async def _async_setup_block_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Shelly block based device from a config entry."""
    options = aioshelly.common.ConnectionOptions(
        entry.data[CONF_HOST],
        entry.data.get(CONF_USERNAME),
        entry.data.get(CONF_PASSWORD),
    )

    coap_context = await get_coap_context(hass)

    device = await BlockDevice.create(
        aiohttp_client.async_get_clientsession(hass),
        coap_context,
        options,
        False,
    )

    dev_reg = device_registry.async_get(hass)
    device_entry = None
    if entry.unique_id is not None:
        device_entry = dev_reg.async_get_device(
            identifiers=set(),
            connections={
                (
                    device_registry.CONNECTION_NETWORK_MAC,
                    device_registry.format_mac(entry.unique_id),
                )
            },
        )
    # https://github.com/home-assistant/core/pull/48076
    if device_entry and entry.entry_id not in device_entry.config_entries:
        device_entry = None

    sleep_period = entry.data.get(CONF_SLEEP_PERIOD)
    shelly_entry_data = get_entry_data(hass)[entry.entry_id]

    @callback
    def _async_block_device_setup() -> None:
        """Set up a block based device that is online."""
        shelly_entry_data.block = ShellyBlockCoordinator(hass, entry, device)
        shelly_entry_data.block.async_setup()

        platforms = BLOCK_SLEEPING_PLATFORMS

        if not entry.data.get(CONF_SLEEP_PERIOD):
            shelly_entry_data.rest = ShellyRestCoordinator(hass, device, entry)
            platforms = BLOCK_PLATFORMS

        hass.config_entries.async_setup_platforms(entry, platforms)

    @callback
    def _async_device_online(_: Any) -> None:
        LOGGER.debug("Device %s is online, resuming setup", entry.title)
        shelly_entry_data.device = None

        if sleep_period is None:
            data = {**entry.data}
            data[CONF_SLEEP_PERIOD] = get_block_device_sleep_period(device.settings)
            data["model"] = device.settings["device"]["type"]
            hass.config_entries.async_update_entry(entry, data=data)

        _async_block_device_setup()

    if sleep_period == 0:
        # Not a sleeping device, finish setup
        LOGGER.debug("Setting up online block device %s", entry.title)
        try:
            await device.initialize()
        except DeviceConnectionError as err:
            raise ConfigEntryNotReady(repr(err)) from err
        except InvalidAuthError as err:
            raise ConfigEntryAuthFailed(repr(err)) from err

        _async_block_device_setup()
    elif sleep_period is None or device_entry is None:
        # Need to get sleep info or first time sleeping device setup, wait for device
        shelly_entry_data.device = device
        LOGGER.debug(
            "Setup for device %s will resume when device is online", entry.title
        )
        device.subscribe_updates(_async_device_online)
    else:
        # Restore sensors for sleeping device
        LOGGER.debug("Setting up offline block device %s", entry.title)
        _async_block_device_setup()

    return True


async def _async_setup_rpc_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Shelly RPC based device from a config entry."""
    options = aioshelly.common.ConnectionOptions(
        entry.data[CONF_HOST],
        entry.data.get(CONF_USERNAME),
        entry.data.get(CONF_PASSWORD),
    )

    ws_context = await get_ws_context(hass)

    device = await RpcDevice.create(
        aiohttp_client.async_get_clientsession(hass),
        ws_context,
        options,
        False,
    )

    dev_reg = device_registry.async_get(hass)
    device_entry = None
    if entry.unique_id is not None:
        device_entry = dev_reg.async_get_device(
            identifiers=set(),
            connections={
                (
                    device_registry.CONNECTION_NETWORK_MAC,
                    device_registry.format_mac(entry.unique_id),
                )
            },
        )
    # https://github.com/home-assistant/core/pull/48076
    if device_entry and entry.entry_id not in device_entry.config_entries:
        device_entry = None

    sleep_period = entry.data.get(CONF_SLEEP_PERIOD)
    shelly_entry_data = get_entry_data(hass)[entry.entry_id]

    @callback
    def _async_rpc_device_setup() -> None:
        """Set up a RPC based device that is online."""
        shelly_entry_data.rpc = ShellyRpcCoordinator(hass, entry, device)
        shelly_entry_data.rpc.async_setup()

        platforms = RPC_SLEEPING_PLATFORMS

        if not entry.data.get(CONF_SLEEP_PERIOD):
            shelly_entry_data.rpc_poll = ShellyRpcPollingCoordinator(
                hass, entry, device
            )
            platforms = RPC_PLATFORMS

        hass.config_entries.async_setup_platforms(entry, platforms)

    @callback
    def _async_device_online(_: Any) -> None:
        LOGGER.debug("Device %s is online, resuming setup", entry.title)
        shelly_entry_data.device = None

        if sleep_period is None:
            data = {**entry.data}
            data[CONF_SLEEP_PERIOD] = get_rpc_device_sleep_period(device.config)
            hass.config_entries.async_update_entry(entry, data=data)

        _async_rpc_device_setup()

    if sleep_period == 0:
        # Not a sleeping device, finish setup
        LOGGER.debug("Setting up online RPC device %s", entry.title)
        try:
            await device.initialize()
        except DeviceConnectionError as err:
            raise ConfigEntryNotReady(repr(err)) from err
        except InvalidAuthError as err:
            raise ConfigEntryAuthFailed(repr(err)) from err

        _async_rpc_device_setup()
    elif sleep_period is None or device_entry is None:
        # Need to get sleep info or first time sleeping device setup, wait for device
        shelly_entry_data.device = device
        LOGGER.debug(
            "Setup for device %s will resume when device is online", entry.title
        )
        device.subscribe_updates(_async_device_online)
    else:
        # Restore sensors for sleeping device
        LOGGER.debug("Setting up offline block device %s", entry.title)
        _async_rpc_device_setup()

    return True


class BlockDeviceWrapper(update_coordinator.DataUpdateCoordinator):
    """Wrapper for a Shelly block based device with Home Assistant specific functions."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, device: BlockDevice
    ) -> None:
        """Initialize the Shelly device wrapper."""
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
            raise update_coordinator.UpdateFailed(
                f"Sleeping device did not update within {sleep_period} seconds interval"
            )

        LOGGER.debug("Polling Shelly Block Device - %s", self.name)
        try:
            async with async_timeout.timeout(POLLING_TIMEOUT_SEC):
                await self.device.update()
                device_update_info(self.hass, self.device, self.entry)
        except OSError as err:
            raise update_coordinator.UpdateFailed("Error fetching data") from err

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
        """Set up the wrapper."""
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

    async def async_trigger_ota_update(self, beta: bool = False) -> None:
        """Trigger or schedule an ota update."""
        update_data = self.device.status["update"]
        LOGGER.debug("OTA update service - update_data: %s", update_data)

        if not update_data["has_update"] and not beta:
            LOGGER.warning("No OTA update available for device %s", self.name)
            return

        if beta and not update_data.get("beta_version"):
            LOGGER.warning(
                "No OTA update on beta channel available for device %s", self.name
            )
            return

        if update_data["status"] == "updating":
            LOGGER.warning("OTA update already in progress for %s", self.name)
            return

        new_version = update_data["new_version"]
        if beta:
            new_version = update_data["beta_version"]
        LOGGER.info(
            "Start OTA update of device %s from '%s' to '%s'",
            self.name,
            self.device.firmware_version,
            new_version,
        )
        try:
            async with async_timeout.timeout(AIOSHELLY_DEVICE_TIMEOUT_SEC):
                result = await self.device.trigger_ota_update(beta=beta)
        except (asyncio.TimeoutError, OSError) as err:
            LOGGER.exception("Error while perform ota update: %s", err)
        LOGGER.debug("Result of OTA update call: %s", result)

    def shutdown(self) -> None:
        """Shutdown the wrapper."""
        self.device.shutdown()

    @callback
    def _handle_ha_stop(self, _event: Event) -> None:
        """Handle Home Assistant stopping."""
        LOGGER.debug("Stopping BlockDeviceWrapper for %s", self.name)
        self.shutdown()


class ShellyDeviceRestWrapper(update_coordinator.DataUpdateCoordinator):
    """Rest Wrapper for a Shelly device with Home Assistant specific functions."""

    def __init__(
        self, hass: HomeAssistant, device: BlockDevice, entry: ConfigEntry
    ) -> None:
        """Initialize the Shelly device wrapper."""
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
        try:
            async with async_timeout.timeout(AIOSHELLY_DEVICE_TIMEOUT_SEC):
                LOGGER.debug("REST update for %s", self.name)
                await self.device.update_status()

                if self.device.status["uptime"] > 2 * REST_SENSORS_UPDATE_INTERVAL:
                    return
                old_firmware = self.device.firmware_version
                await self.device.update_shelly()
                if self.device.firmware_version == old_firmware:
                    return
                device_update_info(self.hass, self.device, self.entry)
        except OSError as err:
            raise update_coordinator.UpdateFailed("Error fetching data") from err

    @property
    def mac(self) -> str:
        """Mac address of the device."""
        return cast(str, self.device.settings["device"]["mac"])


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    shelly_entry_data = get_entry_data(hass)[entry.entry_id]

    # If device is present, block/rpc coordinator is not setup yet
    device = shelly_entry_data.device
    if isinstance(device, RpcDevice):
        await device.shutdown()
        return True
    if isinstance(device, BlockDevice):
        device.shutdown()
        return True

    platforms = RPC_SLEEPING_PLATFORMS
    if not entry.data.get(CONF_SLEEP_PERIOD):
        platforms = RPC_PLATFORMS

    if get_device_entry_gen(entry) == 2:
        if unload_ok := await hass.config_entries.async_unload_platforms(
            entry, platforms
        ):
            if shelly_entry_data.rpc:
                await shelly_entry_data.rpc.shutdown()
            get_entry_data(hass).pop(entry.entry_id)

        return unload_ok

    platforms = BLOCK_SLEEPING_PLATFORMS

    if not entry.data.get(CONF_SLEEP_PERIOD):
        shelly_entry_data.rest = None
        platforms = BLOCK_PLATFORMS

    if unload_ok := await hass.config_entries.async_unload_platforms(entry, platforms):
        if shelly_entry_data.block:
            shelly_entry_data.block.shutdown()
        get_entry_data(hass).pop(entry.entry_id)

    return unload_ok
