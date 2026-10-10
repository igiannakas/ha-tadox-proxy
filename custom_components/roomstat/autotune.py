"""Background PI(D) auto-tuning for Roomstat (HA-independent).

Design summary (more in CLAUDE.md, "Auto-tune")
-----------------------------------------------
The proxy cannot move the valve.  It sets a target for the TRV, whose own
controller (with its own integrator) moves the valve.  Because the
feedforward cancels the TRV reading, the TRV acts on

    demand = (1 + kp) * error + integral + derivative_brake

so the plant seen by the tuner is *demand -> TRV controller -> valve ->
radiator -> room air -> room sensor*.  Measured on real rooms it is:

* an integrating process with dead time (rooms lose heat over tens of
  hours, so SIMC's integrating rules apply: Ti = 4 (tau_c + theta));
* strongly asymmetric: heating runs at 1-3 °C/h, passive cooling at
  0.3-0.5 °C/h, so overshoot is far more costly than undershoot;
* followed by a long "coast": the TRV keeps delivering heat for 30-60 min
  after its demand turns negative.

The tuner therefore learns from naturally occurring episodes, never from
injected test signals:

* **Heat-ups** (setpoint step up) give the dead time theta (tangent
  method), the heating rate, the coast time and the overshoot.
* **Holds** (steady setpoint after a heat-up) give the mean offset and
  any oscillation.

and moves Kp, Ki and the derivative brake time Td in small, bounded,
rate-limited steps, with SIMC providing the ceilings and a watchdog that
rolls back any change that made things worse.  It only ever adjusts the
values the regulator uses; it never writes config options.
"""
from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any

from .parameters import AutotuneConfig

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Robust room-temperature slope
# ---------------------------------------------------------------------------

