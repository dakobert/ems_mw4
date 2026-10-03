"""Einrichtung und Einstellungen für EMS MW4."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import CONF_ROOM_SENSORS, DEFAULT_ROOM_SENSORS, DOMAIN, NAME, SOURCES


class EmsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Einmalige Einrichtung ohne Eingaben."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(title=NAME, data={})
        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EmsOptionsFlow()


class EmsOptionsFlow(OptionsFlow):
    """Quell-Entitäten anpassen."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        options = self.config_entry.options
        schema: dict[Any, Any] = {}
        for source in SOURCES:
            schema[vol.Required(source.key, default=options.get(source.key, source.default))] = (
                selector.EntitySelector()
            )
        schema[
            vol.Required(
                CONF_ROOM_SENSORS,
                default=options.get(CONF_ROOM_SENSORS, DEFAULT_ROOM_SENSORS),
            )
        ] = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="temperature", multiple=True)
        )
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))
