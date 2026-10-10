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
            truncated=False, kp=0.6, ki=0.002, crossed=True):
    return A.HeatupMetrics(
        ts=ts, start_ts=ts - 3 * 3600, setpoint_c=20.0, step_c=1.5,
        overshoot_c=overshoot, theta_s=theta, rate_c_per_h=rate, demand0_c=2.0,
        coast_s=coast, coast_rise_c=0.3, reach_s=3000.0, kp=kp, ki=ki, td_s=td,
        truncated=truncated, crossed=crossed,
    )


def _force_more_heat(t, now):
    """Make the tuner raise Kp (a judged, "more heat" change)."""
    t._trim["kp"] = 1.4
    reason = t._maybe_update(now, "test")
    assert reason and t._pending is not None
    return A._tuning_from(t._pending["prev"])


def _hold(ts, *, mean=0.0, osc=False, amp=0.1, period=None, half=0, heating=0.5):
    return A.HoldMetrics(
        ts=ts, mean_error_c=mean, sd_c=amp / 2, half_cycles=half, period_s=period,
        amplitude_c=amp, heating_fraction=heating, oscillating=osc,
    )


# ---------------------------------------------------------------------------
# 1. Pieces
# ---------------------------------------------------------------------------

class TestSpikeFilter:
    def test_single_report_spike_is_dropped_even_if_reread(self):
        f = A.SpikeFilter()
        assert f.update(0, 20.0, 0) == 20.0
        # one wild report, re-read on three cycles (same report time)
        for k in range(1, 4):
            assert f.update(k * 60, 21.5, 60) == 20.0
        assert f.update(240, 20.05, 240) == 20.05

    def test_genuine_step_is_accepted_after_confirmation(self):
        f = A.SpikeFilter()
        f.update(0, 20.0, 0)
        assert f.update(60, 21.2, 60) == 20.0     # held back once
        assert f.update(120, 21.25, 120) == 21.25

    def test_fast_trend_is_followed(self):
        f = A.SpikeFilter()
        f.update(0, 20.0, 0)
        f.update(300, 21.1, 300)
        assert f.update(600, 22.2, 600) == 22.2

    def test_steady_room_after_genuine_jump_is_not_held(self):
        """HA sends no new report while the value is steady."""
        f = A.SpikeFilter()
        f.update(0, 20.0, 0)
        assert f.update(60, 18.8, 60) == 20.0          # held once
        for k in range(2, 20):                         # same report re-read
            v = f.update(k * 60, 18.8, 60)
        assert v == 18.8

    def test_aqara_style_half_degree_reports_pass(self):
        f = A.SpikeFilter()
        f.update(0, 20.0, 0)
        assert f.update(600, 20.55, 600) == 20.55

    def test_never_freezes(self):
        f = A.SpikeFilter(max_hold_s=900)
        f.update(0, 20.0, 0)
        f.update(60, 25.0, 60)
        f.update(120, 18.5, 120)                   # disagrees: still held
        assert f.update(1200, 23.0, 1200) == 23.0  # held too long: accept


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

    def test_unchanged_value_decays_slope(self):
        """A sensor that stops changing must not leave the brake engaged."""
        est = A.SlopeEstimator()
        t = 0.0
        v = 19.0
        for k in range(20):                       # 0.1 °C sensor rising at 2 °C/h
            t = k * 60.0
            v = round((19.0 + 2.0 * t / 3600) * 10) / 10
            est.add(t, v)
        assert est.slope_c_per_s() * 3600 > 1.0
        for k in range(20, 40):                   # then flat
            est.add(k * 60.0, v)
        assert abs(est.slope_c_per_s() * 3600) < 0.2

    def test_glitch_reread_over_several_cycles(self):
        est = A.SlopeEstimator()
        for k in range(15):
            wild = 6 <= k <= 9                    # one report seen on 4 cycles
            est.add(k * 60.0, 21.5 if wild else 20.0, 360.0 if wild else k * 60.0)
        assert abs(est.slope_c_per_s() * 3600) < 0.05

    def test_glitch_spanning_two_reports_is_bounded(self):
        """Two wild reports in a row get through the spike filter, but the
        Theil-Sen median keeps the brake's slope small."""
        est = A.SlopeEstimator()
        for k in range(15):
            wild = k in (6, 7)
            est.add(k * 60.0, 21.5 if wild else 20.0, k * 60.0)
        assert abs(est.slope_c_per_s() * 3600) < 0.05

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
                             cross_ts=cross * 60.0, tuning=A.Tuning(0.6, 0.002))
        assert m.theta_s / 60 == pytest.approx(12.0, abs=1.5)
        assert m.rate_c_per_h == pytest.approx(2.0, rel=0.05)
        assert m.overshoot_c == pytest.approx(0.3, abs=0.02)
        assert m.coast_s is not None and m.coast_s > 0

    def test_glitch_does_not_move_dead_time(self):
        s, cross = _synthetic_heatup(glitch=True)
        m = A.analyse_heatup(s, start_ts=0.0, setpoint_c=20.0, cfg=CFG,
                             cross_ts=cross * 60.0, tuning=A.Tuning(0.6, 0.002))
        assert m.theta_s / 60 == pytest.approx(12.0, abs=1.5)

    def test_small_step_gives_no_dead_time(self):
        s, cross = _synthetic_heatup(y0=19.5)
        m = A.analyse_heatup(s, start_ts=0.0, setpoint_c=20.0, cfg=CFG,
                             cross_ts=cross * 60.0, tuning=A.Tuning(0.6, 0.002))
        assert m.theta_s is None           # 0.5 °C step: too small to trust
        assert m.overshoot_c is not None   # ...but overshoot still counts

    def test_truncated_low_peak_is_unknown(self):
        """A cut-off episode must not argue for less braking."""
        s, cross = _synthetic_heatup(overshoot=0.1)
        cut = [x for x in s if x[0] <= (cross + 20) * 60]
        m = A.analyse_heatup(cut, start_ts=0.0, setpoint_c=20.0, cfg=CFG,
                             cross_ts=cross * 60.0, tuning=A.Tuning(0.6, 0.002),
                             truncated=True)
        assert m.overshoot_c is None
        assert m.truncated                 # any coast is only a lower bound

    def test_truncated_high_peak_is_kept(self):
        s, cross = _synthetic_heatup(overshoot=0.8)
        cut = [x for x in s if x[0] <= (cross + 20) * 60]
        m = A.analyse_heatup(cut, start_ts=0.0, setpoint_c=20.0, cfg=CFG,
                             cross_ts=cross * 60.0, tuning=A.Tuning(0.6, 0.002),
                             truncated=True)
        assert m.overshoot_c is not None and m.overshoot_c > CFG.overshoot_target_c
        assert m.truncated                 # its coast only counts as a lower bound

    def test_two_sample_glitch_does_not_inflate_overshoot(self):
        s, cross = _synthetic_heatup(overshoot=0.15)
        k = cross + 30
        s[k] = (s[k][0], s[k][1] + 1.5, s[k][2])
        s[k + 1] = (s[k + 1][0], s[k + 1][1] + 1.5, s[k + 1][2])
        m = A.analyse_heatup(s, start_ts=0.0, setpoint_c=20.0, cfg=CFG,
                             cross_ts=cross * 60.0, tuning=A.Tuning(0.6, 0.002))
        assert m.overshoot_c == pytest.approx(0.15, abs=0.05)

    def test_crossed_flag(self):
        s, cross = _synthetic_heatup()
        m = A.analyse_heatup(s, start_ts=0.0, setpoint_c=20.0, cfg=CFG,
                             cross_ts=None, tuning=A.Tuning(0.6, 0.002))
        assert not m.crossed and m.coast_s is None

    def test_too_short(self):
        assert A.analyse_heatup([(0, 18, 1)] * 5, start_ts=0, setpoint_c=20, cfg=CFG,
                                cross_ts=None, tuning=A.Tuning(0.6, 0.002)) is None


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

    def test_very_slow_cycle_is_not_oscillation(self):
        """A daily-ish swing (sun, routine) is not the controller."""
        s = self._samples(lambda t: 0.4 * math.sin(2 * math.pi * t / (14 * 3600)), hours=24)
        m = A.analyse_hold(s, cfg=CFG, tuning=A.Tuning(0.6, 0.002))
        assert not m.oscillating

    def test_offset(self):
        s = self._samples(lambda t: 0.25, noise=0.02)
        m = A.analyse_hold(s, cfg=CFG, tuning=A.Tuning(0.6, 0.002))
        assert m.mean_error_c == pytest.approx(0.25, abs=0.02)
        assert not m.oscillating


