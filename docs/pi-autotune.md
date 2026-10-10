# Auto-tune: design and rationale

How the background auto-tuner works, why it is shaped the way it is, and
what keeps it from running away. Written for contributors and for anyone who
wants to check the reasoning before letting it loose on a nursery.

Short version: switch it on per room under **Options → Auto-tune**. It
watches ordinary heat-ups and steady periods, learns how the room and its
TRV behave, and adjusts Kp, Ki and a new *heat-up braking time* in small,
bounded steps. Each change is checked against the next heat-up and undone if
it made things worse.

---

## 1. What real rooms showed

Seven days of recorder data from four UK rooms (October 2026, Kp 0.5–0.9,
Ki 0.002–0.003, 60 s command interval):

| Measurement | Value |
|---|---|
| Setpoint step → Tado's own reading moves | 9–14 min |
| Setpoint step → room sensor moves (+0.15 °C) | 13–21 min |
| Heat-up rate, valve open | 1.3–3.2 °C/h |
| Passive cooling, valve shut | 0.3–0.5 °C/h |
| Heat-up overshoot | 0.4–1.2 °C in 4 of 6 clean heat-ups |
| Room-sensor noise (median 1-min change) | 0.005 °C, with occasional single 1–1.5 °C glitches |

Two things stood out.

