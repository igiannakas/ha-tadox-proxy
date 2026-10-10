"""Auto-tune wired into the real climate entity (stubbed Home Assistant).

Covers: off by default, manual derivative time, learned values used by the
regulator, persistence through the restore-state data (without disturbing
the automation snapshot), reset, baseline changes, eligibility, and the
diagnostic attributes.
"""
from __future__ import annotations

import pytest

from tests.ha_harness import (
    _climate,
    _Extra,
    _make_entity,
    _run,
    _start,
    e2e,
    reset_timers,
)

pytestmark = e2e

AUTOTUNE_ON = {"autotune_enabled": True, "correction_kp": 0.6, "correction_ki": 0.002}


@pytest.fixture(autouse=True)
def _reset():
    reset_timers()
    yield
    reset_timers()


def _feed(ent, room=19.0, tado=21.0, sp=None):
    ent.coordinator.data = {
        "room_temp": room, "room_temp_ts": None,
        "tado_internal_temp": tado, "tado_setpoint": sp,
    }


class _Clock:
    def __init__(self, monkeypatch, start=1_800_000_000.0):
        self.now = start
        monkeypatch.setattr("time.time", lambda: self.now)

    def tick(self, s=60.0):
        self.now += s


def test_off_by_default_uses_configured_gains():
    ent = _make_entity({"correction_kp": 0.7, "correction_ki": 0.001})
    _run(_start(ent))
    assert not ent.autotune_enabled
    assert ent._config.tuning.kp == 0.7
    assert ent._config.tuning.ki == 0.001
    assert ent._config.tuning.td_s == 0.0
    assert ent.autotune_summary()["status"] == "disabled"
    assert ent.extra_state_attributes["autotune_status"] == "disabled"


def test_manual_derivative_time_reaches_regulator():
    ent = _make_entity({"derivative_time_min": 20})
    _run(_start(ent))
    assert ent._config.tuning.td_s == 1200.0
    assert ent._regulator.cfg.tuning.td_s == 1200.0
    assert ent.extra_state_attributes["correction_td_min"] == 20.0


def _learned_state(kp=0.5, ki=0.001, td=600.0, base=(0.6, 0.002, 0.0)):
    old = _make_entity(dict(AUTOTUNE_ON))
    d = old._autotuner.as_dict()
    d["baseline"] = {"kp": base[0], "ki": base[1], "td_s": base[2]}
    d["params"] = {"kp": kp, "ki": ki, "td_s": td}
    return d


def test_restored_learned_values_are_used():
    ent = _make_entity(dict(AUTOTUNE_ON))
    extra = _Extra({"version": 1, "autotune": _learned_state()})
    _run(_start(ent, extra=extra))
    assert ent._config.tuning.kp == pytest.approx(0.5)
    assert ent._config.tuning.ki == pytest.approx(0.001)
    assert ent._config.tuning.td_s == pytest.approx(600.0)
    assert ent.active_tuning.kp == pytest.approx(0.5)
    assert ent.configured_tuning.kp == 0.6


def test_learned_values_ignored_when_disabled():
    ent = _make_entity({"correction_kp": 0.6, "correction_ki": 0.002})
    _run(_start(ent, extra=_Extra({"version": 1, "autotune": _learned_state()})))
    assert ent._config.tuning.kp == 0.6
    assert ent._config.tuning.td_s == 0.0


def test_changed_baseline_discards_learned_values():
    ent = _make_entity({**AUTOTUNE_ON, "correction_kp": 0.9})
    _run(_start(ent, extra=_Extra({"version": 1, "autotune": _learned_state()})))
    assert ent._config.tuning.kp == 0.9


def test_restore_data_round_trip_keeps_automation_snapshot():
    ent = _make_entity(dict(AUTOTUNE_ON))
    _run(_start(ent, extra=_Extra({"version": 1, "autotune": _learned_state()})))
    data = ent.extra_restore_state_data.as_dict()
    assert data["autotune"]["params"]["kp"] == pytest.approx(0.5)
    # the automation snapshot still parses
    persisted = _climate.PersistedAutomationState.from_dict(data)
    assert persisted is not None
    # and a fresh entity picks the learned values up again
    ent2 = _make_entity(dict(AUTOTUNE_ON))
    _run(_start(ent2, extra=_Extra(data)))
    assert ent2._config.tuning.kp == pytest.approx(0.5)


