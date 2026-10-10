"""Sensor entities for Tado X Proxy.

* boost and schedule-override countdowns;
* the controller gains in use (Kp, Ki, derivative time) and the auto-tuner's
  status and learned room model, as measurement sensors so Home Assistant
  keeps long-term statistics of how the tuning evolves.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .autotune import STATUSES
from .const import DOMAIN


@dataclass(frozen=True, kw_only=True)
class TuningSensorDescription(SensorEntityDescription):
    """A sensor read from the climate entity's tuning / auto-tune state."""

    value_fn: Callable[[Any], Any]
    attrs_fn: Callable[[Any], dict[str, Any]] | None = None
    # Model sensors are only meaningful while auto-tune is on.
    needs_autotune: bool = False


def _gain_attrs(name: str) -> Callable[[Any], dict[str, Any]]:
    def attrs(climate: Any) -> dict[str, Any]:
        configured = getattr(climate.configured_tuning, name)
        if name == "td_s":
            configured = round(configured / 60.0, 1)
        return {
            "configured": configured,
            "source": "auto-tune" if climate.autotune_enabled else "configured",
        }
    return attrs


def _summary(key: str) -> Callable[[Any], Any]:
    return lambda climate: climate.autotune_summary().get(key)


def _status_attrs(climate: Any) -> dict[str, Any]:
    s = dict(climate.autotune_summary())
    s.pop("status", None)
    return s


TUNING_SENSORS: tuple[TuningSensorDescription, ...] = (
    TuningSensorDescription(
        key="pi_kp",
        translation_key="pi_kp",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: round(c.active_tuning.kp, 3),
        attrs_fn=_gain_attrs("kp"),
    ),
    TuningSensorDescription(
        key="pi_ki",
        translation_key="pi_ki",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=5,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: round(c.active_tuning.ki, 6),
        attrs_fn=_gain_attrs("ki"),
    ),
    TuningSensorDescription(
        key="pi_td",
        translation_key="pi_td",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: round(c.active_tuning.td_s / 60.0, 1),
        attrs_fn=_gain_attrs("td_s"),
    ),
    TuningSensorDescription(
        key="autotune_status",
        translation_key="autotune_status",
        device_class=SensorDeviceClass.ENUM,
        options=STATUSES,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_summary("status"),
        attrs_fn=_status_attrs,
    ),
    TuningSensorDescription(
        key="autotune_dead_time",
        translation_key="autotune_dead_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_summary("dead_time_min"),
        needs_autotune=True,
    ),
    TuningSensorDescription(
        key="autotune_coast_time",
        translation_key="autotune_coast_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_summary("coast_time_min"),
        needs_autotune=True,
    ),
    TuningSensorDescription(
        key="autotune_heating_rate",
        translation_key="autotune_heating_rate",
        native_unit_of_measurement="°C/h",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_summary("heating_rate_c_per_h"),
        needs_autotune=True,
    ),
    TuningSensorDescription(
        key="autotune_last_overshoot",
        translation_key="autotune_last_overshoot",
        # A temperature *difference*: no temperature device class, so Home
        # Assistant does not apply a unit conversion offset to it.
        native_unit_of_measurement="°C",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_summary("last_overshoot_c"),
        needs_autotune=True,
    ),
)


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
    tuning = [TadoXProxyTuningSensor(coordinator, entry, d) for d in TUNING_SENSORS]
    # Written by the regulation cycle so a tuning change shows immediately.
    coordinator.tuning_entities = tuning
    async_add_entities([entity, override, *tuning])


class TadoXProxyTuningSensor(CoordinatorEntity, SensorEntity):
    """Controller gain or auto-tune diagnostic, read from the climate entity."""

    _attr_has_entity_name = True
    entity_description: TuningSensorDescription

    def __init__(self, coordinator, entry: ConfigEntry,
                 description: TuningSensorDescription) -> None:
        """Initialize the tuning sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info so this entity appears on the same device."""
        return DeviceInfo(identifiers={(DOMAIN, self._entry.entry_id)})

    @property
    def _climate(self) -> Any:
        return getattr(self.coordinator, "climate_entity", None)

    @property
    def available(self) -> bool:
        """Model sensors are unavailable while auto-tune is off."""
        climate = self._climate
        if climate is None:
            return False
        if self.entity_description.needs_autotune and not climate.autotune_enabled:
            return False
        return True

    @property
    def native_value(self) -> Any:
        """Return the current value from the climate entity."""
        climate = self._climate
        if climate is None:
            return None
        return self.entity_description.value_fn(climate)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the configured value / auto-tune detail."""
        climate = self._climate
        fn = self.entity_description.attrs_fn
        if climate is None or fn is None:
            return None
        return fn(climate)


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
