"""Serve the dashboard card and load it on every dashboard.

The card lives in ``www/tadox-room-card.js``. It is served from
``/tadox_proxy/tadox-room-card.js`` and added to the frontend as an extra
module, so users never add a Lovelace resource by hand. The URL carries a hash
of the file so browsers pick up a new version after an update.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CARD_FILE = Path(__file__).parent / "www" / "tadox-room-card.js"
CARD_URL = f"/{DOMAIN}/tadox-room-card.js"
_REGISTERED = f"{DOMAIN}_card_registered"


def _card_digest() -> str:
    return hashlib.sha256(CARD_FILE.read_bytes()).hexdigest()[:12]


async def async_register_card(hass: HomeAssistant) -> None:
    """Register the card once per Home Assistant start."""
    if hass.data.get(_REGISTERED) or getattr(hass, "http", None) is None:
        return
    # Imported here so the HA-free test harness never needs these modules.
    from homeassistant.components.frontend import add_extra_js_url
    from homeassistant.components.http import StaticPathConfig

    try:
        digest = await hass.async_add_executor_job(_card_digest)
    except OSError as err:
        _LOGGER.warning("Dashboard card not found at %s: %s", CARD_FILE, err)
        return
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(CARD_FILE), True)]
    )
    add_extra_js_url(hass, f"{CARD_URL}?v={digest}")
    hass.data[_REGISTERED] = True
