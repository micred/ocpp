"""Common classes for charge points of all OCPP versions."""

import asyncio
from collections import defaultdict
from collections.abc import MutableMapping
import contextlib
from dataclasses import dataclass, field
from enum import Enum
import logging
from math import sqrt
import secrets
import string
import time

from homeassistant.components.persistent_notification import DOMAIN as PN_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import STATE_OK, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.const import UnitOfTime
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers.dispatcher import async_dispatcher_send
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import WebSocketException
from websockets.protocol import State

from ocpp.charge_point import ChargePoint as cp
from ocpp.v16 import call as callv16
from ocpp.v16 import call_result as call_resultv16
from ocpp.v16.enums import (
    AuthorizationStatus,
    Measurand,
    Phase,
    ReadingContext,
)
from ocpp.v201 import call as callv201
from ocpp.v201 import call_result as call_resultv201
from ocpp.messages import CallError
from ocpp.exceptions import NotImplementedError

from .enums import (
    HAChargerDetails as cdet,
    HAChargerSession as csess,
    HAChargerStatuses as cstat,
    OcppMisc as om,
    Profiles as prof,
)

from .const import (
    CentralSystemSettings,
    ChargerSystemSettings,
    CONF_AUTH_LIST,
    CONF_AUTH_STATUS,
    CONF_DEFAULT_AUTH_STATUS,
    CONF_ID_TAG,
    CONF_MONITORED_VARIABLES,
    CONF_NUM_CONNECTORS,
    CONF_CPIDS,
    CONFIG,
    DATA_UPDATED,
    DEFAULT_CHARGE_RATE_MIN_UPDATE_INTERVAL,
    DEFAULT_CHARGE_RATE_STABILITY_SAMPLES,
    DEFAULT_CHARGE_RATE_TOLERANCE,
    DEFAULT_ENERGY_UNIT,
    DEFAULT_NUM_CONNECTORS,
    DEFAULT_POWER_UNIT,
    DEFAULT_MEASURAND,
    DOMAIN,
    HA_ENERGY_UNIT,
    HA_POWER_UNIT,
    UNITS_OCCP_TO_HA,
)

TIME_MINUTES = UnitOfTime.MINUTES
_LOGGER: logging.Logger = logging.getLogger(__package__)
CHARGE_RATE_ENFORCEMENT_ATTR = "charge_rate_enforcement"
CHARGE_RATE_ENFORCEMENT_SLEEP = 10


class Metric:
    """Metric class."""

    def __init__(self, value, unit):
        """Initialize a Metric."""
        self._value = value
        self._unit = unit
        self._extra_attr = {}

    @property
    def value(self):
        """Get the value of the metric."""
        return self._value

    @value.setter
    def value(self, value):
        """Set the value of the metric."""
        self._value = value

    @property
    def unit(self):
        """Get the unit of the metric."""
        return self._unit

    @unit.setter
    def unit(self, unit: str):
        """Set the unit of the metric."""
        self._unit = unit

    @property
    def ha_unit(self):
        """Get the home assistant unit of the metric."""
        return UNITS_OCCP_TO_HA.get(self._unit, self._unit)

    @property
    def extra_attr(self):
        """Get the extra attributes of the metric."""
        return self._extra_attr

    @extra_attr.setter
    def extra_attr(self, extra_attr: dict):
        """Set the unit of the metric."""
        self._extra_attr = extra_attr


class _ConnectorAwareMetrics(MutableMapping):
    """Backwards compatible mapping for metrics.

    - m["Power.Active.Import"]         -> Metric for connector 0 (flat access)
    - m[(2, "Power.Active.Import")]    -> Metric for connector 2 (per connector)
    - m[2]                             -> dict[str -> Metric] for connector 2

    Iteration, len, keys(), values(), items() operate on connector 0 (flat view).
    """

    def __init__(self):
        self._by_conn = defaultdict(lambda: defaultdict(lambda: Metric(None, None)))

    def __getitem__(self, key):
        if isinstance(key, tuple) and len(key) == 2 and isinstance(key[0], int):
            conn, meas = key
            return self._by_conn[conn][meas]
        if isinstance(key, int):
            return self._by_conn[key]
        return self._by_conn[0][key]

    def __setitem__(self, key, value):
        if isinstance(key, tuple) and len(key) == 2 and isinstance(key[0], int):
            conn, meas = key
            if not isinstance(value, Metric):
                raise TypeError("Metric assignment must be a Metric instance.")
            self._by_conn[conn][meas] = value
            return
        if isinstance(key, int):
            if not isinstance(value, dict):
                raise TypeError("Connector mapping must be dict[str, Metric].")
            self._by_conn[key] = value
            return
        if not isinstance(value, Metric):
            raise TypeError("Metric assignment must be a Metric instance.")
        self._by_conn[0][key] = value

    def __delitem__(self, key):
        if isinstance(key, tuple) and len(key) == 2 and isinstance(key[0], int):
            conn, meas = key
            del self._by_conn[conn][meas]
            return
        if isinstance(key, int):
            del self._by_conn[key]
            return
        del self._by_conn[0][key]

    def __iter__(self):
        return iter(self._by_conn[0])

    def __len__(self):
        return len(self._by_conn[0])

    def get(self, key, default=None):
        if key in self:
            return self[key]
        return default

    def keys(self):
        return self._by_conn[0].keys()

    def values(self):
        return self._by_conn[0].values()

    def items(self):
        return self._by_conn[0].items()

    def clear(self):
        self._by_conn.clear()

    def __contains__(self, key):
        if isinstance(key, tuple) and len(key) == 2 and isinstance(key[0], int):
            conn, meas = key
            return meas in self._by_conn.get(conn, {})
        if isinstance(key, int):
            return key in self._by_conn
        return key in self._by_conn[0]


class OcppVersion(str, Enum):
    """OCPP version choice."""

    V16 = "1.6"
    V201 = "2.0.1"
    V21 = "2.1"


class SetVariableResult(Enum):
    """A response to successful SetVariable call."""

    accepted = 0
    reboot_required = 1


@dataclass
class MeasurandValue:
    """Version-independent representation of a measurand."""

    measurand: str
    value: float
    phase: str | None
    unit: str | None
    context: str | None
    location: str | None


