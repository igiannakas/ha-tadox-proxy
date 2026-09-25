"""Schedule following: the proxy follows a helper set by a scheduler.

Rules under test:
- The helper's state (comfort / night / away / frost_protection) is the
  room's base preset.  "night" is the user-facing name of the eco preset.
- Priority: summer > window > presence away > manual override > schedule.
- A manual change starts an override that ends at the next schedule change
  or after the configured duration – whichever comes first (0 = until the
  schedule changes).  Manual Away is sticky.
- Resume: the "schedule" pseudo-preset, async_resume_schedule (button), or
  picking the preset the schedule asks for.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

from tests.ha_harness import (
    PRESENCE,
    SCHEDULE,
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
    drain_tasks,
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


class _FakeCallLater:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, hass, delay, callback):
        entry = {"delay": delay, "callback": callback, "cancelled": False}
        self.calls.append(entry)

        def cancel():
            entry["cancelled"] = True

        return cancel

    @property
    def active(self):
        return [c for c in self.calls if not c["cancelled"]]


# ===========================================================================
# Pure helpers
# ===========================================================================

class TestParseSchedulePreset:
    @pytest.mark.parametrize(
        "state,expected",
        [
            ("comfort", "comfort"),
            ("Comfort", "comfort"),
            ("night", "eco"),
            ("Night", "eco"),
            ("eco", "eco"),
            ("away", "away"),
            ("frost_protection", "frost_protection"),
            ("Frost Protection", "frost_protection"),
            ("frost", "frost_protection"),
            ("boost", None),
            ("none", None),
            ("unavailable", None),
            ("unknown", None),
            (None, None),
            ("", None),
        ],
    )
    def test_matrix(self, state, expected):
        assert _ctrl.parse_schedule_preset(state) == expected


class TestScheduleController:
    def test_first_value_applies(self):
        c = _ctrl.ScheduleController()
        assert c.schedule_changed("eco") is True
        assert c.schedule_preset == "eco"

    def test_same_value_does_nothing(self):
        c = _ctrl.ScheduleController()
        c.schedule_changed("eco")
        assert c.schedule_changed("eco") is False

    def test_invalid_value_does_nothing(self):
        c = _ctrl.ScheduleController()
        c.schedule_changed("eco")
        assert c.schedule_changed(None) is False
        assert c.schedule_preset == "eco"

    def test_change_ends_override_whichever_first(self):
        cl = _FakeCallLater()
        c = _ctrl.ScheduleController()
        c.schedule_changed("eco")
        c.start_override(None, 60, 1000.0, lambda _: None, call_later=cl)
        assert c.override_active and len(cl.active) == 1
        assert c.schedule_changed("comfort") is True
        assert not c.override_active
        assert cl.active == []

    def test_sticky_override_survives_changes(self):
        c = _ctrl.ScheduleController()
        c.schedule_changed("eco")
        c.start_override(None, 60, 1000.0, lambda _: None, sticky=True,
                         call_later=_FakeCallLater())
        assert c.override_until is None
        assert c.schedule_changed("comfort") is False
        assert c.override_active
        assert c.schedule_preset == "comfort"

    def test_duration_zero_has_no_timer(self):
        cl = _FakeCallLater()
        c = _ctrl.ScheduleController()
        c.start_override(None, 0, 1000.0, lambda _: None, call_later=cl)
        assert c.override_active and c.override_until is None
        assert cl.calls == []
        assert c.remaining_minutes(1000.0) == 0

    def test_timed_override(self):
        cl = _FakeCallLater()
        c = _ctrl.ScheduleController()
        c.start_override(None, 90, 1000.0, lambda _: None, call_later=cl)
        assert cl.calls[0]["delay"] == 90 * 60
        assert c.override_until == 1000.0 + 5400
        assert c.remaining_minutes(1000.0) == 90
        assert c.remaining_minutes(1000.0 + 5400 - 30) == 1
        assert c.remaining_minutes(1000.0 + 6000) == 0

    def test_restart_override_replaces_timer(self):
        cl = _FakeCallLater()
        c = _ctrl.ScheduleController()
        c.start_override(None, 30, 0.0, lambda _: None, call_later=cl)
        c.start_override(None, 30, 60.0, lambda _: None, call_later=cl)
        assert len(cl.active) == 1

    def test_rearm_future(self):
        cl = _FakeCallLater()
        c = _ctrl.ScheduleController()
        c.override_active, c.override_until = True, 1600.0
        assert c.rearm(None, 1000.0, lambda _: None, call_later=cl) is False
        assert cl.calls[0]["delay"] == pytest.approx(600.0)

    def test_rearm_expired_clears(self):
        c = _ctrl.ScheduleController()
        c.override_active, c.override_until = True, 900.0
        assert c.rearm(None, 1000.0, lambda _: None, call_later=_FakeCallLater()) is True
        assert not c.override_active

    def test_rearm_untimed_is_noop(self):
        cl = _FakeCallLater()
        c = _ctrl.ScheduleController()
        c.override_active = True
        assert c.rearm(None, 1000.0, lambda _: None, call_later=cl) is False
        assert c.override_active and cl.calls == []

    def test_persisted_round_trip(self):
        p = _ctrl.PersistedAutomationState(
            schedule_preset="eco",
            schedule_override_active=True,
            schedule_override_until=1234.0,
            schedule_override_sticky=False,
        )
        q = _ctrl.PersistedAutomationState.from_dict(p.as_dict())
        assert q == p
        c = _ctrl.ScheduleController()
        c.load(q)
        assert (c.schedule_preset, c.override_active, c.override_until) == ("eco", True, 1234.0)

    def test_load_ignores_until_without_override(self):
        c = _ctrl.ScheduleController()
        c.load(_ctrl.PersistedAutomationState(schedule_override_until=5.0))
        assert c.override_until is None and not c.override_active


# ===========================================================================
# End-to-end (real entity code, stubbed HA)
# ===========================================================================

def _entity(schedule_state="night", *, override_min=0, **kw):
    options = {"schedule_override_min": override_min}
    ent = _make_entity(options, schedule=True, **kw)
    ent.hass.keep_tasks = True
    ent.hass.set(SCHEDULE, schedule_state)
    ent.hass.set(TRV, "heat", {"temperature": 20.0})
    _run(_start(ent))
    drain_tasks(ent)
    return ent


def _schedule(ent, state):
    ent.hass.set(SCHEDULE, state)
    ent._async_schedule_changed(_event(state))
    drain_tasks(ent)


def _override_timers():
    return [
        c for c in _active_timers()
        if getattr(c["callback"], "__name__", "") == "_async_schedule_override_expired"
    ]


def _fire_override_timer(ent):
    (entry,) = _override_timers()
    entry["cancelled"] = True
    _run(entry["callback"](None))
    drain_tasks(ent)


def _preset(ent, preset):
    _run(_select_preset(ent, preset))
    drain_tasks(ent)


@e2e
class TestFollowSchedule:
    def test_startup_follows_helper(self):
        ent = _entity("night")
        assert ent.preset_mode == "eco"
        assert ent.extra_state_attributes["schedule_preset"] == "eco"
        assert ent.extra_state_attributes["schedule_override_active"] is False

    def test_changes_are_followed(self):
        ent = _entity("night")
        _schedule(ent, "comfort")
        assert ent.preset_mode == "comfort"
        assert ent.target_temperature == 20.5
        _schedule(ent, "Frost Protection")
        assert ent.preset_mode == "frost_protection"

    def test_unsupported_value_ignored(self):
        ent = _entity("night")
        _schedule(ent, "boost")
        _schedule(ent, "unavailable")
        assert ent.preset_mode == "eco"

    def test_schedule_preset_offered_only_when_configured(self):
        ent = _entity("night")
        assert "schedule" in ent._attr_preset_modes
        plain = _make_entity()
        assert "schedule" not in plain._attr_preset_modes

    def test_night_icon(self):
        ent = _entity("night")
        assert ent.icon == "mdi:weather-night"


@e2e
class TestOverrideUntilNextChange:
    """Override duration 0 (path a)."""

    def test_manual_preset_holds_until_schedule_changes(self):
        ent = _entity("night")
        _preset(ent, "comfort")
        attrs = ent.extra_state_attributes
        assert attrs["schedule_override_active"] is True
        assert attrs["schedule_override_until"] is None
        assert ent.schedule_override_remaining_minutes == 0
        assert _override_timers() == []
        _schedule(ent, "away")
        assert ent.preset_mode == "away"
        assert ent.extra_state_attributes["schedule_override_active"] is False

    def test_manual_temperature_is_an_override(self):
        ent = _entity("night")
        _run(ent.async_set_temperature(temperature=22.0))
        assert ent.preset_mode == "none"
        assert ent.extra_state_attributes["schedule_override_active"] is True
        _schedule(ent, "comfort")
        assert ent.preset_mode == "comfort"

    def test_follow_physical_dial_is_an_override(self):
        ent = _entity("night")
        ent._config_entry.options["follow_tado_input"] = True
        ent._last_sent_setpoint = 16.0
        ent._last_command_sent_ts = 0.0
        old = _State("heat", {"temperature": 16.0})
        new = _State("heat", {"temperature": 22.0})
        ent._async_tado_state_changed(
            type("E", (), {"data": {"old_state": old, "new_state": new}})()
        )
        drain_tasks(ent)
        assert ent.preset_mode == "none"
        assert ent.extra_state_attributes["schedule_override_active"] is True


@e2e
class TestOverrideTimed:
    """Override duration > 0 (path b), whichever comes first."""

    def test_timer_returns_to_schedule(self):
        ent = _entity("night", override_min=60)
        _preset(ent, "comfort")
        assert len(_override_timers()) == 1
        assert _override_timers()[0]["delay"] == 3600
        assert ent.schedule_override_remaining_minutes == 60
        assert ent.extra_state_attributes["schedule_override_until"] is not None
        _fire_override_timer(ent)
        assert ent.preset_mode == "eco"
        assert ent.extra_state_attributes["schedule_override_active"] is False
        assert ent.schedule_override_remaining_minutes == 0

    def test_schedule_change_first_ends_override(self):
        ent = _entity("night", override_min=60)
        _preset(ent, "comfort")
        _schedule(ent, "away")
        assert ent.preset_mode == "away"
        assert _override_timers() == []

    def test_new_manual_change_restarts_timer(self):
        ent = _entity("night", override_min=60)
        _preset(ent, "comfort")
        _run(ent.async_set_temperature(temperature=21.0))
        assert len(_override_timers()) == 1


@e2e
class TestResume:
    def test_selecting_schedule_preset_resumes(self):
        ent = _entity("night")
        _preset(ent, "comfort")
        _preset(ent, "schedule")
        assert ent.preset_mode == "eco"
        assert ent.extra_state_attributes["schedule_override_active"] is False

    def test_resume_method_resumes_and_cancels_timer(self):
        ent = _entity("night", override_min=60)
        _preset(ent, "comfort")
        _run(ent.async_resume_schedule())
        drain_tasks(ent)
        assert ent.preset_mode == "eco"
        assert _override_timers() == []

    def test_picking_the_scheduled_preset_ends_override(self):
        ent = _entity("night")
        _preset(ent, "comfort")
        _preset(ent, "eco")
        assert ent.extra_state_attributes["schedule_override_active"] is False
        _schedule(ent, "comfort")
        assert ent.preset_mode == "comfort"

    def test_resume_without_schedule_raises(self):
        ent = _make_entity()
        _run(_start(ent))
        with pytest.raises(sys.modules["homeassistant.exceptions"].ServiceValidationError) as err:
            _run(ent.async_resume_schedule())
        assert err.value.translation_key == "no_schedule"


@e2e
class TestAwayAndAutomations:
    def test_manual_away_is_sticky(self):
        ent = _entity("night", override_min=30)
        _preset(ent, "away")
        assert _override_timers() == []
        _schedule(ent, "comfort")
        assert ent.preset_mode == "away"
        assert ent.extra_state_attributes["schedule_preset"] == "comfort"
        _preset(ent, "schedule")
        assert ent.preset_mode == "comfort"

    def test_presence_away_ignores_schedule_and_returns_to_current_block(self):
        ent = _entity("comfort", window=False, presence=True)
        ent.hass.set(PRESENCE, "off")
        ent._async_presence_changed(_event("off"))
        _fire_last_timer()
        drain_tasks(ent)
        assert ent.preset_mode == "away"
        _schedule(ent, "night")
        assert ent.preset_mode == "away"
        ent.hass.set(PRESENCE, "on")
        ent._async_presence_changed(_event("on"))
        _fire_last_timer()  # home delay
        drain_tasks(ent)
        assert ent.preset_mode == "eco"

    def test_window_open_ignores_schedule_and_restores_current_block(self):
        ent = _entity("comfort")
        ent.hass.set(WINDOW, "on")
        ent._async_window_changed(_event("on"))
        _fire_last_timer()
        drain_tasks(ent)
        assert ent.preset_mode == "frost_protection"
        _schedule(ent, "night")
        assert ent.preset_mode == "frost_protection"
        ent.hass.set(WINDOW, "off")
        ent._async_window_changed(_event("off"))
        drain_tasks(ent)
        assert ent.preset_mode == "eco"

    def test_override_expiring_while_away_updates_return_preset(self):
        ent = _entity("night", override_min=60, window=False, presence=True)
        _preset(ent, "comfort")
        ent.hass.set(PRESENCE, "off")
        ent._async_presence_changed(_event("off"))
        # presence away timer is the last non-override timer
        away = [c for c in _active_timers() if c not in _override_timers()][-1]
        away["cancelled"] = True
        _run(away["callback"](None))
        assert ent._presence_ctrl.get_saved().preset == "comfort"
        _fire_override_timer(ent)
        assert ent.preset_mode == "away"
        assert ent._presence_ctrl.get_saved().preset == "eco"

    def test_boost_ends_on_schedule_not_pre_boost_preset(self):
        ent = _entity("night")
        _preset(ent, "comfort")
        _preset(ent, "boost")
        assert ent.extra_state_attributes["schedule_override_active"] is True
        boost = [c for c in _active_timers()
                 if getattr(c["callback"], "__name__", "") == "_async_boost_expired"]
        boost[0]["cancelled"] = True
        _run(boost[0]["callback"](None))
        drain_tasks(ent)
        assert ent.preset_mode == "eco"
        assert ent.extra_state_attributes["schedule_override_active"] is False

    def test_off_is_not_an_override_and_on_resumes(self):
        ent = _entity("night")
        _preset(ent, "comfort")
        _run(ent.async_set_hvac_mode(_climate.HVACMode.OFF))
        _run(ent.async_set_hvac_mode(_climate.HVACMode.HEAT))
        drain_tasks(ent)
        assert ent.preset_mode == "eco"
        assert ent.extra_state_attributes["schedule_override_active"] is False


@e2e
class TestSummerWithSchedule:
    def _summer_entity(self):
        ent = _entity("night", summer=True)
        ent.hass.set(SUMMER, "off")
        return ent

    def test_schedule_ignored_during_summer_and_used_after(self):
        ent = self._summer_entity()
        _preset(ent, "comfort")
        ent.hass.set(SUMMER, "on")
        ent._async_summer_changed(_event("on"))
        drain_tasks(ent)
        assert ent.extra_state_attributes["schedule_override_active"] is False
        _schedule(ent, "away")
        assert ent.preset_mode == "frost_protection"
        ent.hass.set(SUMMER, "off")
        ent._async_summer_changed(_event("off"))
        drain_tasks(ent)
        assert ent.preset_mode == "away"

    def test_resume_refused_in_summer(self):
        ent = self._summer_entity()
        ent.hass.set(SUMMER, "on")
        ent._async_summer_changed(_event("on"))
        drain_tasks(ent)
        with pytest.raises(sys.modules["homeassistant.exceptions"].ServiceValidationError):
            _run(ent.async_set_preset_mode("schedule"))


@e2e
class TestScheduleAcrossRestart:
    def _boot(self, *, helper, persisted, last_preset="eco", override_min=0):
        ent = _make_entity({"schedule_override_min": override_min}, schedule=True)
        ent.hass.keep_tasks = True
        if helper is not None:
            ent.hass.set(SCHEDULE, helper)
        ent.hass.set(WINDOW, "off")
        last = _State("heat", {"preset_mode": last_preset, "temperature": 20.5})
        _run(_start(ent, last_state=last, extra=_Extra(persisted.as_dict())))
        drain_tasks(ent)
        return ent

    def test_timed_override_rearmed(self):
        p = _ctrl.PersistedAutomationState(
            schedule_preset="eco", schedule_override_active=True,
            schedule_override_until=time.time() + 1200,
        )
        ent = self._boot(helper="night", persisted=p, last_preset="comfort", override_min=60)
        assert ent.preset_mode == "comfort"
        assert len(_override_timers()) == 1
        assert _override_timers()[0]["delay"] <= 1200
        assert 19 <= ent.schedule_override_remaining_minutes <= 20

    def test_override_expired_while_down(self):
        p = _ctrl.PersistedAutomationState(
            schedule_preset="eco", schedule_override_active=True,
            schedule_override_until=time.time() - 60,
        )
        ent = self._boot(helper="night", persisted=p, last_preset="comfort", override_min=60)
        assert ent.preset_mode == "eco"
        assert _override_timers() == []

    def test_schedule_changed_while_down_ends_override(self):
        p = _ctrl.PersistedAutomationState(
            schedule_preset="eco", schedule_override_active=True,
        )
        ent = self._boot(helper="comfort", persisted=p, last_preset="none")
        assert ent.preset_mode == "comfort"
        assert ent.extra_state_attributes["schedule_override_active"] is False

    def test_untimed_override_survives_restart(self):
        p = _ctrl.PersistedAutomationState(
            schedule_preset="eco", schedule_override_active=True,
        )
        ent = self._boot(helper="night", persisted=p, last_preset="comfort")
        assert ent.preset_mode == "comfort"
        assert ent.extra_state_attributes["schedule_override_active"] is True

    def test_sticky_away_survives_schedule_change_while_down(self):
        p = _ctrl.PersistedAutomationState(
            schedule_preset="eco", schedule_override_active=True,
            schedule_override_sticky=True,
        )
        ent = self._boot(helper="comfort", persisted=p, last_preset="away")
        assert ent.preset_mode == "away"

    def test_helper_not_loaded_yet_uses_persisted_value(self):
        p = _ctrl.PersistedAutomationState(schedule_preset="eco")
        ent = self._boot(helper=None, persisted=p, last_preset="comfort")
        assert ent.preset_mode == "eco"
        # helper appears later with the same value: nothing changes
        _schedule(ent, "night")
        assert ent.preset_mode == "eco"

    def test_snapshot_round_trip(self):
        ent = _entity("night", override_min=45)
        _preset(ent, "comfort")
        data = ent.extra_restore_state_data.as_dict()
        assert data["schedule_preset"] == "eco"
        assert data["schedule_override_active"] is True
        assert data["schedule_override_until"] is not None
