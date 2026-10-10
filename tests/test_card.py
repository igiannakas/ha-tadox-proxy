"""Dashboard card: served once from the integration, and fed by the climate.

The card (``www/roomstat-card.js``) reads two climate attributes that exist
only for it: ``boost_duration_min`` for its Boost confirmation text and
``source_entity_id`` to find the Tado heating % sensor.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import re
import sys
import types
from pathlib import Path

from tests.ha_harness import TRV, _make_entity, _run, _start, e2e, reset_timers

_ROOT = Path(__file__).parent.parent / "custom_components" / "roomstat"


def _load_card_module():
    """Load card.py with just enough stubs; it imports frontend/http lazily."""
    calls: dict = {"js": []}
    pkg = types.ModuleType("roomstat_card_t")
    pkg.__path__ = [str(_ROOT)]
    sys.modules["roomstat_card_t"] = pkg
    const = types.ModuleType("roomstat_card_t.const")
    const.DOMAIN = "roomstat"
    sys.modules["roomstat_card_t.const"] = const
    if "homeassistant.core" not in sys.modules:
        sys.modules["homeassistant.core"] = types.ModuleType("homeassistant.core")
    sys.modules["homeassistant.core"].HomeAssistant = object

    class StaticPathConfig:
        def __init__(self, url_path, path, cache_headers=True):
            self.url_path, self.path, self.cache_headers = url_path, path, cache_headers

    for name in ("homeassistant.components", "homeassistant.components.frontend",
                 "homeassistant.components.http"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["homeassistant.components.http"].StaticPathConfig = StaticPathConfig
    sys.modules["homeassistant.components.frontend"].add_extra_js_url = (
        lambda hass, url: calls["js"].append(url)
    )
    spec = importlib.util.spec_from_file_location("roomstat_card_t.card", _ROOT / "card.py")
    card = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(card)
    return card, calls


class _Http:
    def __init__(self):
        self.paths = []

    async def async_register_static_paths(self, configs):
        self.paths.extend(configs)


class _Store:
    key = "lovelace_resources"


class _Resources:
    """Stand-in for HA's ResourceStorageCollection."""

    def __init__(self, items=None):
        self.store = _Store()
        self.loaded = False
        self.items = list(items or [])

    async def async_load(self):
        pass

    def async_items(self):
        return self.items

    async def async_create_item(self, data):
        self.items.append({"id": str(len(self.items)), **data})

    async def async_update_item(self, item_id, data):
        for item in self.items:
            if item["id"] == item_id:
                item.update(data)


class _Lovelace:
    def __init__(self, resources):
        self.resources = resources


class _Hass:
    def __init__(self, http=True, resources=None):
        self.data = {}
        if resources is not None:
            self.data["lovelace"] = _Lovelace(resources)
        self.http = _Http() if http else None

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def test_card_file_ships_with_the_integration():
    js = (_ROOT / "www" / "roomstat-card.js").read_text(encoding="utf-8")
    assert 'registry.define("roomstat-card"' in js
    assert 'whenDefined("home-assistant")' in js
    assert "getConfigForm" in js


def test_card_is_served_once_with_a_cache_busting_hash():
    card, calls = _load_card_module()
    hass = _Hass()
    asyncio.run(card.async_register_card(hass))
    asyncio.run(card.async_register_card(hass))  # second entry / reload

    assert len(hass.http.paths) == 1
    cfg = hass.http.paths[0]
    assert cfg.url_path == "/roomstat/roomstat-card.js"
    assert Path(cfg.path) == _ROOT / "www" / "roomstat-card.js"
    digest = hashlib.sha256(Path(cfg.path).read_bytes()).hexdigest()[:12]
    assert calls["js"] == [f"/roomstat/roomstat-card.js?v={digest}"]
    assert re.fullmatch(r"[0-9a-f]{12}", digest)


def _digest():
    return hashlib.sha256((_ROOT / "www" / "roomstat-card.js").read_bytes()).hexdigest()[:12]


def test_card_becomes_a_dashboard_resource_when_resources_are_ui_managed():
    card, calls = _load_card_module()
    resources = _Resources([{"id": "a", "res_type": "module", "url": "/hacsfiles/x.js"}])
    hass = _Hass(resources=resources)
    asyncio.run(card.async_register_card(hass))
    url = f"/roomstat/roomstat-card.js?v={_digest()}"
    assert resources.items[-1] == {"id": "1", "res_type": "module", "url": url}
    assert calls["js"] == []  # not also added as an early extra module


def test_existing_card_resource_is_moved_to_the_new_version():
    card, calls = _load_card_module()
    resources = _Resources(
        [{"id": "7", "res_type": "module", "url": "/roomstat/roomstat-card.js?v=old"}]
    )
    asyncio.run(card.async_register_card(_Hass(resources=resources)))
    assert len(resources.items) == 1
    assert resources.items[0]["url"] == f"/roomstat/roomstat-card.js?v={_digest()}"


def test_card_registration_is_skipped_without_http():
    card, calls = _load_card_module()
    hass = _Hass(http=False)
    asyncio.run(card.async_register_card(hass))
    assert calls["js"] == []


@e2e
def test_climate_exposes_what_the_card_needs():
    reset_timers()
    ent = _make_entity({"boost_duration": 45})
    _run(_start(ent))
    attrs = ent.extra_state_attributes
    assert attrs["boost_duration_min"] == 45
    assert attrs["source_entity_id"] == TRV
    reset_timers()
