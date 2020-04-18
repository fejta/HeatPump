"""
Support for Mitsubishi heatpumps using https://github.com/SwiCago/HeatPump over MQTT.

For more details about this platform, please refer to the documentation at
https://github.com/lekobob/mitsu_mqtt
"""

from typing import (
        List, Optional,
)
import itertools
import json
import logging

import voluptuous as vol

from homeassistant.components.mqtt import (
    CONF_COMMAND_TOPIC,
    CONF_QOS,
    CONF_RETAIN,
    CONF_STATE_TOPIC,
    MqttAttributes,
    MqttAvailability,
    subscription,
)

from homeassistant.components.mqtt.climate import (
    CONF_TEMP_STATE_TOPIC, CONF_MODE_LIST)
from homeassistant.components.climate import (
    ClimateDevice)
from homeassistant.components.climate.const import (
    SUPPORT_FAN_MODE,
    SUPPORT_SWING_MODE,
    SUPPORT_TARGET_TEMPERATURE,
)
from homeassistant.components.climate.const import (
    HVAC_MODE_COOL,
    HVAC_MODE_DRY,
    HVAC_MODE_FAN_ONLY,
    HVAC_MODE_HEAT,
    HVAC_MODE_HEAT_COOL,
    HVAC_MODE_OFF,
)

from homeassistant.components.climate.const import (
    CURRENT_HVAC_COOL,
    CURRENT_HVAC_DRY,
    CURRENT_HVAC_FAN,
    CURRENT_HVAC_HEAT,
    CURRENT_HVAC_IDLE,
    CURRENT_HVAC_OFF,
)
from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_NAME,
    CONF_VALUE_TEMPLATE,
    TEMP_CELSIUS,
)

from homeassistant.components import mqtt
import homeassistant.helpers.config_validation as cv
from homeassistant.util.temperature import convert as convert_temp

_LOGGER = logging.getLogger(__name__)

DEPENDENCIES = ['mqtt']

DEFAULT_NAME = 'MQTT Climate'

SUPPORT_FLAGS = SUPPORT_TARGET_TEMPERATURE | SUPPORT_FAN_MODE | SUPPORT_SWING_MODE

AVAILABLE_MODES = ["AUTO", "COOL", "DRY", "HEAT", "FAN", "OFF"]

CONF_WIDE_VANE = 'wide_vane'

PLATFORM_SCHEMA = mqtt.MQTT_RW_PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Optional(CONF_TEMP_STATE_TOPIC): mqtt.valid_subscribe_topic,
    vol.Optional(CONF_MODE_LIST, default=AVAILABLE_MODES): cv.ensure_list,
    vol.Optional(CONF_WIDE_VANE, default=False): cv.boolean,
})

TARGET_TEMPERATURE_STEP = 1

ha_to_me = {
    HVAC_MODE_COOL: 'COOL',
    HVAC_MODE_DRY: 'DRY',
    HVAC_MODE_FAN_ONLY: 'FAN',
    HVAC_MODE_HEAT_COOL: 'AUTO',
    HVAC_MODE_HEAT: 'HEAT',
    HVAC_MODE_OFF: 'OFF',
}
me_to_ha = {v: k for k, v in ha_to_me.items()}

# pylint: disable=unused-argument
async def async_setup_platform(hass, config, async_add_devices, discovery_info=None):
    """Setup the MQTT climate device."""
    value_template = config.get(CONF_VALUE_TEMPLATE)
    if value_template is not None:
        value_template.hass = hass
    async_add_devices([MqttClimate(
        hass,
        config.get(CONF_NAME),
        config.get(CONF_STATE_TOPIC),
        config.get(CONF_TEMP_STATE_TOPIC),
        config.get(CONF_COMMAND_TOPIC),
        config.get(CONF_MODE_LIST),
        config.get(CONF_QOS),
        config.get(CONF_RETAIN),
        config.get(CONF_WIDE_VANE),
    )])