# ---------------------------------------------------------------------------
# 2. Safety contract
# ---------------------------------------------------------------------------

def _expected_limits(t: A.Autotuner, name: str) -> tuple[float, float]:
    """Documented bounds, computed independently of the tuner's own code:
    hard bounds intersected with the bounds relative to the configured value,
    widened to include the configured value itself."""
    c, b = t.cfg, t.baseline
    if name == "kp":
        lo, hi, base = max(c.kp_min, b.kp * c.kp_rel_min), min(c.kp_max, max(b.kp * c.kp_rel_max, c.kp_min)), b.kp
    elif name == "ki":
        if b.ki <= 0:
            return 0.0, 0.0
        lo, hi, base = max(c.ki_min, b.ki * c.ki_rel_min), min(c.ki_max, max(b.ki * c.ki_rel_max, c.ki_min)), b.ki
    else:
        lo, hi, base = 0.0, c.td_max_s, b.td_s
    lo = min(lo, hi)
    return min(lo, base), max(hi, base)


def _within_bounds(t: A.Autotuner) -> None:
    a = t.active_tuning()
    for name in ("kp", "ki"):
        lo, hi = _expected_limits(t, name)
        assert lo - 1e-12 <= getattr(a, name) <= hi + 1e-12, (name, getattr(a, name), lo, hi)
    if t.derivative_allowed:
        lo, hi = _expected_limits(t, "td_s")
        assert lo - 1e-9 <= a.td_s <= hi + 1e-9
    assert a.td_s <= max(t.cfg.td_max_s, t.baseline.td_s)


