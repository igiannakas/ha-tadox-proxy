"""Serve the dashboard card and load it on every dashboard.

The card lives in ``www/roomstat-card.js`` and is served from
``/roomstat/roomstat-card.js``. The URL carries a hash of the file so
browsers pick up a new version after an update.

It is loaded as a dashboard resource (the same list HACS uses for cards), so it
is fetched after the frontend has started and lands in the element registry
the dashboards use. Only when resources are managed in YAML is it added as an
extra frontend module instead.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CARD_FILE = Path(__file__).parent / "www" / "roomstat-card.js"
CARD_URL = f"/{DOMAIN}/roomstat-card.js"
_REGISTERED = f"{DOMAIN}_card_registered"


def _card_digest() -> str:
    return hashlib.sha256(CARD_FILE.read_bytes()).hexdigest()[:12]


def _storage_resources(hass: HomeAssistant) -> Any | None:
    """The dashboard resource collection, if it is UI (storage) managed."""
    lovelace = hass.data.get("lovelace")
    if lovelace is None:
        return None
    resources = getattr(lovelace, "resources", None)
    if resources is None and isinstance(lovelace, dict):
        resources = lovelace.get("resources")
    store = getattr(resources, "store", None)
    if resources is None or store is None or getattr(store, "key", None) != "lovelace_resources":
        return None
    return resources


async def _async_set_resource(resources: Any, url: str) -> None:
    """Add the card resource, or point the existing one at the new version."""
    if not resources.loaded:
        await resources.async_load()
        resources.loaded = True
    for item in resources.async_items():
        if item["url"].split("?")[0] == CARD_URL:
            if item["url"] != url:
                await resources.async_update_item(item["id"], {"res_type": "module", "url": url})
            return
    await resources.async_create_item({"res_type": "module", "url": url})


async def async_register_card(hass: HomeAssistant) -> None:
    """Serve the card and register it once per Home Assistant start."""
    if hass.data.get(_REGISTERED) or getattr(hass, "http", None) is None:
        return
    # Imported here so the HA-free test harness never needs these modules.
    from homeassistant.components.http import StaticPathConfig

    try:
        digest = await hass.async_add_executor_job(_card_digest)
    except OSError as err:
        _LOGGER.warning("Dashboard card not found at %s: %s", CARD_FILE, err)
        return
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(CARD_FILE), True)]
    )
    url = f"{CARD_URL}?v={digest}"
    hass.data[_REGISTERED] = True

    if (resources := _storage_resources(hass)) is not None:
        try:
            await _async_set_resource(resources, url)
            return
        except Exception:  # noqa: BLE001 - never block setup on the card
            _LOGGER.exception("Could not add the dashboard card as a resource")

    from homeassistant.components.frontend import add_extra_js_url

    add_extra_js_url(hass, url)
