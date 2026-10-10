"""Button entities for Roomstat: resume the schedule, reset auto-tune."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Roomstat button entities."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        ResumeScheduleButton(coordinator, entry),
        ResetAutotuneButton(coordinator, entry),
    ])


class ResumeScheduleButton(CoordinatorEntity, ButtonEntity):
    """Ends a manual override and returns the thermostat to its schedule."""

    _attr_has_entity_name = True
    _attr_translation_key = "resume_schedule"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        """Initialize the resume-schedule button."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_resume_schedule"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info so this entity appears on the same device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
        )

    @property
    def available(self) -> bool:
        """Only available when a schedule helper is configured."""
        climate = getattr(self.coordinator, "climate_entity", None)
        return (
            super().available
            and climate is not None
            and climate.schedule_configured
        )

    async def async_press(self) -> None:
        """Resume the schedule (refused while summer mode is on)."""
        climate = self.coordinator.climate_entity
        await climate.async_resume_schedule()


class ResetAutotuneButton(CoordinatorEntity, ButtonEntity):
    """Forgets everything the auto-tuner learned for this room.

    The room goes straight back to the Kp / Ki / derivative time set in the
    options, and learning starts again from there.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "reset_autotune"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        """Initialize the reset button."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_reset_autotune"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info so this entity appears on the same device."""
        return DeviceInfo(identifiers={(DOMAIN, self._entry.entry_id)})

    @property
    def available(self) -> bool:
        """Only available while auto-tune is switched on."""
        climate = getattr(self.coordinator, "climate_entity", None)
        return super().available and climate is not None and climate.autotune_enabled

    async def async_press(self) -> None:
        """Reset the learned values."""
        await self.coordinator.climate_entity.async_reset_autotune()