def test_reset_returns_to_configured():
    ent = _make_entity(dict(AUTOTUNE_ON))
    _run(_start(ent, extra=_Extra({"version": 1, "autotune": _learned_state()})))
    _run(ent.async_reset_autotune())
    assert ent._config.tuning.kp == 0.6
    assert ent._config.tuning.ki == 0.002
    assert ent._config.tuning.td_s == 0.0


def test_options_update_keeps_learned_values_when_baseline_unchanged():
    ent = _make_entity(dict(AUTOTUNE_ON))
    _run(_start(ent, extra=_Extra({"version": 1, "autotune": _learned_state()})))
    ent._config_entry.options = {**ent._config_entry.options, "comfort_target": 21.0}
    _run(ent._async_config_entry_updated(None, ent._config_entry))
    assert ent._config.tuning.kp == pytest.approx(0.5)
    # switching auto-tune off falls back to the configured values at once
    ent._config_entry.options = {**ent._config_entry.options, "autotune_enabled": False}
    _run(ent._async_config_entry_updated(None, ent._config_entry))
    assert ent._config.tuning.kp == 0.6


def test_regulation_cycle_feeds_tuner_and_brake(monkeypatch):
    clock = _Clock(monkeypatch)
    ent = _make_entity({**AUTOTUNE_ON, "derivative_time_min": 20})
    _run(_start(ent))
    ent._autotuner.derivative_allowed = False   # keep the manual Td fixed
    ent._apply_active_tuning()
    seen = []
    orig = ent._autotuner.observe
    ent._autotuner.observe = lambda s: (seen.append(s), orig(s))[1]
    room = 18.0
    for _ in range(15):
        _feed(ent, room=room, tado=21.0)
        _run(ent._async_regulation_cycle("timer"))
        clock.tick(60)
        room += 0.03                       # 1.8 °C/h rise
    assert len(seen) == 15
    assert all(s.eligible for s in seen)
    attrs = ent.extra_state_attributes
    assert attrs["d_correction_c"] < 0     # brake engaged while rising
    assert seen[-1].tado_setpoint_c is not None


def test_not_eligible_while_window_open(monkeypatch):
    _Clock(monkeypatch)
    ent = _make_entity(dict(AUTOTUNE_ON))
    _run(_start(ent))
    seen = []
    orig = ent._autotuner.observe
    ent._autotuner.observe = lambda s: (seen.append(s), orig(s))[1]
    ent._window_ctrl.activate("comfort", None)
    _feed(ent)
    _run(ent._async_regulation_cycle("timer"))
    assert seen and not seen[-1].eligible


def test_tuner_not_fed_when_disabled(monkeypatch):
    _Clock(monkeypatch)
    ent = _make_entity({"correction_kp": 0.6})
    _run(_start(ent))
    called = []
    ent._autotuner.observe = lambda s: called.append(s)
    _feed(ent)
    _run(ent._async_regulation_cycle("timer"))
    assert called == []


def test_degraded_sensor_hides_room_temp_and_slope(monkeypatch):
    clock = _Clock(monkeypatch)
    ent = _make_entity({**AUTOTUNE_ON, "derivative_time_min": 20})
    _run(_start(ent))
    seen = []
    orig = ent._autotuner.observe
    ent._autotuner.observe = lambda s: (seen.append(s), orig(s))[1]
    _feed(ent, room=19.0)
    _run(ent._async_regulation_cycle("timer"))
    clock.tick(60)
    _feed(ent, room=None)                  # sensor drops out, grace bridges it
    _run(ent._async_regulation_cycle("timer"))
    assert ent._sensor_degraded
    assert seen[-1].room_temp_c is None
    assert not seen[-1].eligible
    assert ent.extra_state_attributes["d_correction_c"] == 0.0
