"""Support for Rainwise IP-100 service."""
import asyncio
import logging
import async_timeout
import aiohttp
import voluptuous as vol

from bs4 import BeautifulSoup

import homeassistant.helpers.config_validation as cv
from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.const import (
    CONF_NAME,
    CONF_URL,
    ATTR_ATTRIBUTION,
    CONF_MONITORED_CONDITIONS,
)

from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_call_later

REQUIREMENTS = ["beautifulsoup4==4.8.0"]

_LOGGER = logging.getLogger(__name__)

ATTRIBUTION = "Weather from a Rainwise IP-100"

SENSOR_TYPES = {
    "precipitation": [
        "Precipitation",
        "in",
        "#rfd",
        "mdi:weather-pouring",
        lambda text: float(text[0][:-1]),
    ],
    "temperature": [
        "Temperature",
        "°F",
        "#tic",
        "mdi:thermometer",
        lambda text: float(text[0][:-1]),
    ],
    "humidity": [
        "Humidity",
        "%",
        "#ric",
        "mdi:water-percent",
        lambda text: int(text[0][:-1]),
    ],
    "windDirection": [
        "Wind direction",
        "",
        "#wic",
        "mdi:compass-outline",
        lambda text: _get_wind_direction(float(text[0][:-1].split(", ")[1])),
    ],
    "pressure": [
        "Pressure",
        "inHg",
        "#bic",
        "mdi:gauge",
        lambda text: float(text[0][:-1]),
    ],
    "windSpeed": [
        "Wind speed",
        "mph",
        "#wic",
        "mdi:weather-windy",
        lambda text: float(text[0].split(", ")[0][:-4]),
    ],
    "windGust": [
        "Wind gust",
        "mph",
        "#wdh",
        "mdi:weather-windy",
        lambda text: float(text[0].split(", ")[0][:-4]),
    ],
    "battery": [
        "Sation Battery",
        "Volts",
        "#batt",
        "mdi:gauge",
        lambda text: float(text[0][:-6]),
    ],
}

DEFAULT_NAME = "Rainwise"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_URL): cv.string,
        vol.Optional(CONF_MONITORED_CONDITIONS, default=["symbol"]): vol.All(
            cv.ensure_list, vol.Length(min=1), [vol.In(SENSOR_TYPES)]
        ),
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    }
)


def _get_wind_direction(wind_direction_degree: float) -> str:
    """Convert wind direction degree to named direction."""
    if 11.25 <= wind_direction_degree < 33.75:
        return "NNE"
    if 33.75 <= wind_direction_degree < 56.25:
        return "NE"
    if 56.25 <= wind_direction_degree < 78.75:
        return "ENE"
    if 78.75 <= wind_direction_degree < 101.25:
        return "E"
    if 101.25 <= wind_direction_degree < 123.75:
        return "ESE"
    if 123.75 <= wind_direction_degree < 146.25:
        return "SE"
    if 146.25 <= wind_direction_degree < 168.75:
        return "SSE"
    if 168.75 <= wind_direction_degree < 191.25:
        return "S"
    if 191.25 <= wind_direction_degree < 213.75:
        return "SSW"
    if 213.75 <= wind_direction_degree < 236.25:
        return "SW"
    if 236.25 <= wind_direction_degree < 258.75:
        return "WSW"
    if 258.75 <= wind_direction_degree < 281.25:
        return "W"
    if 281.25 <= wind_direction_degree < 303.75:
        return "WNW"
    if 303.75 <= wind_direction_degree < 326.25:
        return "NW"
    if 326.25 <= wind_direction_degree < 348.75:
        return "NNW"
    return "N"


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the Rainwise sensor."""
    name = config.get(CONF_NAME)
    url = config.get(CONF_URL)

    dev = []
    for sensor_type in config[CONF_MONITORED_CONDITIONS]:
        dev.append(RainwiseSensor(name, sensor_type))
    async_add_entities(dev)

    weather = RainwiseData(hass, url, dev)
    await weather.fetching_data()


class RainwiseSensor(Entity):
    """Representation of an Rainwise sensor."""

    def __init__(self, name, sensor_type):
        """Initialize the sensor."""
        self.client_name = name
        self._name = SENSOR_TYPES[sensor_type][0]
        self.type = sensor_type
        self._state = None
        self._unit_of_measurement = SENSOR_TYPES[self.type][1]
        self._selector = SENSOR_TYPES[self.type][2]
        self._icon = SENSOR_TYPES[self.type][3]
        self._parser = SENSOR_TYPES[self.type][4]

    def update_changes(self, new_state):
        """Update the state if it has changed, return true if it changed"""
        if new_state != self._state:
            _LOGGER.info("Changing state of %s to: %s", self._name, new_state)
            self._state = new_state
            return True
        else:
            return False

    @property
    def name(self):
        """Return the name of the sensor."""
        return f"{self.client_name} {self._name}"

    @property
    def parser(self):
        """Return the parser method."""
        return self._parser

    @property
    def state(self):
        """Return the state of the device."""
        return self._state

    @property
    def should_poll(self):
        """No polling needed."""
        return False

    @property
    def selector(self):
        """Return the CSS selector"""
        return self._selector

    @property
    def icon(self):
        """Return the icon to use in the frontend, if any."""
        return self._icon

    @property
    def entity_picture(self):
        return None

    @property
    def device_state_attributes(self):
        """Return the state attributes."""
        return {ATTR_ATTRIBUTION: ATTRIBUTION}

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement of this entity, if any."""
        return self._unit_of_measurement


class RainwiseData:
    """Get the latest data and update the sensors."""

    def __init__(self, hass, url, devices):
        """Initialize the data object."""
        self._url = url
        self.devices = devices
        self.data = {}
        self.hass = hass

    async def fetching_data(self, *_):
        """Ensure this is called every 60 seconds"""

        def try_again(err: str):
            async_call_later(self.hass, 60, self.fetching_data)

        websession = async_get_clientsession(self.hass)

        data = b""
        try:
            with async_timeout.timeout(10):
                request = await websession.get(self._url)

                if request.status != 200:
                    _LOGGER.error(
                        "Error %d on load URL %s", request.status, request.url
                    )
                    try_again("HTTP Error")

            data = await request.text()

        except (asyncio.TimeoutError, aiohttp.ClientError):
            _LOGGER.error("Timeout calling IP-100")
            try_again("Timeout Error")

        raw_data = BeautifulSoup(data, "html.parser")

        tasks = []
        for dev in self.devices:
            new_state = None
            new_state = dev.parser(raw_data.select(dev.selector)[0].contents)

            if dev.update_changes(new_state):
                tasks.append(dev.async_update_ha_state())

        if tasks:
            await asyncio.wait(tasks)

        async_call_later(self.hass, 60, self.fetching_data)
