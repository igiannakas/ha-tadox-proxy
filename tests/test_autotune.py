"""Tests for the background PI(D) auto-tuner and the derivative brake.

Three layers:

1. The pieces: robust slope, episode analysis, the one-sided brake.
2. The tuner's safety contract: bounds, step limits, rate limits,
   confirmation of "more heat" moves, rollback, emergency detune,
   persistence.  Fuzzed where it matters.
3. Closed loop against ``plant_sim`` (room + Tado TRV with its own PI):
   the tuner must cut the measured overshoot and calm an oscillating room
   without ever leaving its bounds.
"""
from __future__ import annotations

import math
import random

import pytest

from tests.plant_sim import (
    PlantParams,
    ProxySettings,
    RoomSim,
    schedule_from,
)
from tests.pure_modules import autotune as A
from tests.pure_modules import parameters as P
from tests.pure_modules import regulation as R

CFG = P.AutotuneConfig()
DAY = 86400.0


def _tuner(kp=0.6, ki=0.002, td=0.0, deriv=True, cfg=None, stored=None):
    return A.Autotuner(cfg or P.AutotuneConfig(), A.Tuning(kp, ki, td),
                       derivative_allowed=deriv, stored=stored)


def _heatup(ts, overshoot, *, theta=780.0, rate=2.0, coast=1800.0, td=0.0,
            truncated=False, kp=0.6, ki=0.002):
    return A.HeatupMetrics(
        ts=ts, start_ts=ts - 3 * 3600, setpoint_c=20.0, step_c=1.5,
        overshoot_c=overshoot, theta_s=theta, rate_c_per_h=rate, demand0_c=2.0,
        coast_s=coast, coast_rise_c=0.3, reach_s=3000.0, kp=kp, ki=ki, td_s=td,
        truncated=truncated,
    )


def _hold(ts, *, mean=0.0, osc=False, amp=0.1, period=None, half=0, heating=0.5):
    return A.HoldMetrics(
        ts=ts, mean_error_c=mean, sd_c=amp / 2, half_cycles=half, period_s=period,
        amplitude_c=amp, heating_fraction=heating, oscillating=osc,
    )


# ---------------------------------------------------------------------------
# 1. Pieces
# ---------------------------------------------------------------------------

class TestSlopeEstimator:
    def test_linear_ramp(self):
        est = A.SlopeEstimator()
        for k in range(15):
            est.add(k * 60.0, 19.0 + 0.02 * k)   # 1.2 °C/h
        assert est.slope_c_per_s() * 3600 == pytest.approx(1.2, rel=1e-6)

    def test_single_glitch_ignored(self):
        est = A.SlopeEstimator()
        for k in range(13):
            v = 20.0 + (1.5 if k == 12 else 0.0)  # last reading wild
            est.add(k * 60.0, v)
        assert abs(est.slope_c_per_s() * 3600) < 0.05

    def test_needs_half_a_window(self):
        est = A.SlopeEstimator()
        for k in range(4):
            est.add(k * 60.0, 20.0 + k)
        assert est.slope_c_per_s() is None

    def test_gap_resets(self):
        est = A.SlopeEstimator()
        for k in range(12):
            est.add(k * 60.0, 20.0)
        est.add(12 * 60.0 + 900.0, 25.0)          # 15 min gap
        assert est.slope_c_per_s() is None

    def test_rejects_non_finite(self):
        est = A.SlopeEstimator()
        for k in range(12):
            est.add(k * 60.0, float("nan") if k == 5 else 20.0)
        assert est.slope_c_per_s() == 0.0


