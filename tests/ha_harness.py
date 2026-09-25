"""Shared end-to-end harness: the real TadoXProxyClimate on a stubbed HA.

Installs a minimal Home Assistant stub (only when Home Assistant itself is not
importable) and loads the integration package under ``tadox_proxy_e2e`` so the
real climate / preset / summer code can be exercised without an HA bootstrap.

Not a test module (no ``test_`` prefix); imported by the e2e test files.
"""
from __future__ import annotations

import asyncio
import enum
import importlib.util
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent / "custom_components" / "tadox_proxy"

try:  # pragma: no cover - depends on the environment
    import homeassistant  # noqa: F401
    _HA_INSTALLED = True
except ImportError:
    _HA_INSTALLED = False

_CALL_LATER: list[dict] = []


def _stub_call_later(hass, delay, callback):
    entry = {"delay": delay, "callback": callback, "cancelled": False}
    _CALL_LATER.append(entry)

    def cancel():
        entry["cancelled"] = True

    return cancel


def _install_ha_stubs() -> None:
    def mod(name: str, **attrs) -> types.ModuleType:
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        sys.modules[name] = m
        return m

    class HVACMode(str, enum.Enum):  # noqa: UP042 - StrEnum needs py3.11+
        OFF = "off"
        HEAT = "heat"

    class HVACAction(str, enum.Enum):  # noqa: UP042
        OFF = "off"
        HEATING = "heating"
        IDLE = "idle"

    class ClimateEntityFeature(enum.IntFlag):
        TARGET_TEMPERATURE = 1
        PRESET_MODE = 16
        TURN_OFF = 128
        TURN_ON = 256

    class _Entity:
        hass = None

        async def async_added_to_hass(self):
            return None

        async def async_will_remove_from_hass(self):
            return None

        def async_on_remove(self, func):
            return None

        def async_write_ha_state(self):
            self.state_writes = getattr(self, "state_writes", 0) + 1

    class ClimateEntity(_Entity):
        pass

    class RestoreEntity(_Entity):
        _stub_last_state = None
        _stub_last_extra = None

        async def async_get_last_state(self):
            return self._stub_last_state

        async def async_get_last_extra_data(self):
            return self._stub_last_extra

    class ExtraStoredData:
        def as_dict(self):  # pragma: no cover - abstract in HA
            raise NotImplementedError

    class CoordinatorEntity(_Entity):
        def __init__(self, coordinator):
            self.coordinator = coordinator

    class HomeAssistantError(Exception):
        def __init__(self, *args, translation_domain=None, translation_key=None,
                     translation_placeholders=None):
            super().__init__(*args)
            self.translation_domain = translation_domain
            self.translation_key = translation_key

    class ServiceValidationError(HomeAssistantError):
        pass

    mod("homeassistant")
    mod("homeassistant.components")
    mod(
        "homeassistant.components.climate",
        PRESET_AWAY="away", PRESET_BOOST="boost", PRESET_COMFORT="comfort",
        PRESET_ECO="eco", PRESET_NONE="none", HVACMode=HVACMode,
        HVACAction=HVACAction, ClimateEntity=ClimateEntity,
        ClimateEntityFeature=ClimateEntityFeature,
    )
    mod("homeassistant.config_entries", ConfigEntry=object)
    mod("homeassistant.const", ATTR_TEMPERATURE="temperature", PRECISION_TENTHS=0.1,
        UnitOfTemperature=types.SimpleNamespace(CELSIUS="°C"))
    mod("homeassistant.core", CALLBACK_TYPE=object, HomeAssistant=object,
        callback=lambda f: f)
    mod("homeassistant.exceptions", HomeAssistantError=HomeAssistantError,
        ServiceValidationError=ServiceValidationError)
    mod("homeassistant.helpers")
    mod("homeassistant.helpers.device_registry", DeviceInfo=dict)
    mod("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
    mod(
        "homeassistant.helpers.event",
        async_call_later=_stub_call_later,
        async_track_state_change_event=lambda *a, **k: (lambda: None),
        async_track_time_interval=lambda *a, **k: (lambda: None),
    )
    mod("homeassistant.helpers.restore_state", ExtraStoredData=ExtraStoredData,
        RestoreEntity=RestoreEntity)
    mod("homeassistant.helpers.update_coordinator", CoordinatorEntity=CoordinatorEntity)


def _load_entity_package():
    pkg_name = "tadox_proxy_e2e"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(_ROOT)]
    sys.modules[pkg_name] = pkg
    spec = importlib.util.spec_from_file_location(f"{pkg_name}.climate", _ROOT / "climate.py")
    climate = importlib.util.module_from_spec(spec)
    sys.modules[f"{pkg_name}.climate"] = climate
    spec.loader.exec_module(climate)
    return climate


