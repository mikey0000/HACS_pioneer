"""Support for Pioneer Network Receivers."""
from __future__ import annotations

import logging
import socket
import telnetlib
import typing

import voluptuous as vol

from homeassistant.components.media_player import (
    PLATFORM_SCHEMA,
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_TIMEOUT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_SOURCES,
    CONF_ZONES,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_SOURCES,
    DEFAULT_TIMEOUT,
    DEFAULT_ZONE,
    MAX_SOURCE_NUMBERS,
    MAX_VOLUME,
    ZONE_COMMANDS,
)

_LOGGER = logging.getLogger(__name__)


PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
        vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): cv.socket_timeout,
        vol.Optional(CONF_SOURCES, default=DEFAULT_SOURCES): {cv.string: cv.string},
        vol.Required(CONF_ZONES, default=DEFAULT_ZONE): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=2)
        ),
    }
)


def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Pioneer platform."""
    pioneer = [
        PioneerDevice(
            entry.data[CONF_NAME],
            entry.data[CONF_HOST],
            entry.data[CONF_PORT],
            entry.data[CONF_TIMEOUT],
            entry.data[CONF_SOURCES],
            zone,
        )
        for zone in range(0, entry.data[CONF_ZONES])
    ]

    if pioneer[0].update():
        async_add_entities(pioneer, True)


class PioneerDevice(MediaPlayerEntity):
    """Representation of a Pioneer device."""

    _attr_device_class = MediaPlayerDeviceClass.RECEIVER
    _attr_supported_features = (
        MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.PLAY
    )

    def __init__(
        self, name, host, port, timeout, sources: list, zone=DEFAULT_ZONE
    ) -> None:
        """Initialize the Pioneer device."""

        def filter_sources(pair):
            key, _ = pair
            return bool(key in sources)

        source_list = dict(
            filter(
                filter_sources,
                DEFAULT_SOURCES.items(),
            )
        )
        self._name = name
        self._host = host
        self._port = port
        self._timeout = timeout
        self._pwstate = "PWR1"
        self._volume = 0
        self._muted = False
        self._selected_source = ""
        self._source_name_to_number = source_list
        self._source_number_to_name = {v: k for k, v in source_list.items()}
        self._zone = zone

        self._attr_unique_id = f"pioneer_{zone}"

    @classmethod
    def telnet_request(cls, telnet, command, expected_prefix):
        """Execute `command` and return the response."""
        try:
            telnet.write(command.encode("ASCII") + b"\r")
        except telnetlib.socket.timeout:
            _LOGGER.debug("Pioneer command %s timed out", command)
            return None

        # The receiver will randomly send state change updates, make sure
        # we get the response we are looking for
        for _ in range(3):
            result = telnet.read_until(b"\r\n", timeout=0.2).decode("ASCII").strip()
            if result.startswith(expected_prefix):
                return result

        return None

    def telnet_command(self, command) -> None:
        """Establish a telnet connection and sends command."""
        try:
            try:
                telnet = telnetlib.Telnet(self._host, self._port, self._timeout)
            except OSError:
                _LOGGER.warning("Pioneer %s refused connection", self._name)
                return
            telnet.write(command.encode("ASCII") + b"\r")
            telnet.read_very_eager()  # skip response
            telnet.close()
        except socket.timeout:
            _LOGGER.debug("Pioneer %s command %s timed out", self._name, command)

    def update(self) -> bool:
        """Get the latest details from the device."""
        try:
            telnet = telnetlib.Telnet(self._host, self._port, self._timeout)
        except OSError:
            _LOGGER.warning("Pioneer %s refused connection", self._name)
            return False
        zone_commands = ZONE_COMMANDS[self._zone]
        pwstate = self.telnet_request(
            telnet,
            zone_commands.get("POWER")["COMMAND"],
            zone_commands.get("POWER")["PREFIX"],
        )
        if pwstate:
            self._pwstate = pwstate

        volume_str = self.telnet_request(
            telnet,
            zone_commands.get("VOL")["COMMAND"],
            zone_commands.get("VOL")["PREFIX"],
        )
        self._volume = float(volume_str[3:]) / MAX_VOLUME if volume_str else None

        muted_value = self.telnet_request(
            telnet,
            typing.cast(typing.Dict[str, dict], zone_commands.get("MUTE")["COMMAND"]),
            zone_commands.get("MUTE")["PREFIX"],
        )
        self._muted = (
            (muted_value == zone_commands["MUTED_VALUE"]["COMMAND"])
            if muted_value
            else None
        )

        # Build the source name dictionaries if necessary
        if not self._source_name_to_number:
            for i in range(MAX_SOURCE_NUMBERS):
                result = self.telnet_request(telnet, f"?RGB{str(i).zfill(2)}", "RGB")

                if not result:
                    continue

                source_name = result[6:]
                source_number = str(i).zfill(2)

                self._source_name_to_number[source_name] = source_number
                self._source_number_to_name[source_number] = source_name

        source_number = self.telnet_request(
            telnet,
            zone_commands.get("SOURCE_NUM")["COMMAND"],
            zone_commands.get("SOURCE_NUM")["PREFIX"],
        )

        if source_number:
            self._selected_source = self._source_number_to_name.get(source_number[2:])
        else:
            self._selected_source = None

        telnet.close()
        return True

    @property
    def name(self) -> str:
        """Return the name of the device."""
        return self._name

    @property
    def state(self) -> MediaPlayerState | None:
        """Return the state of the device."""
        if self._pwstate == "PWR2":
            return MediaPlayerState.OFF
        if self._pwstate == "PWR1":
            return MediaPlayerState.OFF
        if self._pwstate == "PWR0":
            return MediaPlayerState.ON

        return None

    @property
    def volume_level(self) -> int:
        """Volume level of the media player (0..1)."""
        return self._volume

    @property
    def is_volume_muted(self) -> bool:
        """Boolean if volume is currently muted."""
        return self._muted

    @property
    def source(self):
        """Return the current input source."""
        return self._selected_source

    @property
    def source_list(self):
        """List of available input sources."""
        return list(self._source_name_to_number)

    @property
    def media_title(self):
        """Title of current playing media."""
        return self._selected_source

    def turn_off(self) -> None:
        """Turn off media player."""
        self.telnet_command(ZONE_COMMANDS[self._zone].get("TURN_OFF")["COMMAND"])

    def volume_up(self) -> None:
        """Volume up media player."""
        self.telnet_command(ZONE_COMMANDS[self._zone].get("VOL_UP")["COMMAND"])

    def volume_down(self) -> None:
        """Volume down media player."""
        self.telnet_command(ZONE_COMMANDS[self._zone].get("VOL_DOWN")["COMMAND"])

    def set_volume_level(self, volume: float) -> None:
        """Set volume level, range 0..1."""
        # 60dB max
        vol_command = ZONE_COMMANDS[self._zone].get("VOL_LEVEL")["COMMAND"]
        self.telnet_command(f"{round(volume * MAX_VOLUME):03}{vol_command}")

    def mute_volume(self, mute: bool) -> None:
        """Mute (true) or unmute (false) media player."""
        self.telnet_command(
            ZONE_COMMANDS[self._zone].get("UNMUTE_VOL")["COMMAND"]
            if mute
            else ZONE_COMMANDS[self._zone].get("MUTE_VOL")["COMMAND"]
        )

    def turn_on(self) -> None:
        """Turn the media player on."""
        self.telnet_command(ZONE_COMMANDS[self._zone].get("TURN_ON")["COMMAND"])

    def select_source(self, source: str) -> None:
        """Select input source."""
        source_command = ZONE_COMMANDS[self._zone].get("SELECT_SOURCE")["COMMAND"]
        self.telnet_command(
            f"{self._source_name_to_number.get(source)}{source_command}"
        )
