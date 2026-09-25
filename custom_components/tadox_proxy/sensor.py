"""Sensor entities for Tado X Proxy: boost and schedule-override countdowns."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
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
    """Set up Tado X Proxy sensor entities."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entity = TadoXProxyBoostTimerSensor(coordinator, entry)
    coordinator.sensor_entity = entity
    override = TadoXProxyScheduleOverrideSensor(coordinator, entry)
    coordinator.schedule_sensor_entity = override
    async_add_entities([entity, override])


class TadoXProxyBoostTimerSensor(CoordinatorEntity, SensorEntity):
    """Sensor showing remaining boost timer minutes.

    Reports 0 when boost is not active, otherwise the remaining
    minutes rounded up to the next whole minute.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_suggested_display_precision = 0
    _attr_translation_key = "boost_remaining"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        """Initialize the boost timer sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_boost_remaining"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info so this entity appears on the same device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
        )

    @property
    def native_value(self) -> int:
        """Return remaining boost minutes."""
        climate = getattr(self.coordinator, "climate_entity", None)
        if climate is None:
            return 0
        return climate.boost_remaining_minutes


class TadoXProxyScheduleOverrideSensor(CoordinatorEntity, SensorEntity):
    """Minutes left before a manual change hands back to the schedule.

    0 when the schedule is being followed, or when the override has no timer
    (override duration 0 = until the schedule changes; manual Away).  The
    climate entity's ``schedule_override_active`` attribute tells those apart.
    Unavailable when no schedule helper is configured.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 0
    _attr_translation_key = "schedule_override_remaining"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        """Initialize the schedule override sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_schedule_override_remaining"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info so this entity appears on the same device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
        )

    @property
    def available(self) -> bool:
        """Only meaningful when a schedule helper is configured."""
        climate = getattr(self.coordinator, "climate_entity", None)
        return (
            super().available
            and climate is not None
            and climate.schedule_configured
        )

    @property
    def native_value(self) -> int:
        """Return remaining override minutes."""
        climate = getattr(self.coordinator, "climate_entity", None)
        if climate is None:
            return 0
        return climate.schedule_override_remaining_minutes
