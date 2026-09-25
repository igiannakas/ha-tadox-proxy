"""Schedule-following mixin for TadoXProxyClimate.

The proxy follows an external schedule helper (an ``input_select`` set by a
scheduler such as Scheduler card/component).  Its state is the preset the room
should be in: ``comfort``, ``night`` (= eco), ``away`` or ``frost_protection``.

Priority, highest first: summer mode → open window → presence away →
manual override → schedule.  While a window or presence automation is active,
schedule changes update the preset they will restore.

A manual change (preset, temperature, boost, physical dial when "follow
physical thermostat" is on) starts an *override*.  It ends at the next change
of the schedule preset, or after the configured override duration –
whichever comes first.  Duration 0 means "until the schedule changes".
Selecting Away by hand is sticky: it stays until the user changes it.
Selecting the preset the schedule currently asks for, the ``schedule``
pseudo-preset or the "Resume schedule" button ends the override.
"""

from __future__ import annotations

import datetime
import logging
import time

from homeassistant.components.climate import PRESET_AWAY, PRESET_COMFORT
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError

from .climate_controllers import parse_schedule_preset
from .const import CONF_SCHEDULE_ENTITY_ID, CONF_SCHEDULE_OVERRIDE_MIN, DOMAIN

_LOGGER = logging.getLogger(__name__)


class ScheduleMixin:
    """Schedule following extracted from TadoXProxyClimate."""

    # ------------------------------------------------------------------
    # Read-only helpers
    # ------------------------------------------------------------------

    @property
    def _schedule_entity_id(self) -> str | None:
        return self._config_entry.options.get(CONF_SCHEDULE_ENTITY_ID) or None

    @property
    def schedule_configured(self) -> bool:
        """True when a schedule helper is configured for this thermostat."""
        return self._schedule_entity_id is not None

    @property
    def schedule_override_remaining_minutes(self) -> int:
        """Minutes left on a timed override (0 when none or untimed)."""
        return self._schedule_ctrl.remaining_minutes(time.time())

    def _fallback_preset(self) -> str:
        """Preset to fall back to: the schedule's, else COMFORT."""
        if self.schedule_configured and self._schedule_ctrl.schedule_preset:
            return self._schedule_ctrl.schedule_preset
        return PRESET_COMFORT

    def _schedule_attributes(self) -> dict:
        ctrl = self._schedule_ctrl
        until = None
        if ctrl.override_active and ctrl.override_until is not None:
            until = datetime.datetime.fromtimestamp(
                ctrl.override_until, tz=datetime.UTC
            ).isoformat()
        return {
            "schedule_preset": ctrl.schedule_preset if self.schedule_configured else None,
            "schedule_override_active": self.schedule_configured and ctrl.override_active,
            "schedule_override_until": until,
        }

    # ------------------------------------------------------------------
    # Manual changes → overrides
    # ------------------------------------------------------------------

    def _schedule_note_manual_preset(self, preset_mode: str) -> None:
        """Book-keep a user preset change against the schedule."""
        if not self.schedule_configured:
            return
        ctrl = self._schedule_ctrl
        if preset_mode == PRESET_AWAY:
            self._schedule_start_override(sticky=True)
        elif preset_mode == ctrl.schedule_preset:
            if ctrl.override_active:
                _LOGGER.info("Schedule preset selected by hand – override ended")
            ctrl.clear_override()
        else:
            self._schedule_start_override()

    def _schedule_note_manual_temperature(self) -> None:
        """A manual temperature (slider / physical dial) overrides the schedule."""
        if self.schedule_configured:
            self._schedule_start_override()

    def _schedule_start_override(self, sticky: bool = False) -> None:
        duration = self._config_entry.options.get(CONF_SCHEDULE_OVERRIDE_MIN, 0) or 0
        self._schedule_ctrl.start_override(
            self.hass,
            float(duration),
            time.time(),
            self._async_schedule_override_expired,
            sticky=sticky,
        )
        _LOGGER.info(
            "Schedule override started (%s)",
            "sticky" if sticky else (f"{duration} min" if duration else "until next change"),
        )

    async def _async_schedule_override_expired(self, _now) -> None:
        """Override timer ran out: return to the schedule."""
        self._schedule_ctrl.timer_fired()
        _LOGGER.info("Schedule override expired")
        await self._async_follow_schedule(trigger="override_expired")

    async def async_resume_schedule(self) -> None:
        """End any override and return to the schedule's current preset."""
        self._summer_guard("resume schedule")
        if not self.schedule_configured:
            raise ServiceValidationError(
                "No schedule helper is configured for this thermostat.",
                translation_domain=DOMAIN,
                translation_key="no_schedule",
            )
        self._schedule_ctrl.clear_override()
        _LOGGER.info("Schedule resumed")
        await self._async_follow_schedule(trigger="resume_schedule")

    async def _async_follow_schedule(self, trigger: str) -> None:
        """Apply the schedule's preset (routed via window/presence if active)."""
        preset = self._schedule_ctrl.schedule_preset
        if self._summer_active or not self.schedule_configured or preset is None:
            self._write_state_with_binary_sensor()
            return
        await self._async_apply_preset(preset, trigger=trigger)

    # ------------------------------------------------------------------
    # Schedule helper changes
    # ------------------------------------------------------------------

    @callback
    def _async_schedule_changed(self, event) -> None:
        """React to the schedule helper changing."""
        new_state = event.data.get("new_state")
        state = new_state.state if new_state is not None else None
        preset = parse_schedule_preset(state)
        if preset is None:
            if state not in (None, "unavailable", "unknown"):
                _LOGGER.warning(
                    "Schedule helper %s has unsupported value %r – ignored",
                    self._schedule_entity_id,
                    state,
                )
            return
        apply_now = self._schedule_ctrl.schedule_changed(preset)
        if apply_now and not self._summer_active:
            _LOGGER.info("Schedule: switching to %s", preset)
            self.hass.async_create_task(
                self._async_apply_preset(preset, trigger="schedule")
            )
        else:
            self._write_state_with_binary_sensor()

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def _startup_schedule(self, persisted, summer_on: bool) -> None:
        """Restore override state and align with the helper at startup."""
        ctrl = self._schedule_ctrl
        entity_id = self._schedule_entity_id
        if entity_id is None:
            ctrl.clear_override()
            ctrl.schedule_preset = None
            return

        ctrl.load(persisted)
        st = self.hass.states.get(entity_id)
        current = parse_schedule_preset(st.state if st is not None else None)
        if current is not None and current != ctrl.schedule_preset:
            # Schedule moved on while HA was down (or first run).
            ctrl.schedule_preset = current
            if ctrl.override_active and not ctrl.override_sticky:
                ctrl.clear_override()

        if summer_on:
            ctrl.clear_override()
            return

        if ctrl.rearm(self.hass, time.time(), self._async_schedule_override_expired):
            _LOGGER.info("Startup: schedule override expired while HA was down")

        if not ctrl.override_active and ctrl.schedule_preset is not None:
            # Follow the schedule without side effects (not added yet).
            self._route_preset(ctrl.schedule_preset)