class TestDerivativeBrake:
    def _cfg(self, td=1200.0, kp=0.6):
        c = P.RegulationConfig()
        c.tuning = P.CorrectionTuning(kp=kp, ki=0.0, td_s=td)
        c.gain_scheduling_enabled = False
        return c

    def _run(self, cfg, slope):
        return R.FeedforwardPiRegulator(cfg).compute(
            setpoint_c=20.0, room_temp_c=19.5, tado_internal_c=21.0,
            time_delta_s=0.0, state=R.RegulationState(), room_slope_c_per_s=slope,
        )

    def test_off_by_default(self):
        c = P.RegulationConfig()
        assert c.tuning.td_s == 0.0
        res = R.FeedforwardPiRegulator(c).compute(
            setpoint_c=20.0, room_temp_c=19.5, tado_internal_c=21.0,
            time_delta_s=0.0, state=R.RegulationState(), room_slope_c_per_s=1e-3)
        assert res.d_correction_c == 0.0

    def test_brakes_when_rising(self):
        cfg = self._cfg()
        rising = 2.0 / 3600                       # 2 °C/h
        res = self._run(cfg, rising)
        expected = -(1 + 0.6) * 1200 * (rising - 0.1 / 3600)
        assert res.d_correction_c == pytest.approx(expected, abs=0.01)
        assert res.target_for_tado_c == pytest.approx(
            round(20.0 + 1.5 + 0.6 * 0.5 + expected, 1), abs=0.051)

    @pytest.mark.parametrize("slope", [-5.0 / 3600, -0.5 / 3600, 0.0, 0.05 / 3600])
    def test_never_adds_heat(self, slope):
        assert self._run(self._cfg(), slope).d_correction_c == 0.0

    def test_clamped(self):
        res = self._run(self._cfg(td=2700), 50.0 / 3600)   # absurd slope
        assert res.d_correction_c == pytest.approx(-2.0)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), None])
    def test_bad_slope_ignored(self, bad):
        assert self._run(self._cfg(), bad).d_correction_c == 0.0

    def test_does_not_touch_integral(self):
        cfg = self._cfg()
        cfg.tuning.ki = 0.003
        st = R.RegulationState(integral_c=0.1)
        a = R.FeedforwardPiRegulator(cfg).compute(20.0, 19.9, 21.0, 60.0, st, 2.0 / 3600)
        b = R.FeedforwardPiRegulator(cfg).compute(20.0, 19.9, 21.0, 60.0, st, None)
        assert a.new_state.integral_c == b.new_state.integral_c


def _synthetic_heatup(theta_min=12.0, rate=2.0, sp=20.0, y0=18.5, overshoot=0.3,
                      cross_after_reach_min=-10.0, minutes=240, glitch=False):
    """Room: flat for theta, ramps at `rate`, then eases into sp + overshoot."""
    samples = []
    cross_idx = None
    t_reach = theta_min + (sp - y0) / rate * 60.0
    for k in range(minutes):
        t = k * 60.0
        m = float(k)
        if m < theta_min:
            y = y0
        elif m < t_reach:
            y = y0 + rate * (m - theta_min) / 60.0
        else:
            y = sp + overshoot * (1 - math.exp(-(m - t_reach) / 15.0))
            if m > t_reach + 60:
                y -= 0.002 * (m - t_reach - 60)
        if glitch and k == 30:
            y += 1.5
        d = 2.0 if m < t_reach + cross_after_reach_min else -0.5
        if d <= 0 and cross_idx is None:
            cross_idx = k
        samples.append((t, y, d))
    return samples, cross_idx