def _gen_swing():
    _swing = {
            'Auto': ('AUTO', '|'),
            'Horizontal': ('AUTO', 'SWING'),
            'Vertical': ('SWING', '|'),
            'Both': ('SWING', 'SWING'),
    }

    _vert = {
            'Top':  '1',
            'High':  '2',
            'Middle':  '3',
            'Low':  '4', 
            'Bottom': '5',
    }

    _horz = { # Consider '|' == AUTO
            'Wide left':  '<<',
            'Left':  '<',
            'Right':  '>',
            'Wide right': '>>',
    }

    for k in _vert:
        v = _vert[k]
        _swing['Swing ' + k.lower()] = (v, 'SWING')
        _swing[k] = (v, '|')


    for k in _horz:
        h = _horz[k]
        _swing['Swing ' + k.lower()] = ('SWING', h)
        _swing[k] = ('AUTO', h)

    for (v, h) in itertools.product(_vert, _horz):
        _swing[v + ' ' + h.lower()] = (_vert[v], _horz[h])


    _swing_mqtt = {v: k for (k, v) in _swing.items()}
    return _swing, _swing_mqtt


_swing, _swing_mqtt = _gen_swing()
_swing_vert = {k: v for (k, v) in _swing.items() if v[1] == '|'}
_swing_vert_mqtt = {v: k for (k, v) in _swing_vert.items()}