def theil_sen(points: list[tuple[float, float]], min_dt: float = 60.0) -> float | None:
    """Median of pairwise slopes (y units per t unit), or None."""
    slopes = []
    n = len(points)
    for i in range(n):
        ti, yi = points[i]
        for j in range(i + 1, n):
            tj, yj = points[j]
            if tj - ti >= min_dt:
                slopes.append((yj - yi) / (tj - ti))
    if not slopes:
        return None
    slopes.sort()
    m = len(slopes)
    return slopes[m // 2] if m % 2 else 0.5 * (slopes[m // 2 - 1] + slopes[m // 2])


def _median(values: list[float]) -> float | None:
    vals = sorted(v for v in values if v is not None and math.isfinite(v))
    if not vals:
        return None
    m = len(vals)
    return vals[m // 2] if m % 2 else 0.5 * (vals[m // 2 - 1] + vals[m // 2])


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _finite(*values: Any) -> bool:
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)


class SpikeFilter:
    """Rejects single wild sensor reports, per *report*, not per cycle.

    The regulation cycle re-reads the room sensor every minute, so one bad
    report can be seen on several cycles (until the sensor next reports a
    different value).  A new report that jumps more than ``spike_c`` from the
    last accepted value is held back and the last accepted value is used
    instead; it is accepted once the next report confirms it (lands close to
    it, or continues in the same direction), or after ``max_hold_s``.  A
    genuine fast change is therefore delayed by one report at most.

    ``report_ts`` is the sensor's own report time.  Home Assistant only
    changes it when the value changes, which is exactly what "a new report"
    means here.  Without it, every changed value counts as a new report.
    """

    def __init__(self, spike_c: float = 1.0, max_hold_s: float = 900.0) -> None:
        self.spike_c = spike_c
        self.max_hold_s = max_hold_s
        self._accepted: float | None = None
        self._pending: float | None = None
        self._pending_since = 0.0
        self._last_key: Any = None

    def reset(self) -> None:
        self._accepted = self._pending = self._last_key = None

    def update(self, ts: float, value: float | None, report_ts: float | None = None) -> float | None:
        if value is None or not _finite(value, ts):
            return self._accepted
        key = report_ts if _finite(report_ts) else value
        if key == self._last_key:                 # same report, re-read
            if self._pending is None:
                return value
            if ts - self._pending_since >= self.max_hold_s:
                # The held report has stood unchallenged for long enough:
                # a steady room produces no new report, so accept it.
                self._accepted, self._pending = self._pending, None
            return self._accepted
        self._last_key = key
        acc = self._accepted
        if acc is None or abs(value - acc) <= self.spike_c:
            self._accepted, self._pending = value, None
            return value
        pend = self._pending
        if pend is not None and (
            abs(value - pend) <= self.spike_c                  # confirmed
            or (value - acc) * (pend - acc) > 0 and abs(value - acc) >= abs(pend - acc)  # trend
            or ts - self._pending_since >= self.max_hold_s     # never freeze
        ):
            self._accepted, self._pending = value, None
            return value
        self._pending, self._pending_since = value, ts if pend is None else self._pending_since
        return acc


class SlopeEstimator:
    """Theil-Sen slope of the room temperature over a sliding window.

    One sample per regulation cycle.  A repeated (unchanged) reading is real
    information – "no change at this resolution" – and pulls the slope
    towards zero, which for a brake is the safe direction.  Wild reports are
    removed by a :class:`SpikeFilter` first, and the median of pairwise
    slopes then ignores up to ~29 % remaining outliers.  Returns °C/s, or
    None while the window is too short or after a data gap.
    """

    def __init__(self, window_s: float = 900.0, max_gap_s: float = 300.0,
                 min_samples: int = 6) -> None:
        self.window_s = window_s
        self.max_gap_s = max_gap_s
        self.min_samples = min_samples
        self._buf: deque[tuple[float, float]] = deque()
        self._spikes = SpikeFilter()

    def reset(self) -> None:
        self._buf.clear()
        self._spikes.reset()

    def add(self, ts: float, value: float | None, value_ts: float | None = None) -> None:
        if not _finite(ts):
            return
        value = self._spikes.update(ts, value, value_ts)
        if value is None:
            return
        if self._buf and (ts - self._buf[-1][0] > self.max_gap_s or ts < self._buf[-1][0]):
            self._buf.clear()
        self._buf.append((ts, value))
        while self._buf and ts - self._buf[0][0] > self.window_s:
            self._buf.popleft()

    def slope_c_per_s(self) -> float | None:
        buf = self._buf
        if len(buf) < self.min_samples or buf[-1][0] - buf[0][0] < self.window_s / 2:
            return None
        return theil_sen(list(buf))


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Tuning:
    """The three values the tuner hands to the regulator."""

    kp: float
    ki: float
    td_s: float = 0.0


@dataclass
class AutotuneSample:
    """What the regulation cycle tells the tuner, once per cycle."""

    ts: float
    setpoint_c: float | None
    room_temp_c: float | None          # None when the sensor is missing/stale
    tado_internal_c: float | None
    tado_setpoint_c: float | None      # the command the TRV currently holds
    eligible: bool = True              # False: window/summer/off/degraded...
    command_saturated: bool = False
    room_temp_ts: float | None = None  # sensor report time (dedupes re-reads)

    @property
    def demand_c(self) -> float | None:
        """What the TRV's own controller acts on: its setpoint - its reading."""
        if not _finite(self.tado_setpoint_c, self.tado_internal_c):
            return None
        return self.tado_setpoint_c - self.tado_internal_c


@dataclass
class HeatupMetrics:
    """Result of one completed heat-up episode."""

    ts: float                          # completion time
    start_ts: float
    setpoint_c: float
    step_c: float                      # setpoint - room at start
    overshoot_c: float | None          # peak - setpoint (negative = sag), None = unknown
    theta_s: float | None = None       # apparent dead time (tangent method)
    rate_c_per_h: float | None = None  # max heating rate
    demand0_c: float | None = None     # peak demand early in the episode
    coast_s: float | None = None       # heat still arriving after demand <= 0
    coast_rise_c: float | None = None
    reach_s: float | None = None
    kp: float = 0.0
    ki: float = 0.0
    td_s: float = 0.0
    truncated: bool = False            # ended early (schedule moved on)
    crossed: bool = False              # TRV demand went <= 0 (we told it to stop)

    def as_dict(self) -> dict[str, Any]:
        return {k: (round(v, 6) if isinstance(v, float) else v)
                for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, d: Any) -> HeatupMetrics | None:
        try:
            m = cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
        except (TypeError, KeyError):
            return None
        if not _finite(m.ts, m.start_ts, m.setpoint_c, m.step_c):
            return None
        if m.overshoot_c is not None and not _finite(m.overshoot_c):
            return None
        for name in ("theta_s", "rate_c_per_h", "demand0_c", "coast_s", "coast_rise_c", "reach_s"):
            if getattr(m, name) is not None and not _finite(getattr(m, name)):
                setattr(m, name, None)
        # Values that feed divisions must be strictly positive.
        if m.theta_s is not None and m.theta_s <= 0:
            m.theta_s = None
        if m.rate_c_per_h is not None and m.rate_c_per_h <= 0:
            m.rate_c_per_h = None
        if m.coast_s is not None and m.coast_s < 0:
            m.coast_s = None
        if not _finite(m.kp, m.ki, m.td_s):
            return None
        m.truncated, m.crossed = bool(m.truncated), bool(m.crossed)
        return m


@dataclass
class HoldMetrics:
    """Result of one hold window."""

    ts: float
    mean_error_c: float
    sd_c: float
    half_cycles: int
    period_s: float | None
    amplitude_c: float
    heating_fraction: float
    oscillating: bool
    kp: float = 0.0
    ki: float = 0.0
    td_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {k: (round(v, 6) if isinstance(v, float) else v)
                for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, d: Any) -> HoldMetrics | None:
        try:
            m = cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
        except (TypeError, KeyError):
            return None
        if not _finite(m.ts, m.mean_error_c, m.sd_c, m.amplitude_c, m.heating_fraction):
            return None
        if not isinstance(m.half_cycles, int) or m.half_cycles < 0:
            return None
        if m.period_s is not None and (not _finite(m.period_s) or m.period_s <= 0):
            m.period_s = None
        m.oscillating = bool(m.oscillating)
        return m


# ---------------------------------------------------------------------------
# Episode analysis (pure functions, unit-tested directly)
# ---------------------------------------------------------------------------

def _median_filter(values: list[float], k: int = 3) -> list[float]:
    """Running median (window k) – removes single-sample glitches."""
    if len(values) < k:
        return list(values)
    h = k // 2
    out = list(values)
    for i in range(h, len(values) - h):
        out[i] = sorted(values[i - h:i + h + 1])[h]
    return out


def analyse_heatup(
    samples: list[tuple[float, float, float | None]],
    *,
    start_ts: float,
    setpoint_c: float,
    cfg: AutotuneConfig,
    cross_ts: float | None,
    tuning: Tuning,
    truncated: bool = False,
) -> HeatupMetrics | None:
    """Extract dead time, heating rate, coast and overshoot from a heat-up.

    ``samples`` are (ts, room_temp, demand), one per regulation cycle, with
    wild reports already removed by the tuner's :class:`SpikeFilter`.
    ``cross_ts`` is when the TRV demand first fell to <= 0 after having been
    positive, or None if it never did.

    ``truncated`` means the schedule moved on before the room settled.  The
    peak seen so far is then only a lower bound, so the overshoot is kept
    only when it already shows too much (evidence for *more* braking is
    still valid), and the coast is reported as a lower bound (the tuner only
    ever lets lower bounds *raise* the braking time).  A cut-off episode can
    therefore never argue for *less* braking.
    """
    if len(samples) < 10:
        return None
    ts = [s[0] for s in samples]
    # 5-point running median on top of the spike filter.
    ys = _median_filter([s[1] for s in samples], 5)
    cross_index = None
    if cross_ts is not None:
        cross_index = next((i for i, t in enumerate(ts) if t >= cross_ts), None)
    y0 = _median(ys[:4])
    if y0 is None:
        return None
    end_ts = ts[-1]
    peak_i = max(range(len(ys)), key=lambda i: ys[i])
    peak = ys[peak_i]

    # Tangent method on the rising part: steepest 10-min Theil-Sen line.
    win = 600.0
    best = None  # (slope, t_center, y_center)
    rise_end = peak_i if cross_index is None else max(peak_i, cross_index)
    j0 = 0
    for j in range(rise_end + 1):
        while ts[j] - ts[j0] > win:
            j0 += 1
        if ts[j] - ts[j0] < win * 0.8:
            continue
        pts = list(zip(ts[j0:j + 1], ys[j0:j + 1]))
        sl = theil_sen(pts)
        if sl is None:
            continue
        if best is None or sl > best[0]:
            tc = 0.5 * (ts[j0] + ts[j])
            yc = _median([y - sl * (t - tc) for t, y in pts])
            best = (sl, tc, yc)

    step = setpoint_c - y0
    theta = rate = None
    rising_done = True
    if truncated and cross_index is None and best is not None:
        # Cut short before the TRV was told to stop: the steepest rise may
        # still be ahead.  Only trust the tangent if the room had clearly
        # passed it (slope at the end well below the maximum).
        tail = [(t, y) for t, y in zip(ts, ys) if end_ts - t <= win]
        tail_slope = theil_sen(tail) if len(tail) >= 5 else None
        rising_done = (
            best[1] <= end_ts - 900.0
            and tail_slope is not None and tail_slope < 0.7 * best[0]
        )
    if best is not None and best[0] > 0 and step >= cfg.rate_min_step_c and rising_done:
        sl, tc, yc = best
        rate_h = sl * 3600.0
        if cfg.rate_min_c_per_h <= rate_h <= cfg.rate_max_c_per_h:
            rate = rate_h
            th = (tc - start_ts) - (yc - y0) / sl
            if step >= cfg.theta_min_step_c and cfg.theta_min_s <= th <= cfg.theta_max_s:
                theta = th

    overshoot: float | None = peak - setpoint_c
    if truncated and overshoot <= cfg.overshoot_target_c:
        overshoot = None

    # Coast: how much the room still rises after the TRV demand went <= 0.
    coast_s = coast_rise = None
    if cross_index is not None and 0 < cross_index < len(samples) - 1:
        t_c = ts[cross_index]
        pre = [(t, y) for t, y in zip(ts, ys) if t_c - win <= t <= t_c]
        s_cross = theil_sen(pre) if len(pre) >= 5 else None
        y_cross = _median(ys[max(0, cross_index - 1):cross_index + 2])
        post_peak = max(ys[cross_index:])
        if y_cross is not None:
            coast_rise = max(0.0, post_peak - y_cross)
            if s_cross is not None and s_cross * 3600.0 >= cfg.rate_min_c_per_h:
                coast_s = _clamp(coast_rise / s_cross, 0.0, 3 * cfg.td_max_s)

    demand0 = None
    early = [s[2] for s in samples if s[0] - start_ts <= 900 and s[2] is not None]
    if early:
        demand0 = max(early)

    reach = next((t for t, y in zip(ts, ys) if y >= setpoint_c - 0.1), None)
    return HeatupMetrics(
        ts=end_ts, start_ts=start_ts, setpoint_c=setpoint_c,
        step_c=step, overshoot_c=overshoot,
        theta_s=theta, rate_c_per_h=rate, demand0_c=demand0,
        coast_s=coast_s, coast_rise_c=coast_rise,
        reach_s=(reach - start_ts) if reach is not None else None,
        kp=tuning.kp, ki=tuning.ki, td_s=tuning.td_s, truncated=truncated,
        crossed=cross_ts is not None,
    )


def analyse_hold(
    samples: list[tuple[float, float, float | None]],
    *,
    cfg: AutotuneConfig,
    tuning: Tuning,
) -> HoldMetrics | None:
    """Mean offset and oscillation while holding a steady setpoint.

    ``samples`` are (ts, error, demand) covering up to ``hold_buffer_s``.
    The mean offset and heating fraction use the most recent
    ``hold_window_s``; oscillation is judged over the whole buffer, because
    integral-driven cycles in a slow room can take many hours.  A swing is
    counted with hysteresis: the filtered error must go from above +A to
    below -A (around its mean) and back, so sensor noise cannot fake one.
    """
    if len(samples) < 30:
        return None
    ts = [s[0] for s in samples]
    es = _median_filter(_median_filter([s[1] for s in samples], 5), 5)
    recent = [i for i, t in enumerate(ts) if ts[-1] - t <= cfg.hold_window_s]
    if len(recent) < 30:
        return None
    mean_recent = sum(es[i] for i in recent) / len(recent)
    mean_all = sum(es) / len(es)
    sd = math.sqrt(sum((e - mean_all) ** 2 for e in es) / len(es))
    demands = [samples[i][2] for i in recent if samples[i][2] is not None]
    heating = sum(1 for d in demands if d > 0) / len(demands) if demands else 0.0
    all_d = [s[2] for s in samples if s[2] is not None]
    heating_all = sum(1 for d in all_d if d > 0) / len(all_d) if all_d else 0.0

    amp = cfg.osc_amplitude_c
    side = 0
    crossings: list[float] = []
    for t, e in zip(ts, es):
        dev = e - mean_all
        if dev > amp and side <= 0:
            if side < 0:
                crossings.append(t)
            side = 1
        elif dev < -amp and side >= 0:
            if side > 0:
                crossings.append(t)
            side = -1
    half_cycles = len(crossings)
    period = None
    if half_cycles >= 2:
        period = 2.0 * (crossings[-1] - crossings[0]) / (half_cycles - 1)
    swing = 0.5 * (max(es) - min(es))
    return HoldMetrics(
        ts=ts[-1], mean_error_c=mean_recent, sd_c=sd, half_cycles=half_cycles,
        period_s=period, amplitude_c=swing, heating_fraction=heating,
        # Any heating at all in the buffer makes a swing a control problem: a
        # sawtooth of short bursts and long passive cool-downs is still a
        # limit cycle.  Pure passive drift (no heating) is not.
        # Cycles slower than osc_max_period_s are weather or daily routine
        # (sun, occupancy), not the controller.
        oscillating=(half_cycles >= cfg.osc_min_half_cycles and heating_all > 0.02
                     and (period is None or period <= cfg.osc_max_period_s)),
        kp=tuning.kp, ki=tuning.ki, td_s=tuning.td_s,
    )


# ---------------------------------------------------------------------------
# Episode trackers
# ---------------------------------------------------------------------------

_RUNNING, _DONE, _ABORT, _TRUNCATED = "running", "done", "abort", "truncated"


class _HeatupTracker:
    """Collects one heat-up: from a setpoint step until the room settles."""

    def __init__(self, start_ts: float, setpoint_c: float, cfg: AutotuneConfig) -> None:
        self.start_ts = start_ts
        self.setpoint_c = setpoint_c
        self.cfg = cfg
        self.samples: list[tuple[float, float, float | None]] = []
        self.demand_was_positive = False
        self.cross_ts: float | None = None
        self.peak_c = -math.inf
        self.peak_ts = start_ts
        self.abort_reason = ""
        self._last_cycle_ts = start_ts

    def _stop(self, reason: str, ts: float) -> str:
        """End early.  Keep what is valid: the rising phase (dead time, rate)
        once it ran long enough, and the coast once the TRV had been told to
        stop for a while.  Short schedule slots (e.g. 06:00-08:00 comfort)
        are the norm, so discarding them would leave nothing to learn from."""
        self.abort_reason = reason
        if (self.cross_ts is not None
                and ts - self.cross_ts >= self.cfg.truncated_min_after_cross_s):
            return _TRUNCATED
        if ts - self.start_ts >= self.cfg.truncated_min_duration_s:
            return _TRUNCATED
        return _ABORT

    # -- persistence (a heat-up in progress survives a reload or restart) --
    _MAX_SAMPLES = 5000   # 5 h at one cycle a minute is 300

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_ts": self.start_ts,
            "setpoint_c": self.setpoint_c,
            # [seconds since start, room °C, TRV demand °C]
            "samples": [[round(t - self.start_ts, 1), round(y, 3),
                         None if d is None else round(d, 3)]
                        for t, y, d in self.samples],
            "demand_was_positive": self.demand_was_positive,
            "cross_ts": self.cross_ts,
            "peak_c": self.peak_c if _finite(self.peak_c) else None,
            "peak_ts": self.peak_ts,
            "last_cycle_ts": self._last_cycle_ts,
        }

    @classmethod
    def from_dict(cls, d: Any, cfg: AutotuneConfig) -> _HeatupTracker | None:
        """Rebuild a stored heat-up, or None if anything looks wrong."""
        if not isinstance(d, dict):
            return None
        start, sp, last = d.get("start_ts"), d.get("setpoint_c"), d.get("last_cycle_ts")
        if not _finite(start, sp, last) or last < start:
            return None
        raw = d.get("samples")
        if not isinstance(raw, list) or len(raw) > cls._MAX_SAMPLES:
            return None
        samples: list[tuple[float, float, float | None]] = []
        prev_off = 0.0
        for x in raw:
            if not isinstance(x, list) or len(x) != 3:
                return None
            off, y, dem = x
            if not _finite(off, y) or off < prev_off or start + off > last + 1.0:
                return None
            if dem is not None and not _finite(dem):
                return None
            samples.append((start + off, float(y), None if dem is None else float(dem)))
            prev_off = off
        cross = d.get("cross_ts")
        if cross is not None and not (_finite(cross) and start <= cross <= last):
            return None
        tr = cls(float(start), float(sp), cfg)
        tr.samples = samples
        tr.demand_was_positive = d.get("demand_was_positive") is True
        tr.cross_ts = None if cross is None else float(cross)
        peak, peak_ts = d.get("peak_c"), d.get("peak_ts")
        if _finite(peak, peak_ts):
            tr.peak_c, tr.peak_ts = float(peak), float(peak_ts)
        tr._last_cycle_ts = float(last)
        return tr

    def add(self, s: AutotuneSample) -> str:
        if not s.eligible:
            return self._stop("not_eligible", s.ts)
        if s.setpoint_c is None or abs(s.setpoint_c - self.setpoint_c) > 0.05:
            return self._stop("setpoint_changed", s.ts)
        gap = s.ts - self._last_cycle_ts
        if gap > self.cfg.max_gap_s or gap < 0:
            self.abort_reason = "data_gap"
            return _ABORT
        self._last_cycle_ts = s.ts
        d = s.demand_c
        if d is not None:
            if d > 0.2:
                self.demand_was_positive = True
            elif d <= 0 and self.demand_was_positive and self.cross_ts is None:
                self.cross_ts = s.ts
        if s.room_temp_c is not None:
            self.samples.append((s.ts, s.room_temp_c, d))
            tail = sorted(x[1] for x in self.samples[-5:])
            recent = tail[len(tail) // 2]
            if recent > self.peak_c:
                self.peak_c, self.peak_ts = recent, s.ts
        elapsed = s.ts - self.start_ts
        if self.cross_ts is not None and s.ts - self.peak_ts >= self.cfg.heatup_settle_s:
            return _DONE
        if elapsed >= self.cfg.heatup_max_s:
            return _DONE
        return _RUNNING


class _HoldTracker:
    """Rolling buffer of a steady-setpoint period, evaluated every window."""

    def __init__(self, start_ts: float, setpoint_c: float, cfg: AutotuneConfig) -> None:
        self.start_ts = start_ts
        self.setpoint_c = setpoint_c
        self.cfg = cfg
        self.samples: deque[tuple[float, float, float | None]] = deque()
        self.last_eval_ts = start_ts
        self._last_cycle_ts = start_ts

    def clear(self, ts: float) -> None:
        """Start collecting fresh evidence (after a change or a verdict)."""
        self.samples.clear()
        self.last_eval_ts = ts

    def add(self, s: AutotuneSample) -> str:
        if not s.eligible or s.setpoint_c is None or abs(s.setpoint_c - self.setpoint_c) > 0.05:
            return _ABORT
        if not 0 <= s.ts - self._last_cycle_ts <= self.cfg.max_gap_s:
            return _ABORT
        self._last_cycle_ts = s.ts
        if s.room_temp_c is not None:
            self.samples.append((s.ts, self.setpoint_c - s.room_temp_c, s.demand_c))
        while self.samples and s.ts - self.samples[0][0] > self.cfg.hold_buffer_s:
            self.samples.popleft()
        if s.ts - self.last_eval_ts >= self.cfg.hold_window_s:
            self.last_eval_ts = s.ts
            return _DONE
        return _RUNNING


# ---------------------------------------------------------------------------
# The tuner
# ---------------------------------------------------------------------------

STATUS_DISABLED = "disabled"   # reported by the integration when switched off
STATUS_LEARNING = "learning"
STATUS_TUNING = "tuning"
STATUSES = [STATUS_DISABLED, STATUS_LEARNING, STATUS_TUNING]

PHASE_IDLE = "idle"
PHASE_HEATUP = "heat_up"
PHASE_HOLD = "hold"

_PARAMS = ("kp", "ki", "td_s")


def _tuning_from(d: Any) -> Tuning | None:
    if not isinstance(d, dict):
        return None
    kp, ki, td = d.get("kp"), d.get("ki"), d.get("td_s", 0.0)
    if not _finite(kp, ki, td):
        return None
    return Tuning(float(kp), float(ki), float(td))


def _tuning_dict(t: Tuning) -> dict[str, float]:
    return {"kp": round(t.kp, 6), "ki": round(t.ki, 8), "td_s": round(t.td_s, 1)}


class Autotuner:
    """Learns per-room Kp / Ki / Td from everyday heat-ups and holds.

    Safety properties (each is unit-tested):

    * every value stays inside absolute bounds *and* bounds relative to the
      user's configured values (the baseline);
    * each update moves a value by a bounded factor, and updates are rate
      limited (longer wait for "more heat" moves than for gentler ones);
    * "more heat" moves (higher Kp/Ki, less braking) need ``confirm_up``
      agreeing episodes; gentler moves need one;
    * after every "more heat" change the next heat-ups are compared with
      the ones before it, and the change is rolled back if things got
      worse.  The tuner may try again later, but only on fresh evidence
      (the trims are re-anchored to the restored values) and only after
      the normal rate limit;
    * a large oscillation detunes immediately;
    * the derivative brake can only lower the command (regulation.py).
    """

    VERSION = 1

    def __init__(
        self,
        cfg: AutotuneConfig,
        baseline: Tuning,
        *,
        derivative_allowed: bool = True,
        stored: dict[str, Any] | None = None,
    ) -> None:
        self.cfg = cfg
        self.derivative_allowed = derivative_allowed
        self._baseline = baseline
        self._now = 0.0
        # Episode context.  A heat-up in progress and what the previous cycle
        # saw are persisted, so a reload (e.g. a preset temperature change)
        # neither loses the heat-up nor hides a setpoint step.  A restart is
        # then just a gap between two cycles and follows the usual rules
        # (max_gap_s).  A hold window and the spike filter start afresh.
        self._heatup: _HeatupTracker | None = None
        self._hold: _HoldTracker | None = None
        self._spikes = SpikeFilter()
        self._prev_sp: float | None = None
        self._prev_ts: float | None = None
        self._prev_eligible = True
        self._sp_since_ts: float | None = None
        self._last_heatup_end_ts = -math.inf
        self._reset_learning()
        if stored is not None:
            self._load(stored)

    # -- state -------------------------------------------------------------
    def _reset_learning(self) -> None:
        b = self._baseline
        self._params = self._bounded(Tuning(b.kp, b.ki, b.td_s))
        self._last_good = self._params
        self._trim = {"kp": 1.0, "ki": 1.0, "td_s": 1.0}
        self._votes = {"kp_up": 0, "ki_up": 0, "td_down": 0}
        self._heatups: list[HeatupMetrics] = []
        self._holds: list[HoldMetrics] = []
        self._last_change_ts = 0.0
        self._pending: dict[str, Any] | None = None
        self._counters = {"heatups": 0, "holds": 0, "updates": 0, "rollbacks": 0}
        self.last_update_reason = ""
        self.last_event = "waiting for a heat-up"

    def reset(self) -> None:
        """Forget everything learned and return to the configured values."""
        self._reset_learning()
        self._heatup = None
        self._hold = None
        self.last_event = "reset to configured values"

    def set_baseline(self, baseline: Tuning, derivative_allowed: bool) -> None:
        """Apply new configured values.  A changed baseline restarts learning."""
        self.derivative_allowed = derivative_allowed
        if baseline != self._baseline:
            self._baseline = baseline
            self.reset()
            self.last_event = "configured values changed – learning restarted"

    @property
    def baseline(self) -> Tuning:
        return self._baseline

    def restore(self, stored: Any) -> None:
        """Load state persisted by :meth:`as_dict` (e.g. after a restart)."""
        if stored is not None:
            self._load(stored)

    def active_tuning(self) -> Tuning:
        """The values the regulator should use right now."""
        p = self._params
        td = p.td_s if self.derivative_allowed else self._baseline.td_s
        return Tuning(p.kp, p.ki, td)

    @property
    def phase(self) -> str:
        if self._heatup is not None:
            return PHASE_HEATUP
        if self._hold is not None:
            return PHASE_HOLD
        return PHASE_IDLE

    def status_at(self, now: float) -> str:
        if self._counters["updates"] == 0 and self._model()["theta_s"] is None:
            return STATUS_LEARNING
        return STATUS_TUNING

    @property
    def status(self) -> str:
        return self.status_at(self._now)

    # -- bounds ------------------------------------------------------------
    def _limits(self, name: str) -> tuple[float, float]:
        c, b = self.cfg, self._baseline
        if name == "kp":
            lo = max(c.kp_min, b.kp * c.kp_rel_min)
            hi = min(c.kp_max, max(b.kp * c.kp_rel_max, c.kp_min))
            base = b.kp
        elif name == "ki":
            if b.ki <= 0:          # the user switched the integral off: respect it
                return 0.0, 0.0
            lo = max(c.ki_min, b.ki * c.ki_rel_min)
            hi = min(c.ki_max, max(b.ki * c.ki_rel_max, c.ki_min))
            base = b.ki
        else:
            lo, hi = 0.0, c.td_max_s
            base = b.td_s
        if lo > hi:
            lo = hi
        # Never move the user's own value by fiat: a configured value outside
        # the hard bounds widens them, so learning starts exactly there.
        lo, hi = min(lo, base), max(hi, base)
        return lo, hi

    def _bounded(self, t: Tuning) -> Tuning:
        vals = []
        for name in _PARAMS:
            v = getattr(t, name)
            if not _finite(v):
                v = getattr(self._baseline, name)
            lo, hi = self._limits(name)
            vals.append(_clamp(v, lo, hi))
        return Tuning(*vals)

    # -- model ---------------------------------------------------------------
    def _model(self) -> dict[str, float | None]:
        hu = self._heatups
        thetas = [m.theta_s for m in hu if m.theta_s is not None and m.theta_s > 0]
        # SIMC needs a dead time it can trust: one heat-up is not enough.
        theta = _median(thetas) if len(thetas) >= self.cfg.simc_min_episodes else None
        rate = _median([m.rate_c_per_h for m in hu if m.rate_c_per_h is not None])
        complete = [m.coast_s for m in hu if m.coast_s is not None and not m.truncated]
        bounds = [m.coast_s for m in hu if m.coast_s is not None and m.truncated]
        # Complete episodes give the coast.  Cut-short ones only give a lower
        # bound: used (as their maximum) until a complete one exists, and
        # after that only when it exceeds the complete median (a bound below
        # it says nothing new; one above it is real evidence of more coast).
        if complete:
            mid = _median(complete)
            coast = _median(complete + [b for b in bounds if b > mid])
        else:
            coast = max(bounds) if bounds else None
        tau_c = self.cfg.simc_lambda * theta if theta is not None else None
        return {"theta_s": theta, "rate_c_per_h": rate, "coast_s": coast,
                "coast_is_lower_bound": not complete and bool(bounds), "tau_c_s": tau_c}

    def _simc_ki(self) -> float | None:
        """SIMC integral gain for the identified dead time (before trims)."""
        m = self._model()
        if m["theta_s"] is None or m["theta_s"] <= 0:
            return None
        return (1.0 + self._params.kp) / (4.0 * (m["tau_c_s"] + m["theta_s"]))

    def _targets(self) -> Tuning:
        c, b, cur = self.cfg, self._baseline, self._params
        m = self._model()
        # Kp: SIMC's Kc needs the plant gain, which closed-loop heat-ups cannot
        # give (the TRV saturates the valve, its own integrator makes the gain
        # drift, and the demand itself scales with Kc).  So Kp stays at the
        # configured value and only moves on direct evidence: oscillation
        # (down) or a stalled approach without braking (up).
        kp_t = b.kp * self._trim["kp"]
        ki_t = b.ki * self._trim["ki"]
        if m["theta_s"] is not None and m["theta_s"] > 0 and b.ki > 0:
            span = m["tau_c_s"] + m["theta_s"]
            kc = 1.0 + kp_t
            ki_t = min(kc / (4.0 * span) * self._trim["ki"],
                       kc / (c.simc_ti_factor_min * span))
        td_t = cur.td_s
        if self.derivative_allowed and m["coast_s"] is not None:
            td_t = _clamp(m["coast_s"] * self._trim["td_s"], 0.0, c.td_max_s)
            if m["coast_is_lower_bound"]:
                td_t = max(td_t, cur.td_s)    # a lower bound may only raise Td
            known = [h.overshoot_c for h in self._heatups if h.overshoot_c is not None]
            if len(known) >= 2 and max(known[-2:]) <= c.overshoot_target_c:
                # Already on target: do not keep adding braking (it costs
                # heat-up speed); the trim may still relax it.
                td_t = min(td_t, cur.td_s)
        return Tuning(kp_t, ki_t, td_t)

    # -- observation -----------------------------------------------------------
    def observe(self, s: AutotuneSample) -> str | None:
        """Feed one regulation cycle.  Returns a reason when values changed."""
        if not _finite(s.ts):
            return None
        self._now = s.ts
        event: str | None = None
        sp = s.setpoint_c if _finite(s.setpoint_c) else None
        room = s.room_temp_c if _finite(s.room_temp_c) else None
        if room is not None:
            room = self._spikes.update(s.ts, room, s.room_temp_ts)
        s = AutotuneSample(s.ts, sp, room, s.tado_internal_c, s.tado_setpoint_c,
                           s.eligible and sp is not None, s.command_saturated,
                           s.room_temp_ts if _finite(s.room_temp_ts) else None)
        resumed = (
            self._prev_ts is not None
            and (not self._prev_eligible or s.ts - self._prev_ts > self.cfg.max_gap_s)
        )

        if self._heatup is not None:
            st = self._heatup.add(s)
            if st in (_DONE, _TRUNCATED):
                hu = self._heatup
                self._heatup = None
                self._last_heatup_end_ts = s.ts
                event = self._on_heatup(analyse_heatup(
                    hu.samples, start_ts=hu.start_ts, setpoint_c=hu.setpoint_c,
                    cfg=self.cfg, cross_ts=hu.cross_ts, tuning=self.active_tuning(),
                    truncated=st == _TRUNCATED,
                ), s.ts)
            elif st == _ABORT:
                self.last_event = f"heat-up discarded ({self._heatup.abort_reason})"
                self._heatup = None
        elif self._hold is not None:
            st = self._hold.add(s)
            if st == _DONE:
                hold = self._hold
                verdict = self._on_hold(analyse_hold(
                    list(hold.samples), cfg=self.cfg, tuning=self.active_tuning()), s.ts)
                if verdict is not None or (self._holds and self._holds[-1].ts == s.ts
                                           and self._holds[-1].oscillating):
                    hold.clear(s.ts)   # judged: collect fresh evidence
                event = verdict
            elif st == _ABORT:
                self._hold = None

        # Start of a heat-up: a setpoint step up (or heating resuming after a
        # window / summer / off period) with the room well below target.
        if (
            self._heatup is None and s.eligible and room is not None
            and sp - room >= self.cfg.heatup_min_error_c
        ):
            step_up = (
                self._prev_sp is not None
                and sp - self._prev_sp >= self.cfg.heatup_min_step_c
            )
            if step_up or resumed:
                self._hold = None
                self._heatup = _HeatupTracker(s.ts, sp, self.cfg)
                self._heatup.add(s)
                self.last_event = "heat-up started"

        if sp is not None and (self._prev_sp is None or abs(sp - self._prev_sp) > 0.05):
            self._sp_since_ts = s.ts
        if not s.eligible or resumed:
            self._sp_since_ts = s.ts

        # Start of a hold window.
        if (
            self._heatup is None and self._hold is None and s.eligible
            and room is not None and self._sp_since_ts is not None
            and s.ts - self._sp_since_ts >= self.cfg.hold_settle_s
            and s.ts - self._last_heatup_end_ts >= self.cfg.hold_after_heatup_s
            and abs(sp - room) <= self.cfg.hold_start_max_error_c
        ):
            self._hold = _HoldTracker(s.ts, sp, self.cfg)
            self._hold.add(s)

        if self._pending is not None and s.ts - self._pending["ts"] > self.cfg.pending_timeout_s:
            self._accept_pending()

        self._prev_sp = sp
        self._prev_ts = s.ts
        self._prev_eligible = s.eligible
        return event

    # -- episode handlers ------------------------------------------------------
    def _trim_by(self, name: str, factor: float) -> None:
        c = self.cfg
        lo, hi = {
            "kp": (c.kp_trim_min, c.kp_trim_max),
            "ki": (c.ki_trim_min, c.ki_trim_max),
            "td_s": (c.td_trim_min, c.td_trim_max),
        }[name]
        self._trim[name] = _clamp(self._trim[name] * factor, lo, hi)

    def _on_heatup(self, m: HeatupMetrics | None, now: float) -> str | None:
        if m is None:
            self.last_event = "heat-up too short to analyse"
            return None
        c = self.cfg
        self._heatups.append(m)
        del self._heatups[:-c.history_len]
        self._counters["heatups"] += 1
        os_txt = f"{m.overshoot_c:+.2f} °C" if m.overshoot_c is not None else "unknown"
        self.last_event = (
            f"heat-up{' (cut short)' if m.truncated else ''}: overshoot {os_txt}"
            + (f", dead time {m.theta_s / 60:.0f} min" if m.theta_s else "")
            + (f", coast {m.coast_s / 60:.0f} min" if m.coast_s is not None else "")
        )
        os = m.overshoot_c
        if os is None:
            return self._maybe_update(now, "heat-up")

        # Residual trims on top of the coast-based Td target.  More braking
        # (safe direction) needs one episode, less braking needs confirm_up.
        model_coast = self._model()["coast_s"]
        td_model = (_clamp(model_coast * self._trim["td_s"], 0, c.td_max_s)
                    if model_coast is not None else None)
        td_near_model = td_model is None or m.td_s >= 0.85 * td_model
        braking = m.td_s > 0                       # a brake was active (learned or manual)
        learns_td = self.derivative_allowed and braking
        if os > c.overshoot_target_c + 0.1:
            if learns_td and td_near_model:
                self._trim_by("td_s", 1.15)
            self._votes["td_down"] = 0
            self._votes["kp_up"] = 0
        elif os < -c.undershoot_sag_c:
            # A stall only says "stopped too early" if we actually told the
            # TRV to stop (demand <= 0) and the episode ran its course.  A
            # room that never got there with demand still positive is
            # under-powered (cold weather, small radiator): more gain will
            # not help, so it is not evidence either way.
            if not m.crossed or m.truncated:
                pass
            elif braking:
                if learns_td:
                    self._votes["td_down"] += 1
                    if self._votes["td_down"] >= c.confirm_up:
                        self._trim_by("td_s", 0.85)
                        self._votes["td_down"] = 0
                # Manual brake with learning off: the user's Td is the cause;
                # raising Kp would not help (the brake scales with 1 + Kp).
            else:
                self._votes["kp_up"] += 1
                if self._votes["kp_up"] >= c.confirm_up:
                    self._trim_by("kp", 1.1)
                    self._votes["kp_up"] = 0
        elif os < 0.5 * c.overshoot_target_c and learns_td and not m.truncated:
            # Comfortably inside the target: give a little speed back.
            self._votes["td_down"] += 1
            if self._votes["td_down"] >= c.confirm_up:
                self._trim_by("td_s", 0.9)
                self._votes["td_down"] = 0
        else:
            self._votes["td_down"] = 0
            self._votes["kp_up"] = 0

        if self._pending is not None and now > self._pending["ts"]:
            self._pending["overshoots"].append(os)
            if len(self._pending["overshoots"]) >= self._pending["needs"]:
                med = _median(self._pending["overshoots"])
                ref = self._pending["ref_overshoot"]
                if (ref is not None and med is not None
                        and med > ref + c.rollback_margin_c and med > c.overshoot_target_c):
                    return self._rollback(now, f"overshoot rose to {med:.2f} °C")
                self._accept_pending()
        return self._maybe_update(now, "heat-up")

    def _on_hold(self, m: HoldMetrics | None, now: float) -> str | None:
        c = self.cfg
        if m is None:
            return None
        if m.heating_fraction < c.hold_min_heating_fraction:
            self.last_event = "hold window ignored (no heating – passive cool-down)"
            return None
        self._holds.append(m)
        del self._holds[:-4]
        self._counters["holds"] += 1
        theta = self._model()["theta_s"] or 900.0
        if m.oscillating:
            fast = m.period_s is None or m.period_s < c.osc_fast_period_factor * theta
            self._trim_by("kp" if fast else "ki", 0.85 if fast else 0.7)
            self._votes["ki_up"] = self._votes["kp_up"] = 0
            self.last_event = (
                f"hold: oscillation ±{m.amplitude_c:.2f} °C"
                + (f", period {m.period_s / 60:.0f} min" if m.period_s else "")
            )
            emergency = m.amplitude_c >= c.emergency_amplitude_factor * c.osc_amplitude_c
            rolled = None
            if self._pending is not None and (emergency or not self._pending["ref_osc"]):
                # A "more heat" change still on trial is undone before anything
                # else: it is never accepted silently.
                rolled = self._rollback(now, "oscillation after the last change")
                if not emergency:
                    return rolled
            if emergency:
                # Cut from where the values actually are, so a trim that was
                # raised but not yet applied cannot swallow the cut.
                self._anchor_trims(only_down=True)
                self._trim_by("kp", 0.85)
                self._trim_by("ki", 0.7)
            reason = self._maybe_update(now, "oscillation", emergency=emergency)
            return reason or rolled
        self.last_event = f"hold: mean error {m.mean_error_c:+.2f} °C, no oscillation"
        # Only a room that stays too *cold* while heating, with no swinging
        # at all, argues for a faster integral.  A room that stays too warm is
        # coasting heat (Td's job) and a partial swing is not an offset: both
        # are classic ways a tuner talks itself into more integral action.
        if m.mean_error_c >= c.hold_offset_c and m.half_cycles <= 1:
            self._votes["ki_up"] += 1
            if self._votes["ki_up"] >= c.confirm_up:
                self._trim_by("ki", 1.2)
                self._votes["ki_up"] = 0
        else:
            self._votes["ki_up"] = 0
        return self._maybe_update(now, "hold")

    # -- updates ------------------------------------------------------------------
    @staticmethod
    def _toward(cur: float, target: float, down: float, up: float) -> float:
        if target < cur:
            return max(target, cur * down)
        return min(target, cur * up) if cur > 0 else target

    def _maybe_update(self, now: float, trigger: str, *, emergency: bool = False) -> str | None:
        c = self.cfg
        if not emergency and self._pending is not None:
            return None
        cur = self._params
        tgt = self._targets()
        kp = self._toward(cur.kp, tgt.kp, c.kp_step_down, c.kp_step_up)
        ki = self._toward(cur.ki, tgt.ki, c.ki_step_down, c.ki_step_up)
        td = cur.td_s
        if self.derivative_allowed:
            step = max(c.td_step_min_s, c.td_step_frac * cur.td_s)
            td = cur.td_s + _clamp(tgt.td_s - cur.td_s, -step, step)
        if emergency:
            # The caller has already cut the trims, so the targets are lower:
            # take one step down towards them now, never up, never less
            # braking.  Going via the trims keeps target and value in step,
            # so a later normal update cannot creep back up.
            kp = min(kp, cur.kp)
            ki = min(ki, cur.ki)
            td = max(td, cur.td_s)
        # Per-value dead-bands: tiny moves (e.g. the SIMC Ki drifting as the
        # dead-time median shifts by a minute) are noise.  Dropping them per
        # value also keeps a clearly gentler change free of a sliver of
        # "more heat" that would put it on trial.
        if abs(kp - cur.kp) <= c.kp_deadband_rel * max(cur.kp, 1e-9):
            kp = cur.kp
        if abs(ki - cur.ki) <= c.ki_deadband_rel * max(cur.ki, 1e-12):
            ki = cur.ki
        if abs(td - cur.td_s) < c.td_deadband_s:
            td = cur.td_s
        new = self._bounded(Tuning(kp, ki, td))
        if emergency:
            new = Tuning(min(new.kp, cur.kp), min(new.ki, cur.ki), max(new.td_s, cur.td_s))

        def rel(a: float, b: float) -> float:
            return abs(a - b) / max(abs(b), 1e-9)

        changed = new != cur
        if not changed:
            return None
        more_heat = (new.kp > cur.kp * 1.0001 or new.ki > cur.ki * 1.0001
                     or new.td_s < cur.td_s - 1.0)
        interval = c.min_interval_up_s if more_heat else c.min_interval_down_s
        if not emergency and self._last_change_ts and now - self._last_change_ts < interval:
            return None

        if more_heat:
            # Judge it against the next heat-ups; roll back if worse.
            prev_os = [m.overshoot_c for m in self._heatups[-2:] if m.overshoot_c is not None]
            self._pending = {
                "ts": now,
                "prev": _tuning_dict(cur),
                "ref_overshoot": _median(prev_os),
                "ref_osc": any(h.oscillating for h in self._holds[-2:]),
                "needs": c.confirm_up,
                "overshoots": [],
            }
        else:
            # More braking, lower gains: cannot raise overshoot or start an
            # oscillation by construction, so a sunny heat-up afterwards must
            # not be able to roll it back.  An emergency detune supersedes
            # any change still being judged.
            self._pending = None
            self._last_good = new
        self._params = new
        self._last_change_ts = now
        self._counters["updates"] += 1
        parts = []
        for name, fmt in (("kp", "{:.2f}"), ("ki", "{:.5f}")):
            a, b = getattr(cur, name), getattr(new, name)
            if rel(b, a) > 0.005:
                parts.append(f"{name} {fmt.format(a)}→{fmt.format(b)}")
        if abs(new.td_s - cur.td_s) >= 1.0:
            parts.append(f"td {cur.td_s / 60:.0f}→{new.td_s / 60:.0f} min")
        self.last_update_reason = f"{trigger}: " + ", ".join(parts)
        _LOGGER.info("Auto-tune update (%s)", self.last_update_reason)
        return self.last_update_reason

    def _anchor_trims(self, only_down: bool = False) -> None:
        """Set the trims so the targets equal the current values.

        Used after a rollback: the evidence that pushed the failed move is
        neutralised, so nothing re-proposes it until new evidence arrives.
        With ``only_down`` the Kp/Ki trims are only lowered (emergency) and
        the Td trim is left alone.
        """
        b, cur, m = self._baseline, self._params, self._model()
        anchored = dict(self._trim)
        if b.kp > 0:
            anchored["kp"] = cur.kp / b.kp
        if b.ki > 0:
            base = b.ki
            if m["theta_s"] is not None and m["theta_s"] > 0:
                base = (1.0 + b.kp * anchored["kp"]) / (4.0 * (m["tau_c_s"] + m["theta_s"]))
            anchored["ki"] = cur.ki / base
        if m["coast_s"]:
            anchored["td_s"] = cur.td_s / m["coast_s"]
        for name in self._trim:
            if only_down:
                if name != "td_s":
                    self._trim[name] = min(self._trim[name], anchored[name])
            else:
                self._trim[name] = anchored[name]
            self._trim_by(name, 1.0)   # clamp into the trim bounds

    def _accept_pending(self) -> None:
        self._last_good = self._params
        self._pending = None

    def _rollback(self, now: float, why: str) -> str:
        """Restore the values from before the change on trial.

        No freeze and no blocked directions: the tuner carries on and may
        try a similar move again.  It needs fresh evidence to do so (the
        trims are re-anchored to the restored values and the votes are
        cleared) and it waits the normal rate limit, counted from now.
        """
        p = self._pending or {}
        prev = _tuning_from(p.get("prev")) or self._last_good
        self._params = self._bounded(prev)
        self._anchor_trims()
        self._votes = dict.fromkeys(self._votes, 0)
        self._last_good = self._params
        self._pending = None
        self._last_change_ts = now
        self._counters["rollbacks"] += 1
        self.last_update_reason = f"rolled back ({why})"
        _LOGGER.warning("Auto-tune rollback: %s", why)
        return self.last_update_reason

    # -- reporting -------------------------------------------------------------------
    def summary(self, now: float | None = None) -> dict[str, Any]:
        """Diagnostic snapshot for attributes and sensors."""
        now = self._now if now is None or not _finite(now) else max(now, self._now)
        m = self._model()
        last = self._heatups[-1] if self._heatups else None
        t = self.active_tuning()
        theta = m["theta_s"]
        ti = None
        if theta is not None and theta > 0:
            ti = 4.0 * (m["tau_c_s"] + theta)

        def r(v: float | None, nd: int = 2) -> float | None:
            return round(v, nd) if v is not None else None

        return {
            "status": self.status_at(now),
            "phase": self.phase,
            "kp": round(t.kp, 3),
            "ki": round(t.ki, 6),
            "td_min": round(t.td_s / 60.0, 1),
            "baseline_kp": self._baseline.kp,
            "baseline_ki": self._baseline.ki,
            "baseline_td_min": round(self._baseline.td_s / 60.0, 1),
            "dead_time_min": r(theta / 60.0 if theta else None, 1),
            "heating_rate_c_per_h": r(m["rate_c_per_h"]),
            "coast_time_min": r(m["coast_s"] / 60.0 if m["coast_s"] is not None else None, 1),
            "simc_tau_c_min": r(m["tau_c_s"] / 60.0 if m["tau_c_s"] else None, 1),
            "simc_ti_min": r(ti / 60.0 if ti else None, 1),
            "simc_ki": r(self._simc_ki(), 6),
            "last_overshoot_c": r(last.overshoot_c if last and last.overshoot_c is not None else None),
            "heatups_analysed": self._counters["heatups"],
            "holds_analysed": self._counters["holds"],
            "updates": self._counters["updates"],
            "rollbacks": self._counters["rollbacks"],
            "awaiting_evaluation": self._pending is not None,
            "last_update": self.last_update_reason,
            "last_event": self.last_event,
            "trims": {k: round(v, 3) for k, v in self._trim.items()},
        }

    # -- persistence ---------------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.VERSION,
            "baseline": _tuning_dict(self._baseline),
            "params": _tuning_dict(self._params),
            "last_good": _tuning_dict(self._last_good),
            "trim": dict(self._trim),
            "votes": dict(self._votes),
            "heatups": [m.as_dict() for m in self._heatups],
            "holds": [m.as_dict() for m in self._holds],
            "last_change_ts": self._last_change_ts,
            "pending": self._pending,
            "counters": dict(self._counters),
            "last_update_reason": self.last_update_reason,
            "last_event": self.last_event,
            "episode": {
                "heatup": self._heatup.to_dict() if self._heatup is not None else None,
                "prev_sp": self._prev_sp,
                "prev_ts": self._prev_ts,
                "prev_eligible": self._prev_eligible,
                "sp_since_ts": self._sp_since_ts,
                "last_heatup_end_ts": (self._last_heatup_end_ts
                                       if _finite(self._last_heatup_end_ts) else None),
            },
        }

    def _load(self, d: Any) -> None:
        """Restore learned state; anything malformed falls back to a fresh start."""
        try:
            if not isinstance(d, dict) or d.get("version") != self.VERSION:
                return
            if _tuning_from(d.get("baseline")) != _tuning_from(_tuning_dict(self._baseline)):
                self.last_event = "configured values changed – learning restarted"
                return
            params = _tuning_from(d.get("params"))
            if params is None:
                return
            self._params = self._bounded(params)
            self._last_good = self._bounded(_tuning_from(d.get("last_good")) or self._params)
            for k, v in (d.get("trim") or {}).items():
                if k in self._trim and _finite(v):
                    self._trim[k] = float(v)
                    self._trim_by(k, 1.0)  # re-clamp
            for k, v in (d.get("votes") or {}).items():
                if k in self._votes and isinstance(v, int) and v >= 0:
                    self._votes[k] = min(v, self.cfg.confirm_up)
            self._heatups = [m for m in (HeatupMetrics.from_dict(x) for x in d.get("heatups") or [])
                             if m is not None][-self.cfg.history_len:]
            self._holds = [m for m in (HoldMetrics.from_dict(x) for x in d.get("holds") or [])
                           if m is not None][-4:]
            if _finite(d.get("last_change_ts")):
                self._last_change_ts = float(d["last_change_ts"])
            p = d.get("pending")
            if (isinstance(p, dict) and _finite(p.get("ts")) and _tuning_from(p.get("prev"))
                    and isinstance(p.get("overshoots"), list)):
                p["overshoots"] = [float(x) for x in p["overshoots"] if _finite(x)]
                p["needs"] = int(p.get("needs", 1)) if isinstance(p.get("needs"), int) else 1
                ref = p.get("ref_overshoot")
                p["ref_overshoot"] = float(ref) if _finite(ref) else None
                p["ref_osc"] = bool(p.get("ref_osc"))
                self._pending = p
            for k, v in (d.get("counters") or {}).items():
                if k in self._counters and isinstance(v, int) and v >= 0:
                    self._counters[k] = v
            if isinstance(d.get("last_update_reason"), str):
                self.last_update_reason = d["last_update_reason"][:200]
            # Keep the last real event across restarts.  "heat-up started" is
            # only true if that heat-up came back with the state.
            ev = d.get("last_event")
            if isinstance(ev, str) and ev and ev != "heat-up started":
                self.last_event = ev[:200]
            elif self._counters["heatups"] or self._counters["holds"]:
                self.last_event = "restored learned values"
            else:
                self.last_event = "waiting for a heat-up"
        except (TypeError, ValueError, KeyError, AttributeError, IndexError):
            _LOGGER.warning("Auto-tune: stored state unreadable, starting fresh")
            self._reset_learning()
            return
        self._load_episode(d.get("episode"))

    def _load_episode(self, e: Any) -> None:
        """Restore the episode context; anything malformed is simply dropped."""
        self._heatup = None
        self._hold = None
        if not isinstance(e, dict):
            return
        try:
            def opt(key: str) -> float | None:
                v = e.get(key)
                return float(v) if _finite(v) else None

            self._prev_sp = opt("prev_sp")
            self._prev_ts = opt("prev_ts")
            self._prev_eligible = e.get("prev_eligible") is not False
            self._sp_since_ts = opt("sp_since_ts")
            end = opt("last_heatup_end_ts")
            self._last_heatup_end_ts = end if end is not None else -math.inf
            self._heatup = _HeatupTracker.from_dict(e.get("heatup"), self.cfg)
        except (TypeError, ValueError, KeyError, AttributeError, IndexError):
            self._heatup = None
            self._prev_sp = self._prev_ts = self._sp_since_ts = None
            self._prev_eligible = True
            self._last_heatup_end_ts = -math.inf
            return
        if self._heatup is not None:
            self.last_event = "heat-up continued after a restart"