class TestAnalyseHeatup:
    def test_dead_time_and_rate(self):
        s, cross = _synthetic_heatup()
        m = A.analyse_heatup(s, start_ts=0.0, setpoint_c=20.0, cfg=CFG,
                             cross_index=cross, tuning=A.Tuning(0.6, 0.002))
        assert m.theta_s / 60 == pytest.approx(12.0, abs=1.5)
        assert m.rate_c_per_h == pytest.approx(2.0, rel=0.05)
        assert m.overshoot_c == pytest.approx(0.3, abs=0.02)
        assert m.coast_s is not None and m.coast_s > 0

    def test_glitch_does_not_move_dead_time(self):
        s, cross = _synthetic_heatup(glitch=True)
        m = A.analyse_heatup(s, start_ts=0.0, setpoint_c=20.0, cfg=CFG,
                             cross_index=cross, tuning=A.Tuning(0.6, 0.002))
        assert m.theta_s / 60 == pytest.approx(12.0, abs=1.5)

    def test_small_step_gives_no_dead_time(self):
        s, cross = _synthetic_heatup(y0=19.5)
        m = A.analyse_heatup(s, start_ts=0.0, setpoint_c=20.0, cfg=CFG,
                             cross_index=cross, tuning=A.Tuning(0.6, 0.002))
        assert m.theta_s is None           # 0.5 °C step: too small to trust
        assert m.overshoot_c is not None   # ...but overshoot still counts

    def test_truncated_low_peak_is_unknown(self):
        """A cut-off episode must not argue for less braking."""
        s, cross = _synthetic_heatup(overshoot=0.1)
        cut = [x for x in s if x[0] <= (cross + 20) * 60]
        m = A.analyse_heatup(cut, start_ts=0.0, setpoint_c=20.0, cfg=CFG,
                             cross_index=cross, tuning=A.Tuning(0.6, 0.002),
                             truncated=True)
        assert m.overshoot_c is None
        assert m.coast_s is None

    def test_truncated_high_peak_is_kept(self):
        s, cross = _synthetic_heatup(overshoot=0.8)
        cut = [x for x in s if x[0] <= (cross + 20) * 60]
        m = A.analyse_heatup(cut, start_ts=0.0, setpoint_c=20.0, cfg=CFG,
                             cross_index=cross, tuning=A.Tuning(0.6, 0.002),
                             truncated=True)
        assert m.overshoot_c is not None and m.overshoot_c > CFG.overshoot_target_c

    def test_too_short(self):
        assert A.analyse_heatup([(0, 18, 1)] * 5, start_ts=0, setpoint_c=20, cfg=CFG,
                                cross_index=None, tuning=A.Tuning(0.6, 0.002)) is None


class TestAnalyseHold:
    def _samples(self, fn, hours=12, noise=0.0, seed=1):
        rng = random.Random(seed)
        return [(k * 60.0, fn(k * 60.0) + rng.gauss(0, noise), 0.5)
                for k in range(int(hours * 60))]

    def test_detects_oscillation(self):
        s = self._samples(lambda t: 0.4 * math.sin(2 * math.pi * t / 7200))
        m = A.analyse_hold(s, cfg=CFG, tuning=A.Tuning(0.6, 0.002))
        assert m.oscillating
        assert m.period_s == pytest.approx(7200, rel=0.1)

    def test_noise_is_not_oscillation(self):
        s = self._samples(lambda t: 0.0, noise=0.05)
        m = A.analyse_hold(s, cfg=CFG, tuning=A.Tuning(0.6, 0.002))
        assert not m.oscillating

    def test_slow_sawtooth_with_bursts_is_oscillation(self):
        """Short heating bursts + long passive cool-downs (seen in sims)."""
        def e(t):
            ph = (t % 32400) / 32400
            return -0.7 + 1.3 * ph if ph < 0.9 else 0.6 - 13 * (ph - 0.9)
        s = [(t, e(t), (1.0 if (t % 32400) / 32400 > 0.9 else -1.0))
             for t, _, _ in self._samples(lambda t: 0.0, hours=24)]
        m = A.analyse_hold(s, cfg=CFG, tuning=A.Tuning(0.6, 0.002))
        assert m.oscillating

    def test_offset(self):
        s = self._samples(lambda t: 0.25, noise=0.02)
        m = A.analyse_hold(s, cfg=CFG, tuning=A.Tuning(0.6, 0.002))
        assert m.mean_error_c == pytest.approx(0.25, abs=0.02)
        assert not m.oscillating


# ---------------------------------------------------------------------------
# 2. Safety contract
# ---------------------------------------------------------------------------

def _within_bounds(t: A.Autotuner) -> None:
    a, b, c = t.active_tuning(), t.baseline, t.cfg
    assert max(c.kp_min, b.kp * c.kp_rel_min) - 1e-12 <= a.kp <= min(c.kp_max, b.kp * c.kp_rel_max) + 1e-12
    if b.ki > 0:
        assert max(c.ki_min, b.ki * c.ki_rel_min) - 1e-15 <= a.ki <= min(c.ki_max, b.ki * c.ki_rel_max) + 1e-15
    else:
        assert a.ki == 0.0
    assert 0.0 <= a.td_s <= c.td_max_s


