"""EMS MW4 – Energiemanagement (Grundgerüst, noch ohne Funktion)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

DOMAIN = "ems_mw4"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Platzhalter: lädt nichts, steuert nichts."""
    return True
