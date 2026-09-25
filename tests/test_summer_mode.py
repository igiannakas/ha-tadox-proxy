"""Summer mode: an on/off helper that locks the thermostat at 5 °C.

While the helper is on nothing may change the thermostat: presets,
temperature and HVAC mode changes are refused (with an error, and the card is
snapped back), window / presence / follow-Tado are ignored, and a change made
on the TRV itself is pushed back to 5 °C.  Turning the helper off returns the
thermostat to COMFORT.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

from tests.ha_harness import (
    PRESENCE,
    SUMMER,
    TRV,
    WINDOW,
    _active_timers,
    _climate,
    _event,
    _Extra,
    _fire_last_timer,
    _make_entity,
    _run,
    _select_preset,
    _start,
    _State,
    e2e,
    reset_timers,
)

_ROOT = Path(__file__).parent.parent / "custom_components" / "tadox_proxy"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_ctrl = _load("climate_controllers")


@pytest.fixture(autouse=True)
def _reset_timers():
    reset_timers()
    yield
    reset_timers()


# ===========================================================================
# Pure helpers
# ===========================================================================

class TestResolveSummerState:
    r = staticmethod(_ctrl.resolve_summer_state)

    @pytest.mark.parametrize("current", [True, False])
    def test_on_locks(self, current):
        assert self.r("on", current) is True

    @pytest.mark.parametrize("current", [True, False])
    def test_off_unlocks(self, current):
        assert self.r("off", current) is False

    @pytest.mark.parametrize("state", ["unavailable", "unknown", None, "garbage"])
    @pytest.mark.parametrize("current", [True, False])
    def test_anything_else_keeps_current(self, state, current):
        assert self.r(state, current) is current


class TestSummerEnforcementNeeded:
    n = staticmethod(_ctrl.summer_enforcement_needed)

    @pytest.mark.parametrize(
        "state,setpoint,expected",
        [
            ("heat", 5.0, False),
            ("heat", 5.05, False),
            ("heat", 20.0, True),
            ("heat", None, True),
            ("off", 5.0, True),
            ("auto", 5.0, True),
            ("unavailable", 20.0, False),
            ("unknown", 20.0, False),
            (None, None, False),
        ],
    )
    def test_matrix(self, state, setpoint, expected):
        assert self.n(state, setpoint, 5.0) is expected


class TestPersistedSummerFlag:
    def test_round_trip(self):
        p = _ctrl.PersistedAutomationState(summer_active=True)
        assert _ctrl.PersistedAutomationState.from_dict(p.as_dict()).summer_active is True

    def test_missing_key_means_off(self):
        data = _ctrl.PersistedAutomationState().as_dict()
        data.pop("summer_active")
        assert _ctrl.PersistedAutomationState.from_dict(data).summer_active is False


# ===========================================================================
# End-to-end (real entity code, stubbed HA)
# ===========================================================================

def _svc_error():
    return sys.modules["homeassistant.exceptions"].ServiceValidationError


def _summer_entity(**kw):
    ent = _make_entity(summer=True, **kw)
    ent.hass.set(SUMMER, "off")
    ent.hass.set(TRV, "heat", {"temperature": 20.0})
    _run(_start(ent))
    return ent


def _turn_summer(ent, state):
    ent.hass.set(SUMMER, state)
    ent._async_summer_changed(_event(state))


def _cycle(ent):
    _run(ent._async_regulation_cycle(trigger="test"))


def _sent_temps(ent):
    return [
        (d.get("temperature"), d.get("hvac_mode"))
        for dom, svc, d in ent.hass.service_calls
        if dom == "climate" and svc == "set_temperature"
    ]


@e2e
class TestSummerOn:
    def test_locks_at_five_heat(self):
        ent = _summer_entity()
        _run(_select_preset(ent, "comfort"))
        _turn_summer(ent, "on")
        assert ent.summer_mode_active
        assert ent.preset_mode == "frost_protection"
        assert ent.target_temperature == 5.0
        assert ent.hvac_mode == _climate.HVACMode.HEAT
        assert ent.icon == "mdi:weather-sunny"
        assert ent.extra_state_attributes["summer_mode_active"] is True

    def test_first_command_bypasses_rate_limit(self):
        ent = _summer_entity()
        ent._last_command_sent_ts = time.time()  # would normally be rate limited
        _turn_summer(ent, "on")
        _cycle(ent)
        assert _sent_temps(ent) == [(5.0, _climate.HVACMode.HEAT)]
        assert ent._last_reason == "sent(summer_enforce)"

    def test_turns_on_a_trv_that_was_off(self):
        ent = _summer_entity()
        _run(ent.async_set_hvac_mode(_climate.HVACMode.OFF))
        ent.hass.service_calls.clear()
        ent.hass.set(TRV, "off", {"temperature": None})
        _turn_summer(ent, "on")
        assert ent.hvac_mode == _climate.HVACMode.HEAT
        _cycle(ent)
        assert _sent_temps(ent) == [(5.0, _climate.HVACMode.HEAT)]

    def test_cancels_boost_window_and_presence(self):
        ent = _summer_entity(presence=True)
        _run(_select_preset(ent, "boost"))
        assert ent._boost_cancel is not None
        ent.hass.set(WINDOW, "on")
        ent._async_window_changed(_event("on"))
        _fire_last_timer()
        assert ent._window_ctrl.is_active
        _turn_summer(ent, "on")
        assert ent._boost_cancel is None
        assert not ent._window_ctrl.is_active
        assert not ent._presence_ctrl.is_active
        assert _active_timers() == []

    def test_unavailable_helper_never_unlocks(self):
        ent = _summer_entity()
        _turn_summer(ent, "on")
        _turn_summer(ent, "unavailable")
        _turn_summer(ent, "unknown")
        assert ent.summer_mode_active


@e2e
class TestSummerLock:
    def _locked(self):
        ent = _summer_entity(presence=True)
        _turn_summer(ent, "on")
        _cycle(ent)
        ent.hass.set(TRV, "heat", {"temperature": 5.0})
        ent.hass.service_calls.clear()
        return ent

    def _assert_unchanged(self, ent):
        assert ent.preset_mode == "frost_protection"
        assert ent.target_temperature == 5.0
        assert ent.hvac_mode == _climate.HVACMode.HEAT
        assert ent.hass.service_calls == []

    @pytest.mark.parametrize("preset", ["comfort", "eco", "boost", "away", "frost_protection"])
    def test_preset_change_refused(self, preset):
        ent = self._locked()
        with pytest.raises(_svc_error()) as err:
            _run(ent.async_set_preset_mode(preset))
        assert err.value.translation_key == "summer_mode_locked"
        self._assert_unchanged(ent)

    def test_temperature_change_refused(self):
        ent = self._locked()
        with pytest.raises(_svc_error()):
            _run(ent.async_set_temperature(temperature=22.0))
        self._assert_unchanged(ent)

    @pytest.mark.parametrize("mode", ["OFF", "HEAT"])
    def test_hvac_change_refused(self, mode):
        ent = self._locked()
        with pytest.raises(_svc_error()):
            _run(ent.async_set_hvac_mode(getattr(_climate.HVACMode, mode)))
        self._assert_unchanged(ent)

    def test_refusal_pushes_state_to_revert_the_card(self):
        ent = self._locked()
        writes_before = getattr(ent, "state_writes", 0)
        forced = []
        original = ent.async_write_ha_state

        def spy():
            forced.append(getattr(ent, "_attr_force_update", False))
            original()

        ent.async_write_ha_state = spy
        with pytest.raises(_svc_error()):
            _run(ent.async_set_temperature(temperature=22.0))
        assert forced == [True]
        assert ent._attr_force_update is False
        assert ent.state_writes == writes_before + 1

    def test_window_and_presence_ignored(self):
        ent = self._locked()
        ent.hass.set(WINDOW, "on")
        ent._async_window_changed(_event("on"))
        ent.hass.set(PRESENCE, "off")
        ent._async_presence_changed(_event("off"))
        assert _active_timers() == []
        self._assert_unchanged(ent)

    def test_stale_timers_do_nothing(self):
        ent = self._locked()
        _run(ent._async_window_action(None))
        _run(ent._async_presence_away_action(None))
        assert not ent._window_ctrl.is_active
        assert not ent._presence_ctrl.is_active
        self._assert_unchanged(ent)

    def test_follow_tado_never_follows(self):
        ent = self._locked()
        ent._config_entry.options["follow_tado_input"] = True
        ent._last_command_sent_ts = 0.0
        old = _State("heat", {"temperature": 5.0})
        new = _State("heat", {"temperature": 22.0})
        ent._async_tado_state_changed(
            type("E", (), {"data": {"old_state": old, "new_state": new}})()
        )
        assert ent.preset_mode == "frost_protection"


@e2e
class TestSummerEnforcement:
    def _locked(self):
        ent = _summer_entity()
        _turn_summer(ent, "on")
        _cycle(ent)
        ent.hass.service_calls.clear()
        return ent

    def test_trv_at_target_sends_nothing(self):
        ent = self._locked()
        ent.hass.set(TRV, "heat", {"temperature": 5.0})
        ent._last_command_sent_ts = 0.0
        _cycle(ent)
        assert ent.hass.service_calls == []
        assert ent._last_reason == "summer_locked"

    def test_trv_dial_change_pushed_back(self):
        ent = self._locked()
        ent.hass.set(TRV, "heat", {"temperature": 22.0})
        ent._last_command_sent_ts = 0.0
        _cycle(ent)
        assert _sent_temps(ent) == [(5.0, _climate.HVACMode.HEAT)]

    def test_trv_turned_off_pushed_back_to_heat(self):
        ent = self._locked()
        ent.hass.set(TRV, "off", {"temperature": None})
        ent._last_command_sent_ts = 0.0
        _cycle(ent)
        assert _sent_temps(ent) == [(5.0, _climate.HVACMode.HEAT)]

    def test_enforcement_honours_rate_limit(self):
        ent = self._locked()
        ent.hass.set(TRV, "heat", {"temperature": 22.0})
        ent._last_command_sent_ts = time.time()
        _cycle(ent)
        assert ent.hass.service_calls == []
        assert ent._last_reason.startswith("summer_rate_limited(")

    def test_unavailable_trv_not_commanded(self):
        ent = self._locked()
        ent.hass.set(TRV, "unavailable")
        ent._last_command_sent_ts = 0.0
        _cycle(ent)
        assert ent.hass.service_calls == []
        assert ent._last_reason == "summer_trv_unavailable"

    def test_overlay_refresh_still_resends(self):
        ent = _summer_entity(options={"overlay_refresh_s": 600})
        _turn_summer(ent, "on")
        _cycle(ent)
        ent.hass.set(TRV, "heat", {"temperature": 5.0})
        ent.hass.service_calls.clear()
        ent._last_command_sent_ts = time.time() - 700
        _cycle(ent)
        assert _sent_temps(ent) == [(5.0, _climate.HVACMode.HEAT)]

    def test_trv_state_change_triggers_a_cycle(self):
        ent = self._locked()
        created = []
        ent.hass.async_create_task = lambda coro, *a, **k: (created.append(coro), coro.close())
        old = _State("heat", {"temperature": 5.0})
        new = _State("heat", {"temperature": 22.0})
        ent._async_tado_state_changed(
            type("E", (), {"data": {"old_state": old, "new_state": new}})()
        )
        assert len(created) == 1

    def test_room_sensor_missing_does_not_matter(self):
        ent = self._locked()
        ent.coordinator.last_update_success = False
        ent.hass.set(TRV, "heat", {"temperature": 22.0})
        ent._last_command_sent_ts = 0.0
        _cycle(ent)
        assert _sent_temps(ent) == [(5.0, _climate.HVACMode.HEAT)]


@e2e
class TestSummerOff:
    def test_returns_to_comfort(self):
        ent = _summer_entity()
        _run(_select_preset(ent, "eco"))
        _turn_summer(ent, "on")
        _turn_summer(ent, "off")
        assert not ent.summer_mode_active
        assert ent.preset_mode == "comfort"
        assert ent.target_temperature == 20.5
        assert ent.hvac_mode == _climate.HVACMode.HEAT
        assert ent._last_regulation_ts == 0.0

    def test_changes_allowed_again(self):
        ent = _summer_entity()
        _turn_summer(ent, "on")
        _turn_summer(ent, "off")
        _run(_select_preset(ent, "eco"))
        assert ent.preset_mode == "eco"

    def test_open_window_and_absence_are_picked_up(self):
        ent = _summer_entity(presence=True)
        _turn_summer(ent, "on")
        ent.hass.set(WINDOW, "on")
        ent.hass.set(PRESENCE, "off")
        _turn_summer(ent, "off")
        assert len(_active_timers()) == 2


@e2e
class TestSummerAcrossRestart:
    def _boot(self, *, summer_state, persisted_summer, last_preset="comfort", summer=True,
              window_state="off"):
        ent = _make_entity(summer=summer)
        if summer_state is not None:
            ent.hass.set(SUMMER, summer_state)
        ent.hass.set(WINDOW, window_state)
        ent.hass.set(TRV, "heat", {"temperature": 5.0})
        extra = _ctrl.PersistedAutomationState(summer_active=persisted_summer).as_dict()
        last = _State("heat", {"preset_mode": last_preset, "temperature": 20.5})
        _run(_start(ent, last_state=last, extra=_Extra(extra)))
        return ent

    def test_helper_on_at_boot_locks(self):
        ent = self._boot(summer_state="on", persisted_summer=False, window_state="on")
        assert ent.summer_mode_active
        assert ent.preset_mode == "frost_protection"
        assert _active_timers() == []  # open window ignored
        assert ent._summer_bypass_rate_limit is False  # no forced resend on boot

    def test_helper_turned_off_while_down_returns_to_comfort(self):
        ent = self._boot(summer_state="off", persisted_summer=True, last_preset="frost_protection")
        assert not ent.summer_mode_active
        assert ent.preset_mode == "comfort"
        assert ent.target_temperature == 20.5

    def test_helper_unavailable_at_boot_keeps_lock(self):
        ent = self._boot(summer_state="unavailable", persisted_summer=True,
                         last_preset="frost_protection")
        assert ent.summer_mode_active

    def test_helper_not_loaded_yet_keeps_lock(self):
        ent = self._boot(summer_state=None, persisted_summer=True,
                         last_preset="frost_protection")
        assert ent.summer_mode_active

    def test_helper_removed_from_config_unlocks(self):
        ent = self._boot(summer_state=None, persisted_summer=True,
                         last_preset="frost_protection", summer=False)
        assert not ent.summer_mode_active
        assert ent.preset_mode == "comfort"

    def test_snapshot_carries_the_flag(self):
        ent = _summer_entity()
        _turn_summer(ent, "on")
        assert ent.extra_restore_state_data.as_dict()["summer_active"] is True