class TestSafety:
    def test_starts_at_baseline(self):
        t = _tuner()
        assert t.active_tuning() == A.Tuning(0.6, 0.002, 0.0)
        assert t.status == A.STATUS_LEARNING

    @pytest.mark.parametrize("seed", range(25))
    def test_fuzz_never_leaves_bounds_or_step_limits(self, seed):
        """Random (including absurd) evidence for weeks: bounds always hold."""
        rng = random.Random(seed)
        t = _tuner(kp=rng.choice([0.3, 0.6, 1.5]), ki=rng.choice([0.0, 0.001, 0.004]))
        now = 0.0
        for _ in range(300):
            now += rng.uniform(600, 12 * 3600)
            before = t.active_tuning()
            if rng.random() < 0.6:
                m = _heatup(now, rng.uniform(-1.5, 3.0), theta=rng.uniform(100, 5000),
                            rate=rng.uniform(0.01, 20), coast=rng.choice([None, rng.uniform(0, 20000)]),
                            td=before.td_s, truncated=rng.random() < 0.2)
                if rng.random() < 0.1:
                    m.overshoot_c = None
                t._now = now
                t._on_heatup(m, now)
            else:
                t._now = now
                t._on_hold(_hold(now, mean=rng.uniform(-1, 1), osc=rng.random() < 0.3,
                                 amp=rng.uniform(0, 1.5), period=rng.uniform(600, 40000),
                                 half=rng.randint(0, 8), heating=rng.random()), now)
            after = t.active_tuning()
            _within_bounds(t)
            c = t.cfg
            if after.kp != before.kp:
                assert before.kp * c.kp_step_down * c.kp_step_down - 1e-12 <= after.kp \
                    <= before.kp * c.kp_step_up + 1e-12 or after == t._last_good
            if after.td_s != before.td_s and abs(after.td_s - before.td_s) > 1e-9:
                step = max(c.td_step_min_s, c.td_step_frac * before.td_s)
                assert abs(after.td_s - before.td_s) <= step + 1e-6 or after == t._last_good

    def test_more_heat_needs_confirmation(self):
        """A single sag must not raise Kp (no braking active)."""
        t = _tuner(deriv=False)
        t._on_heatup(_heatup(DAY, -0.5, coast=None), DAY)
        assert t.active_tuning().kp == 0.6
        t._on_heatup(_heatup(2 * DAY, -0.5, coast=None), 2 * DAY)
        # second consecutive sag: trim raised; Kp may now move up (bounded)
        assert t._trim["kp"] > 1.0

    def test_rate_limit_for_more_heat(self):
        t = _tuner(deriv=False)
        t._last_change_ts = DAY
        t._trim["kp"] = 1.4
        assert t._maybe_update(DAY + 3600, "test") is None        # < 12 h
        assert t._maybe_update(DAY + 13 * 3600, "test") is not None
        assert t.active_tuning().kp == pytest.approx(0.6 * 1.10)

    def test_gentler_moves_allowed_sooner(self):
        t = _tuner()
        t._last_change_ts = DAY
        t._trim["ki"] = 0.5
        assert t._maybe_update(DAY + 1800, "test") is None         # < 3 h
        assert t._maybe_update(DAY + 4 * 3600, "test") is not None

    def test_first_heatup_starts_braking_and_lowers_ki(self):
        t = _tuner()
        reason = t._on_heatup(_heatup(DAY, 0.6, coast=1800), DAY)
        a = t.active_tuning()
        assert reason and a.td_s == pytest.approx(300.0)           # first 5-min step
        assert a.ki < 0.002                                         # towards SIMC
        assert a.kp == 0.6

    def test_rollback_on_worse_overshoot(self):
        t = _tuner()
        t._on_heatup(_heatup(DAY, 0.3), DAY)                       # change #1
        changed = t.active_tuning()
        assert t._pending is not None
        prev = A._tuning_from(t._pending["prev"])
        # next episode much worse -> roll back and freeze
        reason = t._on_heatup(_heatup(2 * DAY, 0.9, td=changed.td_s), 2 * DAY)
        assert reason.startswith("rolled back")
        assert t.active_tuning() == prev
        t._now = 2 * DAY + 1
        assert t.status == A.STATUS_FROZEN
        # frozen: nothing changes even with fresh evidence
        assert t._on_heatup(_heatup(2.5 * DAY, 0.9, td=prev.td_s), 2.5 * DAY) is None
        # and the failed direction is blocked for good
        assert t._blocked

    def test_rollback_on_new_oscillation(self):
        t = _tuner()
        t._on_heatup(_heatup(DAY, 0.3), DAY)
        prev = A._tuning_from(t._pending["prev"])
        reason = t._on_hold(_hold(DAY + 6 * 3600, osc=True, amp=0.2, period=3600, half=4), DAY + 6 * 3600)
        assert reason.startswith("rolled back")
        assert t.active_tuning() == prev

    def test_emergency_detune_bypasses_rate_limit(self):
        t = _tuner(kp=1.5, ki=0.004)
        t._last_change_ts = DAY
        t._now = DAY + 60
        reason = t._on_hold(_hold(DAY + 60, osc=True, amp=0.6, period=5000, half=5), DAY + 60)
        assert reason is not None
        a = t.active_tuning()
        assert a.kp < 1.5 and a.ki < 0.004

    def test_baseline_change_restarts(self):
        t = _tuner()
        t._on_heatup(_heatup(DAY, 0.6), DAY)
        assert t.active_tuning() != A.Tuning(0.6, 0.002, 0.0)
        t.set_baseline(A.Tuning(0.7, 0.002, 0.0), True)
        assert t.active_tuning() == A.Tuning(0.7, 0.002, 0.0)
        assert t.summary()["updates"] == 0

    def test_integral_off_is_respected(self):
        t = _tuner(ki=0.0)
        for k in range(1, 6):
            t._on_heatup(_heatup(k * DAY, 0.6), k * DAY)
            t._on_hold(_hold(k * DAY + 4 * 3600, mean=0.4), k * DAY + 4 * 3600)
        assert t.active_tuning().ki == 0.0

    def test_derivative_not_allowed(self):
        t = _tuner(deriv=False, td=0.0)
        for k in range(1, 6):
            t._on_heatup(_heatup(k * DAY, 0.8), k * DAY)
        assert t.active_tuning().td_s == 0.0

    def test_derivative_disallowed_uses_manual_td(self):
        t = _tuner(deriv=False, td=600.0)
        assert t.active_tuning().td_s == 600.0

    def test_warm_room_hold_never_raises_ki(self):
        """A too-warm room (coasting heat) is not an integral problem."""
        t = _tuner()
        for k in range(1, 8):
            t._on_hold(_hold(k * DAY, mean=-0.4), k * DAY)
        assert t._trim["ki"] == 1.0

    def test_cold_room_hold_raises_ki_after_confirmation(self):
        t = _tuner()
        t._on_hold(_hold(DAY, mean=0.3), DAY)
        assert t._trim["ki"] == 1.0
        t._on_hold(_hold(DAY + 4 * 3600, mean=0.3), DAY + 4 * 3600)
        assert t._trim["ki"] > 1.0

    def test_observe_survives_garbage(self):
        t = _tuner()
        for ts, sp, room, tado, cmd in [
            (float("nan"), 20, 19, 21, 22), (1.0, None, 19, 21, 22),
            (2.0, 20, None, 21, 22), (3.0, 20, float("inf"), None, None),
            (4.0, float("nan"), 19, 21, 22),
        ]:
            t.observe(A.AutotuneSample(ts, sp, room, tado, cmd))
        assert t.active_tuning() == A.Tuning(0.6, 0.002, 0.0)