class TestSafety:
    def test_starts_at_baseline(self):
        t = _tuner()
        assert t.active_tuning() == A.Tuning(0.6, 0.002, 0.0)
        assert t.status == A.STATUS_LEARNING

    @pytest.mark.parametrize("seed", range(30))
    def test_fuzz_never_leaves_bounds_or_step_limits(self, seed):
        """Random (including absurd) evidence for weeks: bounds always hold,
        and every change except a rollback respects the per-step limits."""
        rng = random.Random(seed)
        t = _tuner(kp=rng.choice([0.0, 0.3, 0.6, 1.5, 4.0]),
                   ki=rng.choice([0.0, 0.001, 0.004, 0.02]),
                   td=rng.choice([0.0, 600.0]), deriv=rng.random() < 0.8)
        c = t.cfg
        now = 0.0
        rollbacks = 0
        for _ in range(300):
            now += rng.uniform(600, 12 * 3600)
            t._now = now
            before = t.active_tuning()
            if rng.random() < 0.6:
                m = _heatup(now, rng.uniform(-1.5, 3.0), theta=rng.uniform(100, 5000),
                            rate=rng.uniform(0.01, 20), coast=rng.choice([None, rng.uniform(0, 20000)]),
                            td=before.td_s, truncated=rng.random() < 0.2,
                            crossed=rng.random() < 0.8)
                if rng.random() < 0.1:
                    m.overshoot_c = None
                t._on_heatup(m, now)
            else:
                t._on_hold(_hold(now, mean=rng.uniform(-1, 1), osc=rng.random() < 0.3,
                                          amp=rng.uniform(0, 1.5), period=rng.uniform(600, 40000),
                                          half=rng.randint(0, 8), heating=rng.random()), now)
            after = t.active_tuning()
            _within_bounds(t)
            if t.summary()["rollbacks"] != rollbacks:
                # A rollback restores the previous values (and an emergency
                # may then take one step from there): exempt from step limits.
                rollbacks = t.summary()["rollbacks"]
                continue
            if after.kp != before.kp:
                assert before.kp * c.kp_step_down - 1e-12 <= after.kp <= before.kp * c.kp_step_up + 1e-12
            if after.ki != before.ki:
                assert before.ki * c.ki_step_down - 1e-15 <= after.ki <= before.ki * c.ki_step_up + 1e-15
            if abs(after.td_s - before.td_s) > 1e-9:
                step = max(c.td_step_min_s, c.td_step_frac * before.td_s)
                assert abs(after.td_s - before.td_s) <= step + 1e-6

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

    def test_first_heatup_starts_braking_second_moves_ki(self):
        t = _tuner()
        reason = t._on_heatup(_heatup(DAY, 0.6, coast=1800), DAY)
        a = t.active_tuning()
        assert reason and a.td_s == pytest.approx(300.0)           # first 5-min step
        assert a.ki == 0.002                # one dead time is not enough for SIMC
        t._on_heatup(_heatup(2 * DAY, 0.5, coast=1800, td=a.td_s), 2 * DAY)
        assert t.active_tuning().ki < 0.002                         # towards SIMC
        assert t.active_tuning().kp == 0.6

    def test_lower_bound_coast_only_raises_td(self):
        t = _tuner()
        t._on_heatup(_heatup(DAY, 0.6, coast=2400, truncated=True), DAY)
        td1 = t.active_tuning().td_s
        assert td1 > 0
        # a later, shorter lower bound (old one aged out) must not lower Td
        t._heatups = [_heatup(2 * DAY, 0.6, coast=300, truncated=True, td=td1)]
        t._last_change_ts = 0
        t._maybe_update(3 * DAY, "test")
        assert t.active_tuning().td_s >= td1

    def test_lower_bounds_below_complete_median_are_ignored(self):
        t = _tuner()
        t._heatups = [_heatup(DAY, 0.6, coast=600, truncated=True),
                      _heatup(2 * DAY, 0.6, coast=1200)]
        assert t._model()["coast_s"] == 1200
        assert not t._model()["coast_is_lower_bound"]

    def test_lower_bounds_above_complete_median_count(self):
        t = _tuner()
        t._heatups = [_heatup(DAY, 0.6, coast=2400, truncated=True),
                      _heatup(2 * DAY, 0.6, coast=2400, truncated=True),
                      _heatup(3 * DAY, 0.6, coast=600)]
        assert t._model()["coast_s"] == 2400

    def test_cut_short_in_dead_time_gives_no_dead_time(self):
        s, _ = _synthetic_heatup(theta_min=40.0, rate=2.4, y0=18.0)
        cut = [x for x in s if x[0] <= 46 * 60]
        m = A.analyse_heatup(cut, start_ts=0.0, setpoint_c=20.0, cfg=CFG,
                             cross_ts=None, tuning=A.Tuning(0.6, 0.002), truncated=True)
        assert m is None or m.theta_s is None

    def test_rollback_on_worse_overshoot(self):
        t = _tuner(deriv=False)
        t._on_heatup(_heatup(DAY, 0.2), DAY)
        prev = _force_more_heat(t, 2 * DAY)          # Kp up, being judged
        assert t.active_tuning().kp > prev.kp
        t._on_heatup(_heatup(3 * DAY, 0.9), 3 * DAY)  # needs two episodes
        reason = t._on_heatup(_heatup(4 * DAY, 0.9), 4 * DAY)
        assert reason.startswith("rolled back")
        assert t.active_tuning() == prev
        assert t.status == A.STATUS_TUNING              # no freeze
        assert t._limits("kp") == _expected_limits(t, "kp")   # nothing blocked

    def test_after_rollback_it_tries_again_on_fresh_evidence(self):
        t = _tuner(deriv=False)
        prev = _force_more_heat(t, DAY)
        t._rollback(DAY + 3600, "test")
        assert t.active_tuning() == prev
        assert t._votes == dict.fromkeys(t._votes, 0)   # old evidence cleared
        assert t._maybe_update(DAY + 13 * 3600, "test") is None   # no new evidence
        t._trim["kp"] = 1.4                              # fresh evidence arrives
        assert t._maybe_update(DAY + 2 * 3600, "test") is None    # normal 12 h rate limit
        assert t._maybe_update(DAY + 14 * 3600, "test") is not None
        assert t.active_tuning().kp > prev.kp            # same move, tried again
        assert t._pending is not None                    # and on trial again

    def test_gentle_moves_allowed_soon_after_rollback(self):
        t = _tuner(deriv=False)
        _force_more_heat(t, DAY)
        t._rollback(DAY + 3600, "test")
        before = t.active_tuning()
        t._trim["ki"] = 0.5
        assert t._maybe_update(DAY + 3600 + 4 * 3600, "test") is not None
        assert t.active_tuning().ki < before.ki

    def test_rollback_on_new_oscillation(self):
        t = _tuner(deriv=False)
        prev = _force_more_heat(t, DAY)
        reason = t._on_hold(_hold(DAY + 6 * 3600, osc=True, amp=0.2, period=3600, half=4),
                            DAY + 6 * 3600)
        assert reason.startswith("rolled back")
        assert t.active_tuning() == prev

    def test_gentle_change_is_never_rolled_back(self):
        """More braking / lower Ki cannot cause a sunny heat-up's overshoot."""
        t = _tuner()
        t._on_heatup(_heatup(DAY, 0.3), DAY)          # Td up, Ki down
        changed = t.active_tuning()
        assert changed.td_s > 0 and t._pending is None
        reason = t._on_heatup(_heatup(2 * DAY, 1.2, td=changed.td_s), 2 * DAY)
        assert not (reason or "").startswith("rolled back")
        assert t.active_tuning().td_s >= changed.td_s
        assert t.summary()["rollbacks"] == 0

    def test_emergency_still_works_after_rollback(self):
        t = _tuner(kp=1.0, ki=0.003, deriv=False)
        _force_more_heat(t, DAY)
        t._on_hold(_hold(DAY + 6 * 3600, osc=True, amp=0.2, period=3600, half=4), DAY + 6 * 3600)
        assert t.summary()["rollbacks"] == 1
        before = t.active_tuning()
        now = DAY + 7 * 3600
        t._now = now
        reason = t._on_hold(_hold(now, osc=True, amp=0.6, period=3600, half=5), now)
        assert reason is not None
        a = t.active_tuning()
        assert a.kp < before.kp and a.ki < before.ki

    def test_reset_and_baseline_change_after_rollback(self):
        t = _tuner(deriv=False)
        _force_more_heat(t, DAY)
        t._rollback(DAY + 3600, "test")
        t.reset()
        assert t.active_tuning() == A.Tuning(0.6, 0.002, 0.0)
        _force_more_heat(t, 3 * DAY)
        t._rollback(3 * DAY + 3600, "test")
        t.set_baseline(A.Tuning(0.4, 0.001, 0.0), False)
        assert t.active_tuning() == A.Tuning(0.4, 0.001, 0.0)

    def test_emergency_never_reduces_braking(self):
        t = _tuner(td=1800.0)
        t._trim["td_s"] = 0.5                         # target well below current Td
        t._heatups = [_heatup(DAY, 0.1, coast=1800)]
        t._now = DAY
        reason = t._on_hold(_hold(DAY, osc=True, amp=0.6, period=3600, half=5), DAY)
        assert reason is not None
        assert t.active_tuning().td_s >= 1800.0

    def test_cut_short_stall_does_not_relax_braking(self):
        t = _tuner(td=1200.0)
        for k in range(1, 5):
            t._on_heatup(_heatup(k * DAY, -0.3, td=1200.0, truncated=True), k * DAY)
        assert t._trim["td_s"] == 1.0

    def test_manual_brake_stall_does_not_raise_kp(self):
        t = _tuner(td=1200.0, deriv=False)
        for k in range(1, 6):
            t._on_heatup(_heatup(k * DAY, -0.4, td=1200.0), k * DAY)
        assert t._trim["kp"] == 1.0
        assert t.active_tuning().kp == 0.6

    def test_underpowered_room_does_not_raise_kp(self):
        """Never reached the target with demand still positive (cold day)."""
        t = _tuner(deriv=False)
        for k in range(1, 6):
            t._on_heatup(_heatup(k * DAY, -0.8, coast=None, crossed=False), k * DAY)
        assert t._trim["kp"] == 1.0

    @pytest.mark.parametrize("kp,ki", [(0.0, 0.002), (5.0, 0.002), (0.6, 0.02), (0.6, 0.0)])
    def test_out_of_range_baseline_starts_exactly_there(self, kp, ki):
        t = _tuner(kp=kp, ki=ki)
        assert t.active_tuning() == A.Tuning(kp, ki, 0.0)

    def test_old_stored_freeze_and_blocks_are_ignored(self):
        """State saved by 1.5.0 (with a freeze and a block) loads cleanly."""
        t = _tuner(deriv=False)
        d = t.as_dict()
        d["freeze_until"] = 9e12
        d["blocked"] = {"kp": [0.6, 9e12]}
        t2 = _tuner(deriv=False, stored=d)
        assert t2.status != "frozen"
        assert t2._limits("kp") == _expected_limits(t2, "kp")

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
        _force_more_heat(t, 2 * DAY)
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

    def test_zero_dead_time_in_storage_is_ignored(self):
        t = _tuner()
        t._on_heatup(_heatup(DAY, 0.6), DAY)
        d = t.as_dict()
        d["heatups"][0]["theta_s"] = 0.0
        t2 = _tuner(stored=d)
        t2.summary()
        t2._targets()

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

    def test_last_event_survives_restart(self):
        t = _tuner()
        t._on_heatup(_heatup(DAY, 0.6), DAY)
        assert _tuner(stored=t.as_dict()).last_event == t.last_event

    def test_nothing_learned_still_waiting_after_restart(self):
        # Older stored state has no last_event; nothing learned yet.
        d = _tuner().as_dict()
        del d["last_event"]
        assert _tuner(stored=d).last_event == "waiting for a heat-up"
        # An episode in progress is not persisted, so its event is stale.
        d["last_event"] = "heat-up started"
        assert _tuner(stored=d).last_event == "waiting for a heat-up"


