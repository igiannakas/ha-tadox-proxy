"""Summer-mode mixin for TadoXProxyClimate.

Summer mode is driven by an optional on/off helper (usually an
``input_boolean`` shared by every thermostat).  While it is on:

- the TRV is held at a fixed 5 °C in heat mode (no regulation),
- window, presence and follow-Tado automations are ignored,
- boost is cancelled,
- every change through Home Assistant (preset, temperature, HVAC mode) is
  refused with an error and the card is snapped back to the locked state,
- a change on the TRV itself (dial / Tado app) is pushed back to 5 °C on the
  next regulation cycle, honouring the command rate limit.

When the helper turns off, the thermostat returns to the schedule preset (or
COMFORT without a schedule) and the window /
presence sensors are evaluated again.
"""

from __future__ import annotations

import logging

from homeassistant.components.climate import HVACMode
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError

from .climate_controllers import resolve_summer_state, summer_enforcement_needed
from .const import (
    CONF_PRESENCE_AWAY_DELAY_S,
    CONF_PRESENCE_SENSOR_ID,
    CONF_SUMMER_MODE_ENTITY_ID,
    CONF_WINDOW_DELAY_S,
    CONF_WINDOW_SENSOR_ID,
    DOMAIN,
    PRESET_FROST_PROTECTION,
    safe_float,
)
from .parameters import FROST_PROTECT_C
from .regulation import RegulationState

_LOGGER = logging.getLogger(__name__)

# Fixed TRV setpoint while summer mode is on.
SUMMER_TARGET_C: float = FROST_PROTECT_C