class TestPersistence:
    def test_round_trip(self):
        t = _tuner()
        t._on_heatup(_heatup(DAY, 0.6), DAY)
        t._on_hold(_hold(DAY + 4 * 3600, mean=0.05), DAY + 4 * 3600)
        d = t.as_dict()
        t2 = _tuner(stored=d)
        assert t2.active_tuning() == t.active_tuning()
        assert t2.as_dict()["heatups"] == d["heatups"]
        assert t2._pending is not None

    def test_json_safe(self):
        import json
        t = _tuner()
        t._on_heatup(_heatup(DAY, 0.6), DAY)
        json.dumps(t.as_dict())

    @pytest.mark.parametrize("bad", [
        None, [], "x", {"version": 99},
        {"version": 1, "baseline": {"kp": 0.6, "ki": 0.002, "td_s": 0}, "params": {"kp": "x"}},
        {"version": 1, "baseline": {"kp": 0.6, "ki": 0.002, "td_s": 0},
         "params": {"kp": float("nan"), "ki": 0.002, "td_s": 0}},
    ])
    def test_garbage_gives_baseline(self, bad):
        t = _tuner(stored=bad)
        assert t.active_tuning() == A.Tuning(0.6, 0.002, 0.0)

    def test_out_of_bounds_stored_values_are_clamped(self):
        d = _tuner().as_dict()
        d["params"] = {"kp": 50.0, "ki": 1.0, "td_s": 1e6}
        t = _tuner(stored=d)
        _within_bounds(t)

    def test_baseline_mismatch_restarts(self):
        t = _tuner()
        t._on_heatup(_heatup(DAY, 0.6), DAY)
        t2 = _tuner(kp=0.8, stored=t.as_dict())
        assert t2.active_tuning() == A.Tuning(0.8, 0.002, 0.0)