# ---------------------------------------------------------------------------
# 3. Closed loop
# ---------------------------------------------------------------------------

LIVING = dict(trv_offset_c=-9.9, tau_room_h=50, t_out_c=12, trv_beta_c=6, tado_kp=0.5,
              tado_ti_min=60, heat_rate_c_per_h=2.5, tau_rad_min=12,
              water_delay_min=3, mix_lag_min=3)
SCHED = schedule_from([(0, 18.0), (6, 20.0), (8, 5.0), (17, 20.0), (22.5, 18.0)])


def _closed_loop(plant_kw, kp, ki, days, sched=SCHED, td=0.0, deriv=True, cfg=None, tune=True):
    c = P.RegulationConfig()
    c.tuning = P.CorrectionTuning(kp=kp, ki=ki, td_s=td)
    c.gain_fine_threshold_c = 1.0
    tuner = _tuner(kp=kp, ki=ki, td=td, deriv=deriv, cfg=cfg) if tune else None
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
        _, fixed = _closed_loop(LIVING, 0.6, 0.002, days=7, tune=False)
        before = _evening_overshoot(fixed, 6)       # same day, no tuner
        after = _evening_overshoot(tr, 6)
        assert before > 0.35
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

    @pytest.mark.parametrize("period", [30.0, 300.0])
    def test_coarse_sensor_still_calms_oscillation(self, period):
        """0.1 °C sensor whose HA timestamp only changes with the value."""
        plant = dict(LIVING, tado_ti_min=2000, tado_kp=1.5, water_delay_min=8,
                     mix_lag_min=6, sensor_resolution_c=0.1, sensor_period_s=period)
        flat = schedule_from([(0, 20.0)])
        tuner, tr = _closed_loop(plant, 1.2, 0.003, days=7, sched=flat)
        idx = tr.window(5 * DAY, 7 * DAY)
        e = [tr.sp[i] - tr.y_true[i] for i in idx]
        assert max(e) - min(e) < 0.7
        assert tuner.summary()["holds_analysed"] > 5

    def test_coarse_sensor_still_learns_braking(self):
        plant = dict(LIVING, sensor_resolution_c=0.1, sensor_period_s=300.0)
        tuner, tr = _closed_loop(plant, 0.6, 0.002, days=7)
        assert tuner.active_tuning().td_s >= 600
        assert _evening_overshoot(tr, 6) < 0.35

    def test_does_nothing_without_episodes(self):
        """A room held at a steady temperature with no swings is left alone."""
        flat = schedule_from([(0, 20.0)])
        tuner, tr = _closed_loop(dict(LIVING, tado_ti_min=200), 0.6, 0.0003, days=3,
                                 sched=flat)
        a = tuner.active_tuning()
        assert a.kp == 0.6
        assert a.td_s == 0.0