**The TRV keeps heating after it is told to stop.** In every overshooting
heat-up the demand the TRV acts on (`its setpoint − its reading`) went
negative before the peak, yet the radiator stayed hot. In the study on
8 October the TRV's own reading kept *rising* for 20 minutes after demand
was already −0.4 to −1.9 °C. Tado's internal controller has an integrator,
and it unwinds slowly. (A valve that doesn't fully seal would look the same.)
This "coast" lasts 30–60 min.

**Heating is fast, cooling is slow.** A room gains heat 5–10× faster than it
loses it. An overshoot of 0.5 °C costs one to two hours of an over-warm
room. An undershoot of 0.5 °C is recovered in ten minutes.

---

## 2. The plant, as the proxy sees it

The proxy cannot move the valve. It sets the TRV's target, and Tado's own PI
controller moves the valve. Because the feedforward cancels Tado's reading,
what Tado's controller actually acts on is

```
tado_demand = command − tado_reading
            = (1 + Kp)·error + integral + brake
```

Three consequences:

1. **The loop gain on the room error is (1 + Kp), not Kp.** The feedforward
   contributes the 1. Kp = 0 still drives the TRV with the full room error.
2. **There are two integrators in series**: ours (gated to ±0.3 °C around
   the target) and Tado's (always on). Tado's handles most of the steady
   state, and ours trims. That is why ours must be slow.
3. **The demand crosses zero when the error does.** With P + I alone, Tado
   is told to stop only when the room is already at the target. Everything
   still in the radiator then lands as overshoot, whatever Kp is. The
   simulator confirms this: Kp 0.6 → 0.1 moves the living-room overshoot only
   from 0.59 to 0.56 °C, and just makes the heat-up slower.

So the model the tuner works with is:

- **Integrating with dead time θ.** Rooms lose heat over tens of hours, far
  longer than θ, so SIMC's integrating-process rules apply.
- **Saturating.** At large demand the valve is fully open and the rate is
  capped.
- **Asymmetric.** The actuator can only add heat. Cooling is passive.
- **Coasting.** Heat keeps arriving for a "coast time" after demand ≤ 0.

---

## 3. Is SIMC appropriate?

Partly. Each part was checked against the data and the simulator.

| SIMC piece | Used? | Why |
|---|---|---|
| Integrating-process form (τ₁ ≫ θ) | **Yes** | Passive cooling of 0.3–0.5 °C/h means τ₁ is 20–50 h, versus θ of 10–20 min |
| Dead time θ | **Yes** | Measured from every heat-up with the tangent method. Robust: needs no knowledge of Tado's gain |
| Ti = 4 (τc + θ) | **Yes** | Sets Ki = (1 + Kp) / Ti. It gives Ki ≈ 0.0002, 5–10× below the hand-tuned values. Measured: the integral currently swings ±0.5 °C in 20 min, faster than the room can respond |
| τc | **1.5 θ** | Not SIMC's default 1.0 θ. Overshoot is expensive here (slow cooling), so the loop is detuned a little |
| Kc = 1 / (k′ (τc + θ)) | **No** | Needs the plant gain k′, which closed-loop heat-ups cannot give (see below) |
| D when a second lag ≥ θ | **Yes**, as a one-sided brake | The coast is that second lag, and it is measured directly (§4) |

**Why SIMC's Kc is not used.** The only gain estimate available is
`heating rate / demand`. It is wrong in three ways:

- Tado saturates the valve during most heat-ups, so the rate is capped.
- Tado's integrator makes the effective gain drift during the episode.
- The demand itself is proportional to Kc.

The third is the dangerous one. A gain ceiling built this way reduces to a
test that does not depend on Kc, so "lower Kp until under the ceiling" never
terminates. An early version of this tuner did exactly that in simulation,
ratcheting the study's Kp from 0.6 to 0.27 in three days. Kp
therefore stays at the configured value. It moves only on direct evidence:
it goes down on oscillation, and up on a heat-up that repeatedly stalls short
of the target with no braking active.

---

## 4. Do we need a D term?

Yes. It has to be a brake, though, not a classical D.

To land without overshoot, Tado must be told to stop *before* the target.
The distance needed is the heat still on its way: roughly slope × coast
time. That is exactly what derivative action provides:

```
brake = −(1 + Kp) · Td · max(0, room_slope)        clamped to [−2 °C, 0]
```

With Td = coast time, the demand crosses zero when `error = Td × slope`, and
the coast then carries the room the rest of the way.

Why it is one-sided, clamped and on a robust slope:

- **One-sided.** It acts only while the room is *rising*, and can only
  *lower* the command. A falling reading (open window, door, sensor glitch)
  can never make it add heat. Falling-side anticipation buys little anyway,
  because heating is fast.
- **Clamped** to −2 °C. A worst-case slope estimate cannot command an absurd
  setpoint.
- **Theil–Sen slope** over 12 minutes (the median of pairwise slopes). One
  wild 1.5 °C reading moves it by < 0.05 °C/h, where a least-squares slope
  would move by ~5 °C/h. At the measured noise the brake jitters by
  ~0.02 °C in a steady hold, well under the 0.1 °C send threshold. A 0.1 °C/h
  dead-band removes even that.
- **On the measurement, not the error.** A setpoint step never kicks it.
- **Off when the sensor is degraded**, i.e. bridging a gap with the last
  value.

Simulated effect on the living room (Kp 0.6, Ki 0.002): Td 15 min cuts
overshoot from 0.59 to 0.28 °C. Td 20 min with Ki 0.0005 cuts it to 0.21 °C.

A full PID was rejected. A bidirectional D on a 0.005 °C-resolution sensor
with glitches would add heat on every downward spike, and it buys nothing on
the cooling side.

---

## 5. The tuner

### Episodes it learns from

| Episode | Starts | Ends | Gives |
|---|---|---|---|
| **Heat-up** | Setpoint step up ≥ 0.5 °C (or heating resumes) with the room ≥ 0.4 °C below | 45 min after the peak, after 5 h, or early when the schedule moves on (kept only if braking had already happened; see below) | Dead time θ (tangent method, steps ≥ 0.8 °C), max heating rate, coast time, overshoot |
| **Hold** | Setpoint steady ≥ 1 h, room within 0.5 °C | Evaluated every 3 h over a rolling 24 h buffer | Mean offset (last 3 h), oscillation (hysteresis count over the buffer), heating fraction |

Nothing is learned while any of these apply: window or presence automation,
boost, summer mode, HVAC off, sensor degraded. Episodes in progress are
dropped on restart. The learned results are kept.

**Coast time** = rise after the TRV demand first goes ≤ 0, divided by the
slope at that moment. It measures the plant, not the controller, so it is
valid whether or not the brake was active.

**Cut-short heat-ups.** Morning comfort periods often end before the room
settles. A truncated episode keeps its overshoot only if it already shows
*too much* overshoot, or if the peak is at least 15 min old. A cut-off
episode can therefore never argue for *less* braking.

### How values move

| Value | Target | Moves when |
|---|---|---|
| **Td** | median coast time × trim | Towards target after each heat-up. Trim ×1.15 if overshoot stays > 0.3 °C once Td has caught up. Trim ×0.9 after 2 heat-ups under 0.1 °C. Trim ×0.85 after 2 that stall below target. Stops growing once 2 heat-ups are within 0.2 °C |
| **Ki** | (1 + Kp) / (4 (τc + θ)) × trim | Towards SIMC after each heat-up. Trim ×0.7 on a slow oscillation. Trim ×1.2 after 2 holds that stay ≥ 0.15 °C too *cold* with no swing at all |
| **Kp** | configured × trim | Trim ×0.85 on a fast oscillation (period < 6 θ). Trim ×1.1 after 2 stalled heat-ups with no braking |

A room that stays too *warm* never raises Ki: that is coast, Td's job.
Neither does a partial swing. Both are classic ways a tuner talks itself into
more integral.

Simulated result after two weeks, using the living-room and study models
calibrated to the data above. Each row compares the same day with the
tuner off ("fixed") and on:

| Room | | Overshoot (morning / evening) | Within 0.5 °C | Within 0.1 °C | Hold std. dev. |
|---|---|---|---|---|---|
| Living room | fixed | 0.75 / 0.43 °C | 34 min | 45 min | 0.16 °C |
| | tuned | **0.15 / 0.19 °C** | 31 min | 67 min | 0.13 °C |
| Study | fixed | 1.08 / 0.57 °C | 38 min | 51 min | 0.16 °C |
| | tuned | **0.15 / 0.18 °C** | 37 min | 61 min | 0.12 °C |

Learned values: Td 27 min / Ki 0.00021 (living room) and Td 32 min /
Ki 0.00016 (study). Kp stayed at 0.6 in both. Braking costs time only on the
*last* few tenths of a degree. The time to get within half a degree is
unchanged.

The same tuner was also run against 17 variants: Tado integrator strong,
weak or absent; leaky valve; 10 min water delay; fast, weak and strong
radiators; noisy and glitchy sensors; sun gains; 0 °C outside; default
180 s / 0.3 °C command settings; 0.5 °C command steps; an over-aggressive
starting point (Kp 2.0, Ki 0.005); and derivative learning switched off.
None left its bounds or needed a rollback. With derivative learning on,
overshoot ended at ≤ 0.2 °C in every variant except those in §7. With it off,
only Ki is tuned and overshoot fell less (1.04 → 0.71 °C). A deliberately
oscillating room (±0.65 °C, 9 h cycle) was calmed to about ±0.15 °C within a
week.

---

## 6. What stops it running away

Each item is covered by a test in `tests/test_autotune.py`.

1. **Hard bounds.** Kp 0.1–2.0, Ki 0.00005–0.005, Td 0–45 min, whatever the
   measurements say.
2. **Bounds around your values.** Kp stays within 0.25–2× of your Kp, and
   Ki within 0.05–2× of your Ki. If you set Ki = 0, it stays 0.
3. **Small steps.** Per update: Kp ×0.85 / ×1.10, Ki ×0.7 / ×1.2, Td ±50 %
   or 5 min.
4. **Rate limits.** At least 3 h between gentler changes, and 12 h between
   "more heat" changes (higher Kp/Ki, less braking).
5. **Confirmation for "more heat".** Two agreeing episodes are needed. One is
   enough for a gentler move. When unsure, it errs towards less heat, within
   the bounds above.
6. **Watchdog.** After every change, the next heat-up(s) are compared with
   the ones before. Overshoot up by > 0.2 °C, or a new oscillation, triggers
   a rollback. The old values are restored, the tuner freezes for 3 days,
   and that direction is blocked permanently for that value.
7. **Emergency detune.** A swing of ±0.3 °C or more cuts Kp and Ki at once,
   bypassing the rate limit.
8. **Robust measurements.** Median filters, Theil–Sen slopes,
   plausibility windows (θ 3–60 min, rate 0.2–10 °C/h), and minimum step
   sizes. Glitches are discarded rather than learned from.
9. **The brake cannot add heat.** It is one-sided and clamped by
   construction (`regulation.py`).
10. **Existing guards unchanged.** The integral deadband, decay, ±2 °C
    clamp, saturation freeze and the 5–25 °C command clamp.
11. **Isolation.** Learned values live in the restore-state data and are
    never written to the config entry (which would reload the integration).
    Changing Kp, Ki or the braking time in the options restarts learning
    from the new values. Switching auto-tune off reverts to the configured
    values immediately. *Reset auto-tune* does the same and starts over.
12. **Malformed stored state** (corrupt, wrong version, values out of
    bounds) falls back to the configured values.

Fuzz test: 25 seeds × 300 random, including absurd, episodes. Bounds and
step limits hold on every step.

---

## 7. What it cannot fix

- **A valve that does not close.** Heat keeps arriving whatever the demand.
  The tuner sees no peak, learns no coast, and leaves things alone
  (simulated: 0.65 → 0.54 °C).
- **Undersized radiators, or very cold weather.** Td runs to its 45 min cap
  and overshoot stays around 0.25 °C. The last 0.1 °C arrives later (in the
  0 °C-outside simulation the evening heat-up's half-degree time went from
  139 to 159 min).
- **Sun and other free heat.** A sunny heat-up looks like more coast. The
  median over 5 heat-ups and the bounded trims absorb it.
- **Rooms that are never heated from cold** (a constant setpoint, no
  schedule). Without heat-ups there is no dead time or coast to learn. Only
  the hold rules run.
- **Learning takes about a week.** With one or two heat-ups a day, the first
  useful changes come on day 1–2. The values settle in roughly 5–8 days.

---

## 8. Entities

| Entity | Meaning |
|---|---|
| `sensor.<name>_kp_in_use` | Kp the regulator uses now (learned or configured). Attributes: `configured`, `source` |
| `sensor.<name>_ki_in_use` | Ki in use |
| `sensor.<name>_braking_time_in_use` | Td in use, minutes |
| `sensor.<name>_auto_tune_status` | `disabled` / `learning` / `tuning` / `frozen`. Attributes carry the full summary: model, SIMC numbers, last change, last event, counters |
| `sensor.<name>_room_dead_time` | Learned θ, minutes |
| `sensor.<name>_radiator_coast_time` | Learned coast, minutes |
| `sensor.<name>_heat_up_rate` | Learned heating rate, °C/h |
| `sensor.<name>_last_heat_up_overshoot` | Overshoot of the last analysed heat-up, °C |
| `button.<name>_reset_auto_tune` | Forget learned values |

All value sensors are `state_class: measurement`, so Home Assistant keeps
long-term statistics of how the tuning evolves. The climate entity also
gains `d_correction_c` and `correction_td_min` attributes. The diagnostics
download includes the tuner's full state.

---

## 9. Code map

| File | Contains |
|---|---|
| `autotune.py` | Slope estimator, episode analysis, the tuner. No Home Assistant imports |
| `parameters.py` | `AutotuneConfig`: every bound, threshold and step size |
| `regulation.py` | The derivative brake |
| `climate_regulation.py` | Feeds the tuner once per cycle, applies its values |
| `tests/plant_sim.py` | Room + radiator + Tado TRV (with its own PI) + proxy simulator |
| `tests/test_autotune.py` | Unit, safety-contract, fuzz and closed-loop tests |