class SummerMixin:
    """Summer-mode lock extracted from TadoXProxyClimate."""

    @property
    def summer_mode_active(self) -> bool:
        """Return True while summer mode locks the thermostat."""
        return self._summer_active

    # ------------------------------------------------------------------
    # Lock guard for user-facing service calls
    # ------------------------------------------------------------------

    def _summer_guard(self, action: str) -> None:
        """Refuse a change while summer mode is on.

        Before raising, the unchanged (locked) state is written with
        force_update so the frontend receives a fresh state and a card that
        optimistically showed the requested value snaps back to 5 °C.
        """
        if not self._summer_active:
            return
        _LOGGER.info("Summer mode is on: %s refused", action)
        self._attr_force_update = True
        try:
            self.async_write_ha_state()
        finally:
            self._attr_force_update = False
        raise ServiceValidationError(
            "Summer mode is on – this thermostat is locked at 5 °C.",
            translation_domain=DOMAIN,
            translation_key="summer_mode_locked",
        )

    # ------------------------------------------------------------------
    # Switch handling
    # ------------------------------------------------------------------

    @callback
    def _async_summer_changed(self, event) -> None:
        """React to the summer-mode helper turning on or off."""
        new_state = event.data.get("new_state")
        active = resolve_summer_state(
            new_state.state if new_state is not None else None, self._summer_active
        )
        if active == self._summer_active:
            return
        if active:
            self._enter_summer_mode(bypass_rate_limit=True)
            trigger = "summer_on"
        else:
            self._exit_summer_mode(rearm_sensors=True)
            trigger = "summer_off"
        self.async_write_ha_state()
        self.hass.async_create_task(self._async_regulation_cycle(trigger=trigger))

    def _enter_summer_mode(self, bypass_rate_limit: bool = False) -> None:
        """Lock the thermostat: cancel automations, show frost protection 5 °C."""
        self._summer_active = True
        self._window_ctrl.cancel_all()
        self._presence_ctrl.cancel_timer()
        self._presence_ctrl.restore()  # clears active flag and saved state
        if self._boost_cancel is not None:
            self._boost_cancel()
            self._boost_cancel = None
        self._boost_end_ts = 0.0
        self._schedule_ctrl.clear_override()
        self._hvac_mode = HVACMode.HEAT
        self._preset_mode = PRESET_FROST_PROTECTION
        self._target_temp = SUMMER_TARGET_C
        self._summer_bypass_rate_limit = bypass_rate_limit
        _LOGGER.info("Summer mode on: thermostat locked at %.1f °C", SUMMER_TARGET_C)

    def _exit_summer_mode(self, rearm_sensors: bool) -> None:
        """Unlock the thermostat and return to the schedule (else COMFORT)."""
        self._summer_active = False
        self._summer_bypass_rate_limit = False
        self._hvac_mode = HVACMode.HEAT
        self._preset_mode = self._fallback_preset()
        self._target_temp = self._get_preset_target(self._preset_mode)
        # Start regulation fresh: no integral from before summer, dt = 0.
        self._reg_state = RegulationState()
        self._last_regulation_ts = 0.0
        _LOGGER.info("Summer mode off: returning to %s", self._preset_mode)
        if rearm_sensors:
            self._summer_rearm_sensors()

    def _summer_rearm_sensors(self) -> None:
        """After summer mode, react to a window that is open / nobody home."""
        opts = self._config_entry.options
        window_sensor = opts.get(CONF_WINDOW_SENSOR_ID)
        if window_sensor:
            ws = self.hass.states.get(window_sensor)
            if ws is not None and ws.state == "on":
                self._window_ctrl.handle_window_opened(
                    self.hass,
                    opts.get(CONF_WINDOW_DELAY_S, 30),
                    self._async_window_action,
                )
        presence_sensor = opts.get(CONF_PRESENCE_SENSOR_ID)
        if presence_sensor:
            ps = self.hass.states.get(presence_sensor)
            if ps is not None and ps.state == "off":
                self._presence_ctrl.handle_presence_away(
                    self.hass,
                    opts.get(CONF_PRESENCE_AWAY_DELAY_S, 600),
                    self._async_presence_away_action,
                )

    def _startup_summer(self, persisted_summer: bool) -> bool:
        """Apply summer mode at startup.  Returns True when summer is active."""
        entity_id = self._config_entry.options.get(CONF_SUMMER_MODE_ENTITY_ID)
        if not entity_id:
            active = False
        else:
            st = self.hass.states.get(entity_id)
            active = resolve_summer_state(
                st.state if st is not None else None, persisted_summer
            )
        if active:
            # No rate-limit bypass: after a restart/reload the TRV is usually
            # already at 5 °C; the regular enforcement sends only if needed.
            self._enter_summer_mode(bypass_rate_limit=False)
        elif persisted_summer:
            # Switched off while HA was down (or the helper was removed from
            # the config).  The regular window/presence reconciliation runs
            # right after this, so no re-arming here.
            self._exit_summer_mode(rearm_sensors=False)
        return active

    # ------------------------------------------------------------------
    # Regulation replacement while summer mode is on
    # ------------------------------------------------------------------

    async def _async_summer_enforce(self, now: float) -> None:
        """Hold the TRV at the summer target (called by the regulation cycle)."""
        source = self._config_entry.data.get("source_entity_id")
        st = self.hass.states.get(source) if source else None
        trv_state = st.state if st is not None else None
        trv_setpoint = safe_float(st.attributes.get("temperature")) if st is not None else None

        needed = summer_enforcement_needed(trv_state, trv_setpoint, SUMMER_TARGET_C)
        time_since_last = now - self._last_command_sent_ts
        rate_limited = time_since_last < self._config.min_command_interval_s
        overlay_refresh_due = (
            self._overlay_refresh_s > 0 and time_since_last >= self._overlay_refresh_s
        )
        trv_available = trv_state is not None and trv_state not in ("unavailable", "unknown")

        send = False
        if needed and (self._summer_bypass_rate_limit or not rate_limited):
            send, reason = True, "summer_enforce"
        elif needed:
            remaining = int(self._config.min_command_interval_s - time_since_last)
            reason = f"summer_rate_limited({remaining}s)"
        elif overlay_refresh_due and trv_available:
            send, reason = True, "summer_overlay_refresh"
        elif not trv_available:
            reason = "summer_trv_unavailable"
        else:
            reason = "summer_locked"

        if send:
            await self._async_send_to_tado(SUMMER_TARGET_C)
            self._last_command_sent_ts = now
            self._summer_bypass_rate_limit = False
            self._last_reason = f"sent({reason})"
        else:
            self._last_reason = reason
        self._write_state_with_binary_sensor()