if not _HA_INSTALLED:
    _install_ha_stubs()
    _climate = _load_entity_package()
else:  # pragma: no cover
    _climate = None

e2e = pytest.mark.skipif(
    _HA_INSTALLED, reason="end-to-end stubs are only used when HA is not installed"
)

WINDOW = "binary_sensor.window"
PRESENCE = "binary_sensor.presence"
SUMMER = "input_boolean.summer_mode"
SCHEDULE = "input_select.room_schedule"
TRV = "climate.trv"


class _State:
    def __init__(self, state, attributes=None):
        self.state = state
        self.attributes = attributes or {}


class _Hass:
    def __init__(self):
        self.states_by_id: dict[str, _State] = {}
        self.states = types.SimpleNamespace(get=self.states_by_id.get)
        self.service_calls: list[tuple[str, str, dict]] = []
        self.services = types.SimpleNamespace(async_call=self._async_call)
        # When keep_tasks is True, created tasks are queued for drain_tasks()
        # instead of being discarded.
        self.keep_tasks = False
        self.tasks: list = []

    def set(self, entity_id, state, attributes=None):
        self.states_by_id[entity_id] = _State(state, attributes)

    async def _async_call(self, domain, service, service_data=None, blocking=False, **_):
        self.service_calls.append((domain, service, dict(service_data or {})))

    def async_create_task(self, coro, *args, **kwargs):
        if self.keep_tasks:
            self.tasks.append(coro)
        else:
            coro.close()


class _Entry:
    def __init__(self, options):
        self.entry_id = "entry"
        self.title = "Test"
        self.data = {"source_entity_id": TRV}
        self.options = options

    def add_update_listener(self, _listener):
        return lambda: None


class _Coordinator:
    last_update_success = True

    def __init__(self):
        # No sensor data → regulation cycle stops at "waiting_for_sensors",
        # so no service calls are attempted.
        self.data = {"room_temp": None, "tado_internal_temp": None,
                     "tado_setpoint": None, "room_temp_ts": None}


_BASE_OPTIONS = {
    "comfort_target": 20.5,
    "eco_target": 18.5,
    "frost_protection_target": 5.0,
    "window_delay_s": 30,
    "window_close_delay_s": 0,
}


def _make_entity(options=None, *, window=True, presence=False, summer=False,
                 schedule=False):
    opts = dict(_BASE_OPTIONS)
    if window:
        opts["window_sensor_id"] = WINDOW
    if presence:
        opts["presence_sensor_id"] = PRESENCE
    if summer:
        opts["summer_mode_entity_id"] = SUMMER
    if schedule:
        opts["schedule_entity_id"] = SCHEDULE
    opts.update(options or {})
    ent = _climate.TadoXProxyClimate(
        coordinator=_Coordinator(), unique_id="u", config_entry=_Entry(opts)
    )
    ent.hass = _Hass()
    return ent


def _run(coro):
    return asyncio.run(coro)


def _event(state):
    return types.SimpleNamespace(data={"new_state": _State(state)})


def _fire_last_timer():
    """Run the most recent non-cancelled call_later callback."""
    pending = [c for c in _CALL_LATER if not c["cancelled"]]
    assert pending, "no timer scheduled"
    entry = pending[-1]
    entry["cancelled"] = True
    result = entry["callback"](None)
    if asyncio.iscoroutine(result):
        _run(result)


def _active_timers():
    return [c for c in _CALL_LATER if not c["cancelled"]]


def reset_timers() -> None:
    """Forget all recorded call_later timers (call before/after each test)."""
    _CALL_LATER.clear()


async def _start(ent, *, last_state=None, extra=None):
    ent._stub_last_state = last_state
    ent._stub_last_extra = extra
    await ent.async_added_to_hass()


class _Extra:
    def __init__(self, data):
        self._data = data

    def as_dict(self):
        return self._data


def _restart_from(old, new):
    """Simulate HA persisting *old* and restoring it into *new*."""
    attrs = {
        "preset_mode": old.preset_mode,
        "temperature": old.target_temperature,
        "window_open_active": old._window_ctrl.is_active,
    }
    last_state = _State(old.hvac_mode.value, attrs)
    extra = _Extra(old.extra_restore_state_data.as_dict())
    _run(_start(new, last_state=last_state, extra=extra))


async def _select_preset(ent, preset):
    await ent.async_set_preset_mode(preset)



def drain_tasks(ent) -> None:
    """Run the tasks an entity queued via hass.async_create_task (keep_tasks)."""
    while ent.hass.tasks:
        _run(ent.hass.tasks.pop(0))