class MqttClimate(ClimateDevice):
    """Representation of a Mitsubishi Minisplit Heatpump controlled over MQTT."""

    def __init__(self, hass, name, state_topic, temperature_state_topic, command_topic, modes, qos, retain, wide_vane):
        """Initialize the MQTT Heatpump."""
        self._state = False
        self._hass = hass
        self.hass = hass
        self._name = name
        self._state_topic = state_topic
        self._temperature_state_topic = temperature_state_topic
        self._command_topic = command_topic
        self._qos = qos
        self._retain = retain
        self._current_temperature = None
        self._target_temperature = None
        self._fan_modes = ["AUTO", "QUIET", "1", "2", "3", "4"]
        self._fan_mode = None
        self._hvac_modes = modes
        self._hvac_mode = None
        self._powered = False
        self._operating = False
        self._current_vane = None
        self._current_wide_vane = None
        self._sub_state = None
        self._wide_vane = wide_vane
        if wide_vane:
            self._swing = _swing
            self._swing_mqtt = _swing_mqtt
        else:
            self._swing = _swing_vert
            self._swing_mqtt = _swing_vert_mqtt

    async def async_added_to_hass(self):
        """Handle being added to home assistant."""
        await super().async_added_to_hass()
        await self._subscribe_topics()

    async def _subscribe_topics(self):
        """(Re)Subscribe to topics."""
        topics = {}

        def add_subscription(topics, topic, msg_callback):
            if topic is not None:
                topics[topic] = {
                    'topic': topic,
                    'msg_callback': msg_callback,
                    'qos': self._qos}

        def message_received(msg):
            """A new MQTT message has been received."""
            topic = msg.topic
            payload = msg.payload
            parsed = json.loads(payload)
            if topic == self._state_topic:
                self._target_temperature = float(parsed['temperature'])
                self._fan_mode = parsed['fan']
                self._current_vane = parsed['vane']
                self._current_wide_vane = parsed.get('wideVane')
                if parsed['power'] == "OFF":
                    _LOGGER.debug("Power Off")
                    self._powered = False
                else:
                    _LOGGER.debug("Power On")
                    self._hvac_mode = parsed['mode']
                    self._powered = True
            elif topic == self._temperature_state_topic:
                _LOGGER.debug('Room Temp: {0}'.format(parsed['roomTemperature']))
                self._current_temperature = float(parsed['roomTemperature'])
                self._operating = bool(parsed['operating'])
            else:
                _LOGGER.warn("unknown topic=%s", topic)
            self.async_write_ha_state()
            _LOGGER.debug("Power=%s, Operation=%s", self._powered, self._hvac_mode)

        for topic in [self._state_topic, self._temperature_state_topic]:
            add_subscription(topics, topic, message_received)

        self._sub_state = await subscription.async_subscribe_topics(
                self.hass, self._sub_state, topics)

    async def async_will_remove_from_hass(self):
        """Unsubscribe when removed."""
        self._sub_state = await subscription.async_unsubscribe_topics(
            self.hass, self._sub_state)
        await MqttAttributes.async_will_remove_from_hass(self)
        await MqttAvailability.async_will_remove_from_hass(self)

    @property
    def supported_features(self) -> int:
        """Return the list of supported features."""
        return SUPPORT_FLAGS

    @property
    def target_temperature_step(self) -> Optional[float]:
        """Return the target temperature step."""
        return TARGET_TEMPERATURE_STEP

    @property
    def should_poll(self):
        """Polling not needed for a demo climate device."""
        return False

    @property
    def name(self):
        """Return the name of the climate device."""
        return self._name

    @property
    def temperature_unit(self) -> str:
        """Return the unit of measurement."""
        return TEMP_CELSIUS

    @property
    def target_temperature(self) -> Optional[float]:
        """Return the temperature we try to reach."""
        return self._target_temperature

    @property
    def current_temperature(self) -> Optional[float]:
        """Return the current temperature."""
        return self._current_temperature

    @property
    def fan_mode(self) -> Optional[str]:
        """Return the fan setting."""
        if not self._fan_mode:
            return
        return self._fan_mode.capitalize()

    @property
    def fan_modes(self) -> Optional[List[str]]:
        """List of available fan modes."""
        return [k.capitalize() for k in self._fan_modes]

    @property
    def hvac_action(self) -> Optional[str]:
        if not self._powered:
            return CURRENT_HVAC_OFF

        if not self._operating:
            return CURRENT_HVAC_IDLE
        if self._hvac_mode == 'AUTO':
            if self._current_temperature < self._target_temperature:
                return CURRENT_HVAC_HEAT
            return CURRENT_HVAC_COOL
        if self._hvac_mode == 'HEAT':
            return CURRENT_HVAC_HEAT
        if self._hvac_mode == 'COOL':
            return CURRENT_HVAC_COOL
        if self._hvac_mode == 'DRY':
            return CURRENT_HVAC_DRY
        if self._hvac_mode == 'FAN':
            return CURRENT_HVAC_FAN

    @property
    def hvac_mode(self) -> str:
        """Return current operation ie. heat, cool, idle."""
        if not self._hvac_mode or not self._powered:
            return HVAC_MODE_OFF
        return me_to_ha[self._hvac_mode]

    @property
    def hvac_modes(self) -> List[str]:
        """List of available operation modes."""
        return [me_to_ha[k] for k in self._hvac_modes]

    @property
    def swing_mode(self) -> Optional[str]:
        """Return the swing setting."""
        cv = self._current_vane or 'AUTO'
        cwv = self._current_wide_vane or '|'
        return self._swing_mqtt[cv, cwv]

    @property
    def swing_modes(self) -> Optional[List[str]]:
        """List of available swing modes."""
        return list(self._swing)

    async def async_set_temperature(self, temperature=None, hvac_mode=None, **kwargs) -> None:
        """Set new target temperatures."""
        if not temperature:
            return
        self._target_temperature = temperature
        payload = dict(temperature=str(round(temperature * 2)/2.0))
        if not hvac_mode:
            self._publish(payload)
            return
        await self.async_set_hvac_mode(hvac_mode, payload=payload)

    async def async_set_fan_mode(self, fan_mode) -> None:
        """Set new fan mode."""
        if not fan_mode:
            return
        self._fan_mode = fan_mode.upper()
        self._publish(dict(fan=self._fan_mode))

    async def async_set_hvac_mode(self, hvac_mode, payload=None) -> None:
        """Set new operating mode and potentially temperature."""
        if not hvac_mode:
            return
        payload = payload or {}
        if hvac_mode == HVAC_MODE_OFF:
            payload['power'] = "OFF"
            self._powered = False
        else:
            payload['power'] = "ON"
            self._powered = True
            self._hvac_mode = ha_to_me[hvac_mode]
            payload['mode'] = self._hvac_mode
        self._publish(payload)

    async def async_turn_on(self) -> None:
        self._powered = True
        self._publish(dict(power="ON"))

    async def async_turn_off(self) -> None:
        self._powered = False
        self._publish(dict(power="OFF"))

    async def async_set_swing_mode(self, swing_mode) -> None:
        """Set new swing mode."""
        if not swing_mode:
            _LOGGER.warn('not changing empty swing')
            return
        if swing_mode not in self._swing:
            _LOGGER.warn('bad swing_mode: %s', swing_mode)
            return
        self._current_vane, self._current_wide_vane = self._swing[swing_mode]
        payload = dict(vane=self._current_vane)
        if self._wide_vane:
            payload['wideVane'] = self._current_wide_vane
        _LOGGER.debug('parsed %s to %s %s', swing_mode, self._current_vane, self._current_wide_vane)
        self._publish(payload)

    def _publish(self, payload) -> None:
        _LOGGER.debug('publish payload=%s', payload)
        mqtt.async_publish(
                self.hass,
                self._command_topic,
                json.dumps(payload, separators=(',',':')),
                self._qos,
                self._retain,
        )
        self.async_write_ha_state()