def test_production_settings_with_sunny_days_stay_sane():
    """180 s / 0.3 °C sends, random sunny afternoons: no runaway, no locked
    brake, overshoot on ordinary days ends near target."""
    rng = random.Random(8)
    sunny = [rng.random() < 0.4 for _ in range(22)]

    def sun(t):
        d, h = int(t // DAY), (t / 3600) % 24
        return 0.4 if sunny[d] and 13 <= h < 18.5 else 0.0

    c = P.RegulationConfig()
    c.tuning = P.CorrectionTuning(kp=0.6, ki=0.002)
    c.gain_fine_threshold_c = 1.0
    tuner = _tuner()
    sim = RoomSim(PlantParams(**LIVING, extra_gain=sun), R, c,
                  ProxySettings(min_command_interval_s=180, min_change_threshold_c=0.3),
                  y0=19.0, tuner=tuner, autotune_module=A, seed=8)
    tr = sim.run(21 * 24, SCHED)
    _within_bounds(tuner)
    s = tuner.summary()
    assert s["rollbacks"] <= 1
    assert tuner.active_tuning().td_s >= 600            # braking learned, not locked off
    quiet = [d for d in range(14, 21) if not sunny[d]]
    assert quiet
    assert max(_evening_overshoot(tr, d) for d in quiet) < 0.3
