"""Central parameter defaults for roomstat."""
from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Behavioural thresholds (climate-entity logic, independent of the PI engine)
# ---------------------------------------------------------------------------

@dataclass
class BehaviourConfig:
    """Thresholds for the send-decision logic.

    These values can be overridden via config-entry options so operators can
    tune the integration's responsiveness without touching source code.
    """

    # Rate-limit bypass: immediately send if the new target is this many °C
    # below the current Tado setpoint (urgent cool-down).
    urgent_decrease_threshold_c: float = 1.0


# ---------------------------------------------------------------------------
# Integration behaviour defaults
# ---------------------------------------------------------------------------

DEFAULT_CONTROL_INTERVAL_S: int = 60   # seconds between regulation cycles
FROST_PROTECT_C: float = 5.0           # target temperature when HVAC is OFF
DEFAULT_SENSOR_GRACE_S: int = 300      # seconds to use last-valid room_temp when sensor is unavailable


# ---------------------------------------------------------------------------
# Correction tuning  (PI layer on top of feedforward)
# ---------------------------------------------------------------------------

@dataclass
class CorrectionTuning:
    """PI correction parameters applied on top of the feedforward offset.

    These are intentionally *small* – the feedforward does the heavy lifting.
    """

    kp: float = 0.8    # proportional gain for residual room-error correction
    ki: float = 0.003   # integral gain for slow steady-state error correction
    # Derivative brake time (s).  0 = off (default).  Only ever *reduces*
    # the command while the room temperature is rising, so the TRV stops
    # heating before the room reaches the target instead of after it.  See
    # regulation.py and CLAUDE.md for why this is one-sided.
    td_s: float = 0.0


# ---------------------------------------------------------------------------
# Auto-tune (see autotune.py and CLAUDE.md)
# ---------------------------------------------------------------------------

@dataclass
class AutotuneConfig:
    """Bounds, thresholds and step limits for the background auto-tuner."""

    # Absolute hard bounds on the tuned values.  Nothing the tuner does can
    # leave these, whatever the measurements say.
    kp_min: float = 0.1
    kp_max: float = 2.0
    ki_min: float = 0.00005
    ki_max: float = 0.005
    td_max_s: float = 2700.0           # 45 min

    # Bounds relative to the user's configured values (the baseline).
    kp_rel_min: float = 0.25
    kp_rel_max: float = 2.0
    ki_rel_min: float = 0.05
    ki_rel_max: float = 2.0

    # Largest change per update (multiplicative).
    kp_step_down: float = 0.85
    kp_step_up: float = 1.10
    ki_step_down: float = 0.7
    ki_step_up: float = 1.2
    td_step_frac: float = 0.5          # Td moves at most 50 % (or 5 min) per update
    td_step_min_s: float = 300.0
    # Changes smaller than these are ignored (noise, not evidence).
    kp_deadband_rel: float = 0.05
    ki_deadband_rel: float = 0.15
    td_deadband_s: float = 120.0

    # Minimum time between parameter changes, so each change can be judged
    # before the next one.  Detuning (making things gentler) waits less.
    min_interval_up_s: float = 12 * 3600.0
    min_interval_down_s: float = 3 * 3600.0

    # SIMC: tau_c = lambda * theta.  1.5 rather than SIMC's default 1.0
    # because overshoot costs hours here (passive cooling is slow).
    simc_lambda: float = 1.5
    # Ti may not drop below this multiple of (tau_c + theta).  SIMC uses 4.
    simc_ti_factor_min: float = 2.0
    # Heat-ups with a dead time needed before the SIMC Ki is used.
    simc_min_episodes: int = 2

    # Heat-up episode detection.
    heatup_min_step_c: float = 0.5     # setpoint step that starts an episode
    heatup_min_error_c: float = 0.4    # room must be at least this far below
    heatup_max_s: float = 5 * 3600.0   # give up if the target is not reached
    heatup_settle_s: float = 45 * 60.0 # flat/falling this long after the peak
    # Smaller steps still show overshoot and coast, but the dead time and
    # heating rate need a bigger rise to stand out from sensor noise.
    theta_min_step_c: float = 0.8
    rate_min_step_c: float = 0.6
    # An episode cut short by the schedule is kept if the TRV demand had
    # already gone <= 0 at least this long before (coast as a lower bound),
    # or if it ran at least this long (dead time and heating rate only).
    truncated_min_after_cross_s: float = 15 * 60.0
    truncated_min_duration_s: float = 45 * 60.0

    # Hold windows (steady setpoint, room near target, heating active).
    hold_window_s: float = 3 * 3600.0  # evaluated every window
    hold_buffer_s: float = 24 * 3600.0 # oscillation judged over up to this much
    hold_settle_s: float = 3600.0      # setpoint unchanged this long first
    hold_after_heatup_s: float = 1800.0  # and this long after a heat-up ended
    hold_start_max_error_c: float = 0.5
    hold_min_heating_fraction: float = 0.2  # passive cool-downs are not holds
    hold_offset_c: float = 0.15        # mean error that counts as an offset
    osc_amplitude_c: float = 0.15      # swing beyond +/- this around the mean
    osc_min_half_cycles: int = 3
    # Oscillation faster than this many dead times is blamed on Kp, slower
    # on Ki (an integrating loop with too much P rings at ~4 theta).
    osc_fast_period_factor: float = 6.0
    # Slower "cycles" are weather or routine (sun, occupancy), not control.
    osc_max_period_s: float = 12 * 3600.0
    # Emergency detune when the swing is this many times osc_amplitude_c.
    emergency_amplitude_factor: float = 2.0

    # Targets.
    overshoot_target_c: float = 0.2    # acceptable heat-up overshoot
    undershoot_sag_c: float = 0.2      # peak this far below target = braked too early

    # Trim factors applied on top of the model-based targets.
    kp_trim_min: float = 0.25          # = kp_rel_min
    kp_trim_max: float = 1.5
    ki_trim_min: float = 0.05          # = ki_rel_min
    ki_trim_max: float = 3.0
    td_trim_min: float = 0.5
    td_trim_max: float = 2.5

    # Evidence: number of episodes kept for medians, and how many must agree
    # before a value moves in the "more heat" direction.
    history_len: int = 5
    confirm_up: int = 2

    # Watchdog: an update is rolled back if the next episodes are worse by
    # this much (overshoot, °C) or a new oscillation appears.
    # After a rollback the tuner carries on straight away (no freeze, nothing
    # blocked); a retry needs fresh evidence and the normal rate limit.
    rollback_margin_c: float = 0.2
    pending_timeout_s: float = 7 * 86400.0

    # Plausibility of identified values; anything outside is discarded.
    theta_min_s: float = 180.0
    theta_max_s: float = 3600.0
    rate_min_c_per_h: float = 0.2
    rate_max_c_per_h: float = 10.0