@dataclass
class ChargeRateEnforcementState:
    """Runtime state for closed-loop charge-rate enforcement."""

    target_amps: float | None = None
    limit_watts: int = 22000
    conn_id: int = 1
    profile: dict | None = None
    tolerance_amps: float = DEFAULT_CHARGE_RATE_TOLERANCE
    stability_samples: int = DEFAULT_CHARGE_RATE_STABILITY_SAMPLES
    min_update_interval: int = DEFAULT_CHARGE_RATE_MIN_UPDATE_INTERVAL
    last_sent_amps: float | None = None
    last_send_ts: float = 0.0
    task: asyncio.Task | None = None
    backoff: float = 1.0
    samples: list[float] = field(default_factory=list)
    stable_count: int = 0
    last_status: str = "idle"
    last_reconnects: int = 0
    last_boot_generation: int = 0


class ChargePoint(cp):
    """Server side representation of a charger."""

    def __init__(
        self,
        id,  # is charger cp_id not HA cpid
        connection,
        version: OcppVersion,
        hass: HomeAssistant,
        entry: ConfigEntry,
        central: CentralSystemSettings,
        charger: ChargerSystemSettings,
    ):
        """Instantiate a ChargePoint."""

        super().__init__(id, connection, 10)
        if version == OcppVersion.V16:
            self._call = callv16
            self._call_result = call_resultv16
            self._ocpp_version = "1.6"
        elif version == OcppVersion.V201:
            self._call = callv201
            self._call_result = call_resultv201
            self._ocpp_version = "2.0.1"
        elif version == OcppVersion.V21:
            self._call = callv201
            self._call_result = call_resultv201
            self._ocpp_version = "2.1"

        for action in self.route_map:
            self.route_map[action]["_skip_schema_validation"] = (
                charger.skip_schema_validation
            )

        self.hass = hass
        self.entry = entry
        self.cs_settings = central
        self.settings = charger
        self.status = "init"
        # Indicates if the charger requires a reboot to apply new
        # configuration.
        self._requires_reboot = False
        self.preparing = asyncio.Event()
        self.active_transaction_id: int = 0
        self.triggered_boot_notification = False
        self.received_boot_notification = False
        self.post_connect_success = False
        self.tasks = None
        self._charger_reports_session_energy = False
        self._charge_rate_enforcement: dict[int, ChargeRateEnforcementState] = {}
        self._charge_rate_boot_generation = 0

        # Connector-aware, but backwards compatible:
        self._metrics: _ConnectorAwareMetrics = _ConnectorAwareMetrics()

        # Init standard metrics for connector 0
        self._metrics[(0, cdet.identifier.value)].value = id
        self._metrics[(0, cstat.reconnects.value)].value = 0

        self._attr_supported_features = prof.NONE
        alphabet = string.ascii_uppercase + string.digits
        self._remote_id_tag = "".join(secrets.choice(alphabet) for i in range(20))
        self.num_connectors: int = DEFAULT_NUM_CONNECTORS

    def _init_connector_slots(self, conn_id: int) -> None:
        """Ensure connector-scoped metrics exist and carry the right units."""
        _ = self._metrics[(conn_id, cstat.status_connector.value)]
        _ = self._metrics[(conn_id, cstat.error_code_connector.value)]
        _ = self._metrics[(conn_id, csess.transaction_id.value)]

        self._metrics[(conn_id, csess.session_time.value)].unit = TIME_MINUTES
        self._metrics[(conn_id, csess.session_energy.value)].unit = HA_ENERGY_UNIT
        self._metrics[(conn_id, csess.meter_start.value)].unit = HA_ENERGY_UNIT

    async def get_number_of_connectors(self) -> int:
        """Return number of connectors on this charger."""
        return self.num_connectors

    async def get_heartbeat_interval(self):
        """Retrieve heartbeat interval from the charger and store it."""
        pass

    async def get_supported_measurands(self) -> str:
        """Get comma-separated list of measurands supported by the charger."""
        return ""

    async def set_standard_configuration(self):
        """Send configuration values to the charger."""
        pass

    async def get_supported_features(self) -> prof:
        """Get features supported by the charger."""
        return prof.NONE

    async def fetch_supported_features(self):
        """Get supported features."""
        self._attr_supported_features = await self.get_supported_features()
        self._metrics[(0, cdet.features.value)].value = self._attr_supported_features
        _LOGGER.debug(
            "Feature profiles returned: %s", self._attr_supported_features.labels()
        )

    async def post_connect(self):
        """Logic to be executed right after a charger connects."""
        try:
            self.status = STATE_OK
            await self.fetch_supported_features()
            self.num_connectors = await self.get_number_of_connectors()
            for conn in range(1, self.num_connectors + 1):
                self._init_connector_slots(conn)
            self._metrics[(0, cdet.connectors.value)].value = self.num_connectors
            await self.get_heartbeat_interval()

            accepted_measurands: str = await self.get_supported_measurands()
            updated_entry = {**self.entry.data}
            for i in range(len(updated_entry[CONF_CPIDS])):
                if self.id in updated_entry[CONF_CPIDS][i]:
                    s = updated_entry[CONF_CPIDS][i][self.id]
                    if s.get(CONF_MONITORED_VARIABLES) != accepted_measurands or s.get(
                        CONF_NUM_CONNECTORS
                    ) != int(self.num_connectors):
                        s[CONF_MONITORED_VARIABLES] = accepted_measurands
                        s[CONF_NUM_CONNECTORS] = int(self.num_connectors)
                    break
            # if an entry differs this will unload/reload and stop/restart the central system/websocket
            self.hass.config_entries.async_update_entry(self.entry, data=updated_entry)

            await self.set_standard_configuration()

            self.post_connect_success = True
            _LOGGER.debug("'%s' post connection setup completed successfully", self.id)

            # nice to have, but not needed for integration to function
            # and can cause issues with some chargers
            try:
                await self.set_availability()
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                _LOGGER.debug("post_connect: set_availability ignored error: %s", ex)

            if prof.REM in self._attr_supported_features:
                if self.received_boot_notification is False:
                    try:
                        await asyncio.wait_for(
                            self.trigger_boot_notification(), timeout=3
                        )
                    except Exception as ex:
                        _LOGGER.debug("trigger_boot_notification ignored: %s", ex)
                try:
                    await asyncio.wait_for(
                        self.trigger_status_notification(), timeout=3
                    )
                except Exception as ex:
                    _LOGGER.debug("trigger_status_notification ignored: %s", ex)

            # Ensure HA states are correct immediately after connection
            self.hass.async_create_task(self.update(self.settings.cpid))

        except Exception as e:
            _LOGGER.debug("post_connect aborted non-fatally: %s", e)

    async def trigger_boot_notification(self):
        """Trigger a boot notification."""
        pass

    async def trigger_status_notification(self):
        """Trigger status notifications for all connectors."""
        pass

    async def trigger_custom_message(
        self,
        requested_message: str = "StatusNotification",
    ):
        """Trigger message request with a custom message."""
        pass

    async def clear_profile(self):
        """Clear all charging profiles."""
        pass

    async def set_charge_rate(
        self,
        limit_amps: int = 32,
        limit_watts: int = 22000,
        conn_id: int = 0,
        profile: dict | None = None,
        enforce: bool = False,
        tolerance_amps: float = DEFAULT_CHARGE_RATE_TOLERANCE,
        stability_samples: int = DEFAULT_CHARGE_RATE_STABILITY_SAMPLES,
        min_update_interval: int = DEFAULT_CHARGE_RATE_MIN_UPDATE_INTERVAL,
    ):
        """Set a charging profile with defined limit."""
        pass

    def _target_connector_id(self, conn_id: int | None) -> int:
        """Resolve connector 0/None to the connector affected by rate limits."""
        try:
            conn = int(conn_id or 0)
        except Exception:
            conn = 0
        return conn if conn > 0 else 1

    def _get_charge_rate_enforcement_state(
        self, conn_id: int
    ) -> ChargeRateEnforcementState:
        """Return enforcement state for a connector, creating it if needed."""
        conn = self._target_connector_id(conn_id)
        if not hasattr(self, "_charge_rate_enforcement"):
            self._charge_rate_enforcement = {}
        if conn not in self._charge_rate_enforcement:
            self._charge_rate_enforcement[conn] = ChargeRateEnforcementState(
                conn_id=conn,
                last_reconnects=self._charge_rate_reconnect_count(),
                last_boot_generation=getattr(self, "_charge_rate_boot_generation", 0),
            )
        return self._charge_rate_enforcement[conn]

    async def _cancel_charge_rate_enforcement_state(
        self, state: ChargeRateEnforcementState
    ) -> None:
        """Cancel one enforcement task and wait for cancellation to settle."""
        task = state.task
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def cancel_charge_rate_enforcement(self, conn_id: int | None = None) -> None:
        """Cancel enforcement tasks, optionally for a single connector."""
        states = getattr(self, "_charge_rate_enforcement", {})
        if conn_id is None:
            selected = list(states.values())
        else:
            selected = [states.get(self._target_connector_id(conn_id))]

        for state in selected:
            if state is None:
                continue
            if state.task is not None and not state.task.done():
                state.task.cancel()
            state.task = None
            state.last_status = "cancelled"

    def _coerce_current_sample(self, value) -> float | None:
        """Return numeric metric values; ignore unavailable/non-numeric samples."""
        if value in (None, "", STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        if isinstance(value, str) and value.strip().lower() in {
            "unknown",
            "unavailable",
        }:
            return None
        try:
            sample = float(value)
        except (TypeError, ValueError):
            return None
        if sample != sample:
            return None
        return sample

    def _metric_value_and_unit(
        self, conn_id: int, measurand: str
    ) -> tuple[float | None, str | None]:
        """Read a connector-aware metric with legacy fallbacks."""
        conn = self._target_connector_id(conn_id)
        candidates = [(conn, measurand), (0, measurand), measurand]
        if conn != 1:
            candidates.append((1, measurand))

        for key in candidates:
            try:
                metric = self._metrics.get(key)
            except Exception:
                metric = None
            if metric is None:
                continue
            sample = self._coerce_current_sample(getattr(metric, "value", None))
            if sample is not None:
                return sample, getattr(metric, "unit", None)
        return None, None

    def _measure_current_amps(self, conn_id: int) -> float | None:
        """Measure current, preferring Current.Import over power / voltage."""
        current, _unit = self._metric_value_and_unit(
            conn_id, Measurand.current_import.value
        )
        if current is not None:
            return current

        power, power_unit = self._metric_value_and_unit(
            conn_id, Measurand.power_active_import.value
        )
        voltage, _voltage_unit = self._metric_value_and_unit(
            conn_id, Measurand.voltage.value
        )
        if power is None or voltage in (None, 0):
            return None

        unit = str(power_unit or "").lower()
        power_watts = power * 1000 if unit == "kw" else power
        return power_watts / voltage

    def _charge_rate_reconnect_count(self) -> int:
        """Return the last recorded reconnect count."""
        try:
            return int(self._metrics[(0, cstat.reconnects.value)].value or 0)
        except Exception:
            return 0

    def _charge_rate_enforcement_connected(self) -> bool:
        """Return whether enforcement may send OCPP calls now."""
        if getattr(self, "status", None) != STATE_OK:
            return False
        if not getattr(self, "post_connect_success", False):
            return False
        connection = getattr(self, "_connection", None)
        if connection is None:
            return True
        return getattr(connection, "state", State.OPEN) is State.OPEN

    def _charge_rate_stats(
        self, state: ChargeRateEnforcementState
    ) -> dict[str, float | int | str | None]:
        """Build rolling enforcement statistics."""
        samples = list(state.samples)
        count = len(samples)
        mean = sum(samples) / count if count else None
        stdev = (
            sqrt(sum((sample - mean) ** 2 for sample in samples) / count)
            if count and mean is not None
            else None
        )
        return {
            "mean": round(mean, 3) if mean is not None else None,
            "min": min(samples) if count else None,
            "max": max(samples) if count else None,
            "stdev": round(stdev, 3) if stdev is not None else None,
            "sample_count": count,
            "target_amps": state.target_amps,
            "last_status": state.last_status,
        }

    def _publish_charge_rate_enforcement_stats(self, conn_id: int) -> None:
        """Expose enforcement stats on the Current.Import metric attributes."""
        conn = self._target_connector_id(conn_id)
        state = self._get_charge_rate_enforcement_state(conn)
        metric = self._metrics[(conn, Measurand.current_import.value)]
        extra_attr = metric.extra_attr
        if not isinstance(extra_attr, dict):
            extra_attr = {}
        extra_attr[CHARGE_RATE_ENFORCEMENT_ATTR] = self._charge_rate_stats(state)
        metric.extra_attr = extra_attr

    def get_charge_rate_enforcement_stats(
        self, conn_id: int = 1
    ) -> dict[str, float | int | str | None]:
        """Return the latest rolling enforcement statistics."""
        conn = self._target_connector_id(conn_id)
        state = self._get_charge_rate_enforcement_state(conn)
        return self._charge_rate_stats(state)

    def _record_charge_rate_sample(
        self, state: ChargeRateEnforcementState, sample: float
    ) -> None:
        """Record a valid current sample and update the stability counter."""
        state.samples.append(sample)
        window = max(1, int(state.stability_samples or 1))
        state.samples = state.samples[-window:]
        if state.target_amps is not None and abs(sample - state.target_amps) <= float(
            state.tolerance_amps
        ):
            state.stable_count += 1
        else:
            state.stable_count = 0

        if state.stable_count >= window:
            state.last_status = "stable"
        else:
            state.last_status = "outside_tolerance"

    async def _charge_rate_enforcement_tick(self, conn_id: int, send_once) -> bool:
        """Run one deterministic enforcement iteration."""
        conn = self._target_connector_id(conn_id)
        state = self._get_charge_rate_enforcement_state(conn)
        if state.target_amps is None:
            return False

        if not self._charge_rate_enforcement_connected():
            state.last_status = "disconnected"
            self._publish_charge_rate_enforcement_stats(conn)
            return False

        current_reconnects = self._charge_rate_reconnect_count()
        current_boot_generation = getattr(self, "_charge_rate_boot_generation", 0)
        needs_reapply = (
            current_reconnects != state.last_reconnects
            or current_boot_generation != state.last_boot_generation
        )

        sample = self._measure_current_amps(conn)
        if sample is not None:
            self._record_charge_rate_sample(state, sample)

        now = time.monotonic()
        window = max(1, int(state.stability_samples or 1))
        target_changed = (
            state.last_sent_amps is None
            or abs(float(state.last_sent_amps) - float(state.target_amps)) > 0.001
        )
        unstable_window = (
            sample is not None
            and len(state.samples) >= window
            and state.stable_count < window
            and now - state.last_send_ts >= int(state.min_update_interval or 0)
        )

        if not (target_changed or needs_reapply or unstable_window):
            self._publish_charge_rate_enforcement_stats(conn)
            return False

        try:
            ok = await send_once(
                limit_amps=state.target_amps,
                limit_watts=state.limit_watts,
                conn_id=state.conn_id,
                profile=state.profile,
            )
        except Exception as ex:
            ok = False
            _LOGGER.debug("Charge-rate enforcement send failed: %s", ex)

        if ok:
            state.last_sent_amps = state.target_amps
            state.last_send_ts = now
            state.backoff = 1.0
            state.last_reconnects = current_reconnects
            state.last_boot_generation = current_boot_generation
            state.last_status = "resent" if unstable_window and not needs_reapply else "sent"
        else:
            state.last_status = "send_failed"
            state.backoff = min(
                max(float(state.backoff or 1.0) * 2, 1.0),
                max(float(state.min_update_interval or 1), 1.0),
            )

        self._publish_charge_rate_enforcement_stats(conn)
        return bool(ok)

    async def _charge_rate_enforcement_loop(self, conn_id: int, send_once) -> None:
        """Poll metrics and enforce the target without blind repeated sends."""
        conn = self._target_connector_id(conn_id)
        try:
            while True:
                state = self._get_charge_rate_enforcement_state(conn)
                sleep_for = min(
                    CHARGE_RATE_ENFORCEMENT_SLEEP,
                    max(1, int(state.min_update_interval or 1)),
                )
                await asyncio.sleep(sleep_for)
                if state.task is not asyncio.current_task():
                    return
                await self._charge_rate_enforcement_tick(conn, send_once)
                if state.last_status == "send_failed":
                    await asyncio.sleep(max(1.0, float(state.backoff or 1.0)))
        except asyncio.CancelledError:
            raise

    async def _start_charge_rate_enforcement(
        self,
        *,
        limit_amps: int | float = 32,
        limit_watts: int = 22000,
        conn_id: int = 0,
        profile: dict | None = None,
        tolerance_amps: float = DEFAULT_CHARGE_RATE_TOLERANCE,
        stability_samples: int = DEFAULT_CHARGE_RATE_STABILITY_SAMPLES,
        min_update_interval: int = DEFAULT_CHARGE_RATE_MIN_UPDATE_INTERVAL,
        send_once=None,
    ) -> bool:
        """Start or update a connector's charge-rate enforcement loop."""
        conn = self._target_connector_id(conn_id)
        state = self._get_charge_rate_enforcement_state(conn)
        target = float(limit_amps)
        same_target = (
            state.target_amps is not None
            and abs(float(state.target_amps) - target) <= 0.001
            and state.conn_id == conn
            and state.task is not None
            and not state.task.done()
        )

        state.target_amps = target
        state.limit_watts = int(limit_watts)
        state.conn_id = conn
        state.profile = profile
        state.tolerance_amps = float(tolerance_amps)
        state.stability_samples = max(1, int(stability_samples))
        state.min_update_interval = max(1, int(min_update_interval))

        if same_target:
            self._publish_charge_rate_enforcement_stats(conn)
            return True

        await self._cancel_charge_rate_enforcement_state(state)
        state.task = None
        state.samples = []
        state.stable_count = 0

        if send_once is None:
            return False

        ok = True
        if self._charge_rate_enforcement_connected():
            ok = await self._charge_rate_enforcement_tick(conn, send_once)
        else:
            state.last_status = "disconnected"
            self._publish_charge_rate_enforcement_stats(conn)

        state.task = asyncio.create_task(
            self._charge_rate_enforcement_loop(conn, send_once)
        )
        return bool(ok)

    async def set_availability(self, state: bool = True) -> bool:
        """Change availability."""
        return False

    async def start_transaction(self, connector_id: int = 1) -> bool:
        """Remote start a transaction."""
        return False

    async def stop_transaction(self, connector_id: int | None = None) -> bool:
        """Request remote stop of current transaction.

        Leaves charger in finishing state until unplugged.
        Use reset() to make the charger available again for remote start
        """
        return False

    async def reset(self, typ: str | None = None) -> bool:
        """Hard reset charger unless soft reset requested."""
        return False

    async def unlock(self, connector_id: int = 1) -> bool:
        """Unlock charger if requested."""
        return False

    async def update_firmware(self, firmware_url: str, wait_time: int = 0):
        """Update charger with new firmware if available.

        - firmware_url is the http or https url of the new firmware
        - wait_time is hours from now to wait before install
        """
        pass

    async def get_diagnostics(self, upload_url: str):
        """Upload diagnostic data to server from charger."""
        pass

    async def data_transfer(self, vendor_id: str, message_id: str = "", data: str = ""):
        """Request vendor specific data transfer from charger."""
        pass

    async def get_configuration(self, key: str = "") -> str | dict | None:
        """Get Configuration of charger for supported keys else return None."""
        return None

    async def configure(self, key: str, value: str) -> SetVariableResult | None:
        """Configure charger by setting the key to target value."""
        return None

    async def _get_specific_response(self, unique_id, timeout):
        # The ocpp library silences CallErrors by default. See
        # https://github.com/mobilityhouse/ocpp/issues/104.
        # This code 'unsilences' CallErrors by raising them as exception
        # upon receiving.
        resp = await super()._get_specific_response(unique_id, timeout)

        if isinstance(resp, CallError):
            raise resp.to_exception()

        return resp

    async def monitor_connection(self):
        """Monitor the connection, by measuring the connection latency."""
        self._metrics[(0, cstat.latency_ping.value)].unit = "ms"
        self._metrics[(0, cstat.latency_pong.value)].unit = "ms"
        connection = self._connection
        timeout_counter = 0

        # Add backstop to start post connect for non-compliant chargers
        # after 10s to allow for when a boot notification has not been received
        await asyncio.sleep(10)
        if not self.post_connect_success:
            self.hass.async_create_task(self.post_connect())

        while connection.state is State.OPEN:
            try:
                await asyncio.sleep(self.cs_settings.websocket_ping_interval)
                time0 = time.perf_counter()
                latency_ping = self.cs_settings.websocket_ping_timeout * 1000
                latency_pong = self.cs_settings.websocket_ping_timeout * 1000
                pong_waiter = await asyncio.wait_for(
                    connection.ping(), timeout=self.cs_settings.websocket_ping_timeout
                )
                time1 = time.perf_counter()
                latency_ping = round(time1 - time0, 3) * 1000

                await asyncio.wait_for(
                    pong_waiter, timeout=self.cs_settings.websocket_ping_timeout
                )
                timeout_counter = 0
                time2 = time.perf_counter()
                latency_pong = round(time2 - time1, 3) * 1000

                _LOGGER.debug(
                    f"Connection latency from '{self.cs_settings.csid}' to '{self.id}': "
                    f"ping={latency_ping} ms, pong={latency_pong} ms",
                )
                self._metrics[(0, cstat.latency_ping.value)].value = latency_ping
                self._metrics[(0, cstat.latency_pong.value)].value = latency_pong

            except TimeoutError as timeout_exception:
                timeout_counter += 1
                _LOGGER.debug(
                    f"Connection latency from '{self.cs_settings.csid}' to '{self.id}': "
                    f"ping={latency_ping} ms, pong={latency_pong} ms",
                )
                self._metrics[(0, cstat.latency_ping.value)].value = latency_ping
                self._metrics[(0, cstat.latency_pong.value)].value = latency_pong

                if timeout_counter > self.cs_settings.websocket_ping_tries:
                    _LOGGER.debug(
                        f"Connection to '{self.id}' timed out after '{self.cs_settings.websocket_ping_tries}' ping tries",
                    )
                    raise timeout_exception
                else:
                    continue
            except Exception as ex:
                _LOGGER.debug(f"monitor_connection stopping due to exception: {ex}")
                break

    async def _handle_call(self, msg):
        try:
            await super()._handle_call(msg)
        except NotImplementedError as e:
            response = msg.create_call_error(e).to_json()
            await self._send(response)

    async def start(self):
        """Start charge point."""
        await self.run([super().start(), self.monitor_connection()])

    async def run(self, tasks):
        """Run a specified list of tasks."""
        self.tasks = [asyncio.ensure_future(task) for task in tasks]
        try:
            await asyncio.gather(*self.tasks)
        except TimeoutError:
            pass
        except WebSocketException as websocket_exception:
            _LOGGER.debug(f"Connection closed to '{self.id}': {websocket_exception}")
        except Exception as other_exception:
            _LOGGER.error(
                f"Unexpected exception in connection to '{self.id}': '{other_exception}'",
                exc_info=True,
            )
        finally:
            await self.stop()

    async def stop(self):
        """Close connection and cancel ongoing tasks."""
        self.status = STATE_UNAVAILABLE
        if self._connection.state is State.OPEN:
            _LOGGER.debug(f"Closing websocket to '{self.id}'")
            await self._connection.close()
        for task in self.tasks:
            task.cancel()

    async def reconnect(self, connection: ServerConnection):
        """Reconnect charge point."""
        _LOGGER.debug(f"Reconnect websocket to {self.id}")

        await self.stop()
        self.status = STATE_OK
        self._connection = connection
        self.post_connect_success = False
        self.received_boot_notification = False
        self._metrics[(0, cstat.reconnects.value)].value += 1
        # post connect now handled on receiving boot notification or with backstop in monitor connection
        await self.run([super().start(), self.monitor_connection()])

    async def async_update_device_info(
        self, serial: str, vendor: str, model: str, firmware_version: str
    ):
        """Update device info asynchronously."""

        self._metrics[(0, cdet.model.value)].value = model
        self._metrics[(0, cdet.vendor.value)].value = vendor
        self._metrics[(0, cdet.firmware_version.value)].value = firmware_version
        self._metrics[(0, cdet.serial.value)].value = serial

        identifiers = {(DOMAIN, self.id), (DOMAIN, self.settings.cpid)}

        registry = device_registry.async_get(self.hass)
        registry.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers=identifiers,
            manufacturer=vendor,
            model=model,
            sw_version=firmware_version,
        )

    def _register_boot_notification(self):
        self._charge_rate_boot_generation = (
            getattr(self, "_charge_rate_boot_generation", 0) + 1
        )
        if self.triggered_boot_notification is False:
            self.hass.async_create_task(self.notify_ha(f"Charger {self.id} rebooted"))
            if not self.post_connect_success:
                self.hass.async_create_task(self.post_connect())

    async def update(self, cpid: str):
        """Update sensors values in HA (charger + connector child devices)."""
        er = entity_registry.async_get(self.hass)
        dr = device_registry.async_get(self.hass)

        identifiers = {(DOMAIN, cpid), (DOMAIN, self.id)}
        root_dev = dr.async_get_device(identifiers)
        if root_dev is None:
            return

        to_visit: list[str] = [root_dev.id]
        visited: set[str] = set()
        active_entities: set[str] = set()

        while to_visit:
            dev_id = to_visit.pop(0)
            if dev_id in visited:
                continue
            visited.add(dev_id)

            # Collect enabled and currently loaded entities for this device
            for ent in entity_registry.async_entries_for_device(er, dev_id):
                if getattr(ent, "disabled", False) or getattr(ent, "disabled_by", None):
                    continue
                if self.hass.states.get(ent.entity_id) is None:
                    continue
                active_entities.add(ent.entity_id)

            for dev in dr.devices.values():
                if dev.via_device_id == dev_id and dev.id not in visited:
                    to_visit.append(dev.id)

        async_dispatcher_send(self.hass, DATA_UPDATED, active_entities)

    def get_authorization_status(self, id_tag):
        """Get the authorization status for an id_tag."""
        # authorize if its the tag of this charger used for remote start_transaction
        if id_tag == self._remote_id_tag:
            return AuthorizationStatus.accepted.value
        config = self.hass.data[DOMAIN].get(CONFIG, {})
        # get the default authorization status. Use accept if not configured
        default_auth_status = config.get(
            CONF_DEFAULT_AUTH_STATUS, AuthorizationStatus.accepted.value
        )
        # get the authorization list
        auth_list = config.get(CONF_AUTH_LIST, {})
        # search for the entry, based on the id_tag
        auth_status = None
        for auth_entry in auth_list:
            id_entry = auth_entry.get(CONF_ID_TAG, None)
            if id_tag == id_entry:
                # get the authorization status, use the default if not configured
                auth_status = auth_entry.get(CONF_AUTH_STATUS, default_auth_status)
                _LOGGER.debug(
                    f"id_tag='{id_tag}' found in auth_list, authorization_status='{auth_status}'"
                )
                break

        if auth_status is None:
            auth_status = default_auth_status
            _LOGGER.debug(
                f"id_tag='{id_tag}' not found in auth_list, default authorization_status='{auth_status}'"
            )
        return auth_status

    def process_phases(self, data: list[MeasurandValue], connector_id: int = 0):
        """Process per-phase MeterValues and aggregate them into per-connector metrics.

        Rules:
        - Voltage: average (L1-N/L2-N/L3-N or L-L divided by √3); fall back to averaging L1/L2/L3 if needed.
        - Current.*: average of L1/L2/L3 (ignore N).
        - Power.Factor: **average** of L1/L2/L3 (ignore N). *Do not sum; unit is dimensionless and may be missing.*
        - Other (e.g. Power.Active.*): sum of L1/L2/L3 (ignore N).
        """
        # For single-connector chargers, use connector 1.
        n_connectors = getattr(self, CONF_NUM_CONNECTORS, DEFAULT_NUM_CONNECTORS) or 1
        if connector_id in (None, 0):
            target_cid = 1 if n_connectors == 1 else 0
        else:
            try:
                target_cid = int(connector_id)
            except Exception:
                target_cid = 1 if n_connectors == 1 else 0

        def average_of_nonzero(values: list[float]) -> float:
            """Average only non-zero values; return 0.0 if all are zero or list is empty."""
            nonzero = [v for v in values if v != 0.0]
            return (sum(nonzero) / len(nonzero)) if nonzero else 0.0

        measurand_data: dict[str, dict[str, float]] = {}

        for item in data:
            # create ordered Dict for each measurand, eg {"voltage":{"unit":"V","L1-N":"230"...}}
            measurand = item.measurand
            phase = item.phase
            value = item.value
            unit = item.unit
            context = item.context

            if measurand is None or phase is None:
                continue

            if measurand not in measurand_data:
                measurand_data[measurand] = {}

            if unit is not None:
                measurand_data[measurand][om.unit.value] = unit
                self._metrics[(target_cid, measurand)].unit = unit
                self._metrics[(target_cid, measurand)].extra_attr[om.unit.value] = unit

            measurand_data[measurand][phase] = value
            self._metrics[(target_cid, measurand)].extra_attr[phase] = value
            if context is not None:
                self._metrics[(target_cid, measurand)].extra_attr[om.context.value] = (
                    context
                )

        line_phases_all = [
            Phase.l1.value,
            Phase.l2.value,
            Phase.l3.value,
            Phase.n.value,
        ]
        phases_l123 = [Phase.l1.value, Phase.l2.value, Phase.l3.value]
        line_to_neutral_phases = [Phase.l1_n.value, Phase.l2_n.value, Phase.l3_n.value]
        line_to_line_phases = [Phase.l1_l2.value, Phase.l2_l3.value, Phase.l3_l1.value]

        def _avg_l123(phase_info: dict) -> float:
            return average_of_nonzero(
                [phase_info.get(phase, 0.0) for phase in phases_l123]
            )

        def _sum_l123(phase_info: dict) -> float:
            return sum(phase_info.get(phase, 0.0) for phase in phases_l123)

        for metric, phase_info in measurand_data.items():
            metric_value: float | None = None
            mname = str(metric)

            # --- THE NEUTRAL SHIELD ---
            # If the charger sends the "N" phase on its own, skip it to prevent overwriting the real voltage.
            active_phases = set(phase_info.keys()) - {"unit"}
            if active_phases == {"N"}:
                continue
            # --------------------------

            if metric in [Measurand.voltage.value]:
                if not phase_info.keys().isdisjoint(line_to_neutral_phases):
                    # Line to neutral voltages are averaged
                    metric_value = average_of_nonzero(
                        [phase_info.get(phase, 0.0) for phase in line_to_neutral_phases]
                    )
                elif not phase_info.keys().isdisjoint(line_to_line_phases):
                    # Line to line voltages are averaged and converted to line to neutral
                    metric_value = average_of_nonzero(
                        [phase_info.get(phase, 0.0) for phase in line_to_line_phases]
                    ) / sqrt(3)
                elif not phase_info.keys().isdisjoint(line_phases_all):
                    # Workaround for chargers that don't follow engineering convention
                    # Assumes voltages are line to neutral
                    metric_value = _avg_l123(phase_info)

            else:
                is_current = mname.lower().startswith("current")
                if is_current:
                    # Current.* shown per phase -> avg of L1/L2/L3, ignore N
                    if not phase_info.keys().isdisjoint(phases_l123):
                        metric_value = _avg_l123(phase_info)
                    elif not phase_info.keys().isdisjoint(line_to_neutral_phases):
                        # Workaround for some chargers that erroneously use line to neutral for current
                        metric_value = average_of_nonzero(
                            [
                                phase_info.get(phase, 0.0)
                                for phase in line_to_neutral_phases
                            ]
                        )

                # Special-case: Power.Factor must be averaged, never summed
                elif metric == Measurand.power_factor.value:
                    if not phase_info.keys().isdisjoint(phases_l123):
                        metric_value = _avg_l123(phase_info)
                    elif not phase_info.keys().isdisjoint(line_to_neutral_phases):
                        metric_value = average_of_nonzero(
                            [phase_info.get(p, 0.0) for p in line_to_neutral_phases]
                        )
                    # If only a single phase value exists, just pass it through
                    else:
                        metric_value = next(
                            (v for k, v in phase_info.items() if k != om.unit.value),
                            None,
                        )

                else:
                    # Other (e.g. Power.*): total is sum over phases
                    if not phase_info.keys().isdisjoint(phases_l123):
                        metric_value = _sum_l123(phase_info)
                    elif not phase_info.keys().isdisjoint(line_to_neutral_phases):
                        metric_value = sum(
                            phase_info.get(phase, 0.0)
                            for phase in line_to_neutral_phases
                        )

            if metric_value is not None:
                metric_unit = phase_info.get(om.unit.value)

                if metric_unit == DEFAULT_POWER_UNIT:
                    self._metrics[(target_cid, metric)].value = metric_value / 1000
                    self._metrics[(target_cid, metric)].unit = HA_POWER_UNIT
                elif metric_unit == DEFAULT_ENERGY_UNIT:
                    self._metrics[(target_cid, metric)].value = metric_value / 1000
                    self._metrics[(target_cid, metric)].unit = HA_ENERGY_UNIT
                else:
                    self._metrics[(target_cid, metric)].value = metric_value
                    self._metrics[(target_cid, metric)].unit = metric_unit

    @staticmethod
    def get_energy_kwh(measurand_value: MeasurandValue) -> float:
        """Convert energy value from charger to kWh."""
        if (measurand_value.unit == "Wh") or (measurand_value.unit is None):
            return measurand_value.value / 1000
        return measurand_value.value

    def process_measurands(
        self,
        meter_values: list[list[MeasurandValue]],
        is_transaction: bool,
        connector_id: int = 0,
    ):
        """Process all values from OCPP 1.6 MeterValues or OCPP 2.0.1 TransactionEvent."""

        for bucket in meter_values:
            # --- Preselect best EAIR in this bucket (ignore Transaction.Begin) ---
            best_eair_idx = None
            best_pr = -1
            best_val = None
            for j, sv in enumerate(bucket):
                meas = sv.measurand if sv.measurand is not None else DEFAULT_MEASURAND
                if meas != DEFAULT_MEASURAND:
                    continue
                ctx = sv.context or ReadingContext.sample_periodic.value
                # Always ignore Transaction.Begin for EAIR (prevents resets to 0)
                if ctx == ReadingContext.transaction_begin.value:
                    continue
                try:
                    kwh = float(
                        ChargePoint.get_energy_kwh(
                            MeasurandValue(
                                meas,
                                sv.value,
                                sv.phase,
                                sv.unit,
                                ctx,
                                sv.location,
                            )
                        )
                    )
                except Exception:
                    continue
                if kwh < 0.0 or kwh != kwh:
                    continue
                pr = 0
                if ctx == ReadingContext.transaction_end.value:
                    pr = 3
                elif ctx == ReadingContext.sample_periodic.value:
                    pr = 2
                elif ctx == ReadingContext.sample_clock.value:
                    pr = 1
                if (pr > best_pr) or (
                    pr == best_pr and (best_val is None or kwh > best_val)
                ):
                    best_pr = pr
                    best_val = kwh
                    best_eair_idx = j

            unprocessed: list[MeasurandValue] = []

            # Pre-scan: Count how many distinct phases are reported for the main energy register
            eair_phases = set()
            for v in bucket:
                v_measurand = getattr(v, "measurand", None) or DEFAULT_MEASURAND
                if v_measurand == DEFAULT_MEASURAND and getattr(v, "phase", None):
                    eair_phases.add(v.phase)

            for idx, sampled_value in enumerate(bucket):
                measurand = sampled_value.measurand
                value = sampled_value.value
                unit = sampled_value.unit
                phase = sampled_value.phase
                location = sampled_value.location
                context = sampled_value.context or ReadingContext.sample_periodic.value

                # Strip the phase tag ONLY if a single-phase charger sends an isolated L1 energy reading.
                # If multiple phases exist (e.g., L1, L2), leave them intact so process_phases() can sum them.
                normalized_measurand = measurand or DEFAULT_MEASURAND
                if (
                    normalized_measurand == DEFAULT_MEASURAND
                    and phase == Phase.l1.value
                    and len(eair_phases) == 1
                ):
                    phase = None

                # Backwards compatibility
                if sampled_value.measurand is None:
                    measurand = DEFAULT_MEASURAND
                    unit = unit or DEFAULT_ENERGY_UNIT

                if measurand == DEFAULT_MEASURAND and unit is None:
                    unit = DEFAULT_ENERGY_UNIT

                # Normalize units
                if unit == DEFAULT_ENERGY_UNIT:
                    value = ChargePoint.get_energy_kwh(
                        MeasurandValue(measurand, value, phase, unit, context, location)
                    )
                    unit = HA_ENERGY_UNIT

                if unit == DEFAULT_POWER_UNIT:
                    value = value / 1000
                    unit = HA_POWER_UNIT

                if self._metrics[(connector_id, csess.meter_start.value)].value == 0:
                    # Charger reports Energy.Active.Import.Register directly as Session energy for transactions.
                    self._charger_reports_session_energy = True

                if phase is None:
                    is_eair = measurand == DEFAULT_MEASURAND

                    # Determine if this is a single-connector charger (only if explicitly known)
                    try:
                        n_connectors = int(getattr(self, "num_connectors", 1) or 1)
                    except Exception:
                        n_connectors = 1

                    single = n_connectors == 1

                    # Choose target connector id
                    if is_eair:
                        if connector_id and connector_id > 0:
                            # Always honor a positive connector_id for EAIR, even without txId
                            target_cid = connector_id
                        else:
                            # connector_id == 0 or missing → map based on topology
                            target_cid = 1 if single else 0
                    else:
                        target_cid = connector_id

                    # For EAIR: process only the best candidate in this bucket, skip others (incl. Transaction.Begin)
                    if is_eair and idx != best_eair_idx:
                        continue

                    # Determine whether to skip writing EAIR to the main metric:
                    # - Skip only if this is an EAIR reading,
                    # - AND the charger reports session energy (meter_start == 0),
                    # - AND the reading belongs to an active transaction.
                    #
                    # Reason: in this situation, the EAIR value represents **session energy** for the current transaction,
                    # not the lifetime total meter. Writing it to the main metric would overwrite the true cumulative
                    # energy with a session-only value. For all other cases (non-EAIR readings or non-transaction readings),
                    # it is safe to write the metric normally.
                    skip_eair = (
                        is_eair
                        and self._charger_reports_session_energy
                        and is_transaction
                    )

                    if not skip_eair:
                        # Normal write
                        self._metrics[(target_cid, measurand)].value = value
                        self._metrics[(target_cid, measurand)].unit = unit
                        if location is not None:
                            self._metrics[(target_cid, measurand)].extra_attr[
                                om.location.value
                            ] = location
                        self._metrics[(target_cid, measurand)].extra_attr[
                            om.context.value
                        ] = context

                    # Session handling, only for EAIR during a transaction (per-connector)
                    if is_transaction and is_eair:
                        if self._charger_reports_session_energy:
                            # Charger reports session energy directly; ignore Transaction.Begin.
                            if context != ReadingContext.transaction_begin.value:
                                self._metrics[
                                    (target_cid, csess.session_energy.value)
                                ].value = value
                                self._metrics[
                                    (target_cid, csess.session_energy.value)
                                ].unit = unit
                                self._metrics[
                                    (target_cid, csess.session_energy.value)
                                ].extra_attr[cstat.id_tag.name] = self._metrics[
                                    (target_cid, cstat.id_tag.value)
                                ].value
                        else:
                            # Initialize baseline on first tx-bound EAIR; then derive Session = EAIR - meter_start.
                            ms_metric = self._metrics[(target_cid, csess.meter_start)]
                            if ms_metric.value is None:
                                ms_metric.value = value
                                ms_metric.unit = unit
                                self._metrics[
                                    (target_cid, csess.session_energy.value)
                                ].value = 0.0
                                self._metrics[
                                    (target_cid, csess.session_energy.value)
                                ].unit = unit
                            elif ms_metric.unit == unit:
                                self._metrics[
                                    (target_cid, csess.session_energy.value)
                                ].value = round(1000 * (value - ms_metric.value)) / 1000
                                self._metrics[
                                    (target_cid, csess.session_energy.value)
                                ].unit = unit
                else:
                    unprocessed.append(sampled_value)

            try:
                self.process_phases(unprocessed, connector_id)
            except TypeError:
                self.process_phases(unprocessed)

    @property
    def supported_features(self) -> int:
        """Flag of Ocpp features that are supported."""
        # Tests (and some external callers) may set supported features as a
        # `set` of `Profiles` members. Normalize to an IntFlag value so
        # callers can consistently perform bitwise operations or membership
        # checks.
        if isinstance(self._attr_supported_features, set):
            flags = prof.NONE
            for p in self._attr_supported_features:
                try:
                    flags |= p
                except Exception:
                    # ignore non-Profiles items
                    continue
            return flags
        return self._attr_supported_features

    def get_ha_metric(self, measurand: str, connector_id: int | None = None):
        """Return last known value in HA for given measurand, or None if not available."""
        base = self.settings.cpid.lower()
        meas_slug = measurand.lower().replace(".", "_")

        # Build list of possible sensor entity IDs.
        # Include connector-specific ID if applicable, then the generic one as fallback.
        candidates: list[str] = []
        if connector_id and connector_id > 0:
            candidates.append(f"sensor.{base}_connector_{connector_id}_{meas_slug}")
        candidates.append(f"sensor.{base}_{meas_slug}")

        # Return the first valid state found among candidates.
        for entity_id in candidates:
            try:
                st = self.hass.states.get(entity_id)
            except Exception as e:
                _LOGGER.debug("Error getting entity %s from HA: %s", entity_id, e)
                st = None

            if st and st.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
                return st.state

        return None

    async def notify_ha(self, msg: str, title: str = "Ocpp integration"):
        """Notify user via HA web frontend."""
        await self.hass.services.async_call(
            PN_DOMAIN,
            "create",
            service_data={
                "title": title,
                "message": msg,
            },
            blocking=False,
        )
        return True