# ---------------------------------------------------------------------------
# 3. Closed loop
# ---------------------------------------------------------------------------

LIVING = dict(trv_offset_c=-9.9, tau_room_h=50, t_out_c=12, trv_beta_c=6, tado_kp=0.5,
              tado_ti_min=60, heat_rate_c_per_h=2.5, tau_rad_min=12,
              water_delay_min=3, mix_lag_min=3)
SCHED = schedule_from([(0, 18.0), (6, 20.0), (8, 5.0), (17, 20.0), (22.5, 18.0)])


def _closed_loop(plant_kw, kp, ki, days, sched=SCHED, td=0.0, deriv=True):
    c = P.RegulationConfig()
    c.tuning = P.CorrectionTuning(kp=kp, ki=ki, td_s=td)
    c.gain_fine_threshold_c = 1.0
    tuner = _tuner(kp=kp, ki=ki, td=td, deriv=deriv)
    sim = RoomSim(PlantParams(**plant_kw), R, c, ProxySettings(), y0=19.0,
                  tuner=tuner, autotune_module=A, seed=3)
    tr = sim.run(days * 24, sched)
    return tuner, tr


def _evening_overshoot(tr, day):
    t0 = day * DAY
    idx = tr.window(t0 + 17 * 3600, t0 + 22.5 * 3600)
    return max(tr.y_true[i] for i in idx) - tr.sp[idx[0]]


class TestClosedLoop:
    def test_learns_to_stop_overshoot(self):
        tuner, tr = _closed_loop(LIVING, 0.6, 0.002, days=7)
        before = _evening_overshoot(tr, 0)
        after = _evening_overshoot(tr, 6)
        assert before > 0.3
        assert after < 0.25
        assert after < before - 0.15
        assert 0 < tuner.active_tuning().td_s <= P.AutotuneConfig().td_max_s
        assert all(0.1 <= kp <= 2.0 for kp in tr.kp)
        assert tuner.summary()["rollbacks"] == 0

    def test_calms_an_oscillating_room(self):
        plant = dict(LIVING, tado_ti_min=2000, tado_kp=1.5, water_delay_min=8, mix_lag_min=6)
        flat = schedule_from([(0, 20.0)])
        tuner, tr = _closed_loop(plant, 1.2, 0.003, days=7, sched=flat)

        def p2p(d0, d1):
            idx = tr.window(d0 * DAY, d1 * DAY)
            e = [tr.sp[i] - tr.y_true[i] for i in idx]
            return max(e) - min(e)

        assert p2p(0.5, 2) > 1.0
        assert p2p(5, 7) < 0.6
        a = tuner.active_tuning()
        assert a.kp < 1.2 and a.ki < 0.003

    def test_does_nothing_without_episodes(self):
        """A room held at a steady temperature with no swings is left alone."""
        flat = schedule_from([(0, 20.0)])
        tuner, tr = _closed_loop(dict(LIVING, tado_ti_min=200), 0.6, 0.0003, days=3,
                                 sched=flat)
        a = tuner.active_tuning()
        assert a.kp == 0.6
        assert a.td_s == 0.0