# ---------------------------------------------------------------------------
# Preset defaults
# ---------------------------------------------------------------------------

@dataclass
class PresetConfig:
    """Temperature settings for each preset mode."""

    comfort_target_c: float = 20.0   # default comfort temperature
    eco_target_c: float = 17.0       # fixed eco temperature (independent of comfort)
    boost_target_c: float = 25.0     # fixed target during boost
    boost_duration_min: int = 30     # auto-revert to comfort after this many minutes
    away_target_c: float = 17.0      # fixed target when away
    frost_protection_target_c: float = 7.0   # frost protection temperature


# ---------------------------------------------------------------------------
# Full regulation config with safety rails
# ---------------------------------------------------------------------------

@dataclass
class RegulationConfig:
    """All regulation parameters and safety limits."""

    tuning: CorrectionTuning = field(default_factory=CorrectionTuning)
    presets: PresetConfig = field(default_factory=PresetConfig)

    # Absolute temperature limits for commands sent to Tado.
    #
    # Tado V3+ radiator thermostats (reached over HomeKit or the cloud
    # integration) accept 5–25 °C only.  Commanding above 25 makes
    # ``climate.set_temperature`` raise ServiceValidationError
    # ("Provided temperature X is not valid. Accepted range is 5 to 25"),
    # so every write is rejected and the room never heats.  Because the
    # regulator clamps to these limits *before* deciding, keeping the
    # ceiling here (rather than clamping at send time) also keeps the
    # anti-windup saturation logic honest.
    min_target_c: float = 5.0
    max_target_c: float = 25.0

    # Anti-windup limits for the integral correction term
    integral_min_c: float = -2.0
    integral_max_c: float = 2.0

    # Integral deadband: only accumulate integral when |error| < this value.
    # Outside this zone the integral decays, preventing buildup during gross
    # heating/cooling transients that would cause overshoot.
    integral_deadband_c: float = 0.3

    # Decay factor applied to the integral each cycle when |error| >= deadband.
    # 0.95 means ~5% reduction per 60s cycle → drains in ~15 min.
    integral_decay: float = 0.95

    # Rate limiting: minimum seconds between commands to Tado (battery saving)
    min_command_interval_s: float = 180.0

    # Minimum difference to current Tado setpoint before sending a new command
    min_change_threshold_c: float = 0.3

    # Derivative brake limits.  The brake is -(1 + Kp) * Td * max(0, slope),
    # clamped to [-derivative_max_c, 0]: it can lower the command by at most
    # this much and can never raise it.
    derivative_max_c: float = 2.0
    # Room slopes below this (°C/h) are treated as flat, so sensor jitter
    # during a steady hold does not wiggle the command.
    derivative_slope_deadband_c_per_h: float = 0.1

    # Adaptive gain scheduling: scale Kp based on error magnitude
    gain_scheduling_enabled: bool = True
    gain_startup_multiplier: float = 1.5   # Kp multiplier when |error| > startup_threshold
    gain_fine_multiplier: float = 1.0      # Kp multiplier when |error| < fine_threshold
    gain_startup_threshold_c: float = 2.0  # error threshold for startup (aggressive) zone
    gain_fine_threshold_c: float = 0.5     # error threshold for fine (gentle) zone
