"""Config entry migration 1.1 -> 1.2: "follow physical thermostat" removed.

Loads the real ``__init__.py`` on the stubbed HA from ``ha_harness`` and runs
``async_migrate_entry`` against a fake entity registry and config entries.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

from tests.ha_harness import e2e

_ROOT = Path(__file__).parent.parent / "custom_components" / "tadox_proxy"
_PKG = "tadox_proxy_migration"


class _FakeRegistry:
    def __init__(self, entities: dict[tuple[str, str, str], str]) -> None:
        self.entities = dict(entities)
        self.removed: list[str] = []

    def async_get_entity_id(self, domain, platform, unique_id):
        return self.entities.get((domain, platform, unique_id))

    def async_remove(self, entity_id):
        self.removed.append(entity_id)


class _FakeConfigEntries:
    def __init__(self) -> None:
        self.updates = 0

    def async_update_entry(self, entry, *, options=None, minor_version=None):
        self.updates += 1
        if options is not None:
            entry.options = options
        if minor_version is not None:
            entry.minor_version = minor_version
        return True


class _Entry:
    def __init__(self, options, version=1, minor_version=1):
        self.entry_id = "abc123"
        self.title = "Living Room Thermostat"
        self.options = dict(options)
        self.version = version
        self.minor_version = minor_version


class _Hass:
    def __init__(self, registry: _FakeRegistry) -> None:
        self.ent_reg = registry
        self.config_entries = _FakeConfigEntries()


def _load_init():
    """Load the integration's __init__.py with the few extra stubs it needs."""
    def mod(name, **attrs):
        m = sys.modules.get(name) or types.ModuleType(name)
        m.__dict__.update(attrs)
        sys.modules[name] = m
        return m

    platform = types.SimpleNamespace(
        BINARY_SENSOR="binary_sensor", BUTTON="button", CLIMATE="climate",
        NUMBER="number", SENSOR="sensor",
    )
    mod("homeassistant.const", Platform=platform)
    mod("homeassistant.helpers.config_validation",
        config_entry_only_config_schema=lambda domain: None)
    mod("homeassistant.helpers.typing", ConfigType=dict)
    mod("homeassistant.helpers.entity_registry", async_get=lambda hass: hass.ent_reg)
    mod("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=object)
    helpers = sys.modules["homeassistant.helpers"]
    helpers.config_validation = sys.modules["homeassistant.helpers.config_validation"]
    helpers.entity_registry = sys.modules["homeassistant.helpers.entity_registry"]

    pkg = types.ModuleType(_PKG)
    pkg.__path__ = [str(_ROOT)]
    sys.modules[_PKG] = pkg

    async def _register_card(hass):
        return None

    mod(f"{_PKG}.card", async_register_card=_register_card)
    spec = importlib.util.spec_from_file_location(
        _PKG, _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)]
    )
    init = importlib.util.module_from_spec(spec)
    sys.modules[_PKG] = init
    spec.loader.exec_module(init)
    return init


@pytest.fixture(scope="module")
def init_mod():
    return _load_init()


OLD_OPTIONS = {
    "follow_tado_input": True,
    "follow_threshold_c": 0.5,
    "follow_grace_s": 20,
    "sensor_grace_s": 300,
    "correction_kp": 0.6,
}
SWITCH_KEY = ("switch", "tadox_proxy", "abc123_follow_tado_input")


@e2e
class TestMigration:
    def test_old_entry_loses_follow_settings_and_switch(self, init_mod):
        reg = _FakeRegistry({SWITCH_KEY: "switch.living_room_follow_physical_thermostat"})
        hass = _Hass(reg)
        entry = _Entry(OLD_OPTIONS)
        assert asyncio.run(init_mod.async_migrate_entry(hass, entry)) is True
        assert entry.options == {"sensor_grace_s": 300, "correction_kp": 0.6}
        assert entry.minor_version == 2
        assert reg.removed == ["switch.living_room_follow_physical_thermostat"]

    def test_old_entry_without_switch(self, init_mod):
        reg = _FakeRegistry({})
        hass = _Hass(reg)
        entry = _Entry({"sensor_grace_s": 300})
        assert asyncio.run(init_mod.async_migrate_entry(hass, entry)) is True
        assert entry.options == {"sensor_grace_s": 300}
        assert entry.minor_version == 2
        assert reg.removed == []

    def test_current_entry_untouched(self, init_mod):
        reg = _FakeRegistry({SWITCH_KEY: "switch.x"})
        hass = _Hass(reg)
        entry = _Entry(OLD_OPTIONS, minor_version=2)
        assert asyncio.run(init_mod.async_migrate_entry(hass, entry)) is True
        assert hass.config_entries.updates == 0
        assert reg.removed == []

    def test_newer_major_version_refused(self, init_mod):
        hass = _Hass(_FakeRegistry({}))
        entry = _Entry(OLD_OPTIONS, version=2)
        assert asyncio.run(init_mod.async_migrate_entry(hass, entry)) is False
        assert hass.config_entries.updates == 0

    def test_switch_platform_no_longer_loaded(self, init_mod):
        assert "switch" not in init_mod.PLATFORMS
