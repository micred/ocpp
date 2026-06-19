"""Tests for closed-loop OCPP 1.6 charge-rate enforcement."""

from types import SimpleNamespace
import asyncio

import pytest
from homeassistant.const import STATE_OK, STATE_UNAVAILABLE
from websockets.protocol import State

from custom_components.ocpp import chargepoint as cp_mod
from custom_components.ocpp.chargepoint import Metric
from custom_components.ocpp.enums import ConfigurationKey as ckey
from custom_components.ocpp.enums import HAChargerStatuses as cstat
from custom_components.ocpp.enums import Profiles as prof
from custom_components.ocpp.ocppv16 import ChargePoint as ChargePointv16
from ocpp.v16.enums import ChargingProfileStatus
from ocpp.v16.enums import Measurand


@pytest.fixture
def cp_v16():
    """Provide a minimally initialized v1.6 ChargePoint."""
    cp = object.__new__(ChargePointv16)  # type: ignore[misc]
    cp.id = "CP_enforce"
    cp._attr_supported_features = prof.SMART
    cp._ocpp_version = "1.6"
    cp.active_transaction_id = 0
    cp._active_tx = {}
    cp.status = STATE_OK
    cp.post_connect_success = True
    cp.num_connectors = 1
    cp._connection = SimpleNamespace(state=State.OPEN)
    cp._metrics = cp_mod._ConnectorAwareMetrics()
    cp._metrics[(0, cstat.reconnects.value)].value = 0
    cp.hass = SimpleNamespace(async_create_task=lambda coro: None)
    cp.settings = SimpleNamespace(cpid="test_cpid")
    return cp


async def _accepted_get_configuration(key: str = "") -> str:
    if key == ckey.charging_schedule_allowed_charging_rate_unit.value:
        return "A"
    if key == ckey.charge_profile_max_stack_level.value:
        return "2"
    return ""


@pytest.mark.asyncio
async def test_set_charge_rate_without_enforcement_sends_once(cp_v16, monkeypatch):
    """Default behavior remains a one-shot SetChargingProfile call."""
    sent = []

    async def fake_call(req):
        sent.append(req)
        return SimpleNamespace(status=ChargingProfileStatus.accepted)

    async def fake_notify(*_args, **_kwargs):
        return True

    monkeypatch.setattr(cp_v16, "get_configuration", _accepted_get_configuration)
    monkeypatch.setattr(cp_v16, "call", fake_call)
    monkeypatch.setattr(cp_v16, "notify_ha", fake_notify)

    ok = await cp_v16.set_charge_rate(limit_amps=16, conn_id=1, enforce=False)

    assert ok is True
    assert len(sent) == 1
    assert getattr(cp_v16, "_charge_rate_enforcement", {}) == {}


@pytest.mark.asyncio
async def test_enforced_repeated_target_is_debounced(cp_v16, monkeypatch):
    """Repeating the same enforced target does not resend immediately."""
    sent = []

    async def fake_send_once(**kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(cp_v16, "_send_charge_rate_once", fake_send_once, raising=False)

    assert await cp_v16.set_charge_rate(
        limit_amps=16,
        conn_id=1,
        enforce=True,
        tolerance_amps=1.0,
        stability_samples=2,
        min_update_interval=60,
    )
    first_task = cp_v16._get_charge_rate_enforcement_state(1).task

    assert await cp_v16.set_charge_rate(
        limit_amps=16,
        conn_id=1,
        enforce=True,
        tolerance_amps=1.0,
        stability_samples=2,
        min_update_interval=60,
    )
    second_task = cp_v16._get_charge_rate_enforcement_state(1).task

    assert sent == [
        {
            "limit_amps": 16,
            "limit_watts": 22000,
            "conn_id": 1,
            "profile": None,
        }
    ]
    assert second_task is first_task

    if second_task is not None:
        second_task.cancel()


@pytest.mark.asyncio
async def test_enforced_target_change_cancels_old_task(cp_v16, monkeypatch):
    """A newer target replaces the prior enforcement loop."""
    sent = []

    async def fake_send_once(**kwargs):
        sent.append(kwargs["limit_amps"])
        return True

    monkeypatch.setattr(cp_v16, "_send_charge_rate_once", fake_send_once, raising=False)

    await cp_v16.set_charge_rate(limit_amps=16, conn_id=1, enforce=True)
    first_task = cp_v16._get_charge_rate_enforcement_state(1).task

    await cp_v16.set_charge_rate(limit_amps=10, conn_id=1, enforce=True)
    second_task = cp_v16._get_charge_rate_enforcement_state(1).task

    assert sent == [16, 10]
    assert second_task is not first_task
    assert first_task.cancelled() or first_task.done()

    if second_task is not None:
        second_task.cancel()


@pytest.mark.asyncio
async def test_stable_samples_mark_enforcement_stable(cp_v16):
    """Consecutive in-tolerance samples mark enforcement stable and expose stats."""
    state = cp_v16._get_charge_rate_enforcement_state(1)
    state.target_amps = 16.0
    state.last_sent_amps = 16.0
    state.tolerance_amps = 1.0
    state.stability_samples = 3
    state.min_update_interval = 60

    async def fake_send_once(**_kwargs):
        pytest.fail("stable samples must not resend")

    for sample in (15.5, 16.25, 15.75):
        cp_v16._metrics[(1, Measurand.current_import.value)] = Metric(sample, "A")
        await cp_v16._charge_rate_enforcement_tick(1, fake_send_once)

    stats = cp_v16.get_charge_rate_enforcement_stats(1)
    assert stats["last_status"] == "stable"
    assert stats["sample_count"] == 3
    assert stats["target_amps"] == 16.0
    assert stats["mean"] == pytest.approx(15.833, abs=0.001)
    assert stats["min"] == 15.5
    assert stats["max"] == 16.25
    assert stats["stdev"] == pytest.approx(0.312, abs=0.001)

    attr = cp_v16._metrics[(1, Measurand.current_import.value)].extra_attr
    assert attr["charge_rate_enforcement"]["last_status"] == "stable"


@pytest.mark.asyncio
async def test_unstable_samples_resend_after_minimum_interval(
    cp_v16, monkeypatch
):
    """Out-of-tolerance samples trigger one resend after the stability window."""
    now = 1000.0
    monkeypatch.setattr(cp_mod.time, "monotonic", lambda: now)

    state = cp_v16._get_charge_rate_enforcement_state(1)
    state.target_amps = 16.0
    state.last_sent_amps = 16.0
    state.last_send_ts = 900.0
    state.tolerance_amps = 1.0
    state.stability_samples = 2
    state.min_update_interval = 60

    sent = []

    async def fake_send_once(**kwargs):
        sent.append(kwargs["limit_amps"])
        return True

    cp_v16._metrics[(1, Measurand.current_import.value)] = Metric(20.0, "A")
    await cp_v16._charge_rate_enforcement_tick(1, fake_send_once)
    assert sent == []

    cp_v16._metrics[(1, Measurand.current_import.value)] = Metric(21.0, "A")
    await cp_v16._charge_rate_enforcement_tick(1, fake_send_once)

    assert sent == [16.0]
    assert cp_v16.get_charge_rate_enforcement_stats(1)["last_status"] == "resent"


@pytest.mark.asyncio
async def test_disconnect_pauses_and_reconnect_reapplies_once(cp_v16):
    """Enforcement pauses while unavailable and reapplies once after reconnect."""
    state = cp_v16._get_charge_rate_enforcement_state(1)
    state.target_amps = 16.0
    state.last_sent_amps = 16.0
    state.tolerance_amps = 1.0
    state.stability_samples = 1
    state.min_update_interval = 60
    state.last_reconnects = 0

    sent = []

    async def fake_send_once(**kwargs):
        sent.append(kwargs["limit_amps"])
        return True

    cp_v16.status = STATE_UNAVAILABLE
    cp_v16._connection.state = State.CLOSED
    await cp_v16._charge_rate_enforcement_tick(1, fake_send_once)

    assert sent == []
    assert cp_v16.get_charge_rate_enforcement_stats(1)["last_status"] == "disconnected"

    cp_v16.status = STATE_OK
    cp_v16._connection.state = State.OPEN
    cp_v16.post_connect_success = True
    cp_v16._metrics[(0, cstat.reconnects.value)].value = 1

    await cp_v16._charge_rate_enforcement_tick(1, fake_send_once)
    await cp_v16._charge_rate_enforcement_tick(1, fake_send_once)

    assert sent == [16.0]
    assert cp_v16.get_charge_rate_enforcement_stats(1)["last_status"] == "sent"


def test_current_measurement_prefers_current_import_and_falls_back(cp_v16):
    """Current.Import is preferred; otherwise derive amps from power / voltage."""
    cp_v16._metrics[(1, Measurand.current_import.value)] = Metric(12.0, "A")
    cp_v16._metrics[(1, Measurand.power_active_import.value)] = Metric(6.9, "kW")
    cp_v16._metrics[(1, Measurand.voltage.value)] = Metric(230.0, "V")

    assert cp_v16._measure_current_amps(1) == 12.0

    cp_v16._metrics[(1, Measurand.current_import.value)].value = None

    assert cp_v16._measure_current_amps(1) == pytest.approx(30.0)


def test_juicebox_meter_payload_updates_current_power_and_voltage(cp_v16):
    """JuiceBox local meter payloads expose the real per-line charging current."""
    payload = {
        "task": [
            {"name": "Line current L1", "value": "11.56", "suffix": "A"},
            {"name": "Line current L2", "value": "0.0", "suffix": "A"},
            {"name": "Line current L3", "value": "0.0", "suffix": "A"},
            {"name": "Line voltage L1", "value": "227.7", "suffix": "V"},
            {"name": "Line voltage L2", "value": "0.0", "suffix": "V"},
            {"name": "Line voltage L3", "value": "0.0", "suffix": "V"},
            {"name": "Active power L1", "value": "2.308", "suffix": "kW"},
            {"name": "Active power L2", "value": "0.0", "suffix": "kW"},
            {"name": "Active power L3", "value": "0.0", "suffix": "kW"},
        ]
    }

    assert cp_v16._apply_juicebox_meter_payload(1, payload) == pytest.approx(11.56)
    assert cp_v16._metrics[(1, Measurand.current_import.value)].value == pytest.approx(
        11.56
    )
    assert cp_v16._metrics[(1, Measurand.current_import.value)].unit == "A"
    assert cp_v16._metrics[(1, Measurand.current_import.value)].extra_attr["L1"] == (
        pytest.approx(11.56)
    )
    assert cp_v16._metrics[(1, Measurand.power_active_import.value)].value == (
        pytest.approx(2.308)
    )
    assert cp_v16._metrics[(1, Measurand.voltage.value)].value == pytest.approx(227.7)


@pytest.mark.asyncio
async def test_juicebox_meter_fallback_replaces_zero_ocpp_current(
    cp_v16, monkeypatch
):
    """JuiceBox enforcement uses the local meter when core OCPP samples are zero."""
    cp_v16._metrics[(1, Measurand.current_import.value)] = Metric(0.0, "A")
    cp_v16._charge_point_vendor = "ENEL"
    cp_v16._charge_point_model = "JuiceBox30_V1"
    cp_v16._connection.remote_address = ("192.168.0.8", 12345)

    def fake_fetch(conn_id):
        assert conn_id == 1
        return 11.56

    monkeypatch.setattr(cp_v16, "_fetch_juicebox_meter_current_amps", fake_fetch)

    sample = await cp_v16._measure_current_amps_for_enforcement(1)

    assert sample == pytest.approx(11.56)


def test_enforcement_helpers_ignore_bad_samples_and_handle_fallbacks(cp_v16):
    """Helper methods tolerate unavailable metrics and legacy edge cases."""
    assert cp_v16._target_connector_id(object()) == 1

    state = cp_v16._get_charge_rate_enforcement_state(0)
    assert state.conn_id == 1

    for bad in (None, "", "unknown", "unavailable", "not-a-number", float("nan")):
        assert cp_v16._coerce_current_sample(bad) is None

    cp_v16._metrics[(1, Measurand.current_import.value)] = Metric(None, "A")
    cp_v16._metrics[(1, Measurand.power_active_import.value)] = Metric(None, "W")
    cp_v16._metrics[(1, Measurand.voltage.value)] = Metric(230.0, "V")
    assert cp_v16._measure_current_amps(1) is None

    cp_v16._metrics[(2, Measurand.power_active_import.value)] = Metric(None, "W")
    cp_v16._metrics[(1, Measurand.power_active_import.value)] = Metric(6900.0, "W")
    cp_v16._metrics[(1, Measurand.voltage.value)] = Metric(230.0, "V")
    assert cp_v16._measure_current_amps(2) == pytest.approx(30.0)

    cp_v16._connection = None
    cp_v16.status = STATE_OK
    cp_v16.post_connect_success = True
    assert cp_v16._charge_rate_enforcement_connected() is True

    cp_v16._metrics = object()
    assert cp_v16._charge_rate_reconnect_count() == 0


def test_publish_stats_replaces_non_dict_extra_attrs(cp_v16):
    """Publishing stats tolerates a malformed existing extra_attr value."""
    state = cp_v16._get_charge_rate_enforcement_state(1)
    state.target_amps = 16.0
    state.samples = [15.0]
    metric = cp_v16._metrics[(1, Measurand.current_import.value)]
    metric.extra_attr = None

    cp_v16._publish_charge_rate_enforcement_stats(1)

    assert metric.extra_attr["charge_rate_enforcement"]["target_amps"] == 16.0


@pytest.mark.asyncio
async def test_tick_without_target_and_send_exception(cp_v16):
    """No-target ticks are no-ops; send exceptions set failure/backoff state."""
    async def fail_send_once(**_kwargs):
        raise RuntimeError("send failed")

    assert await cp_v16._charge_rate_enforcement_tick(1, fail_send_once) is False

    state = cp_v16._get_charge_rate_enforcement_state(1)
    state.target_amps = 16.0
    state.min_update_interval = 60

    assert await cp_v16._charge_rate_enforcement_tick(1, fail_send_once) is False
    assert state.last_status == "send_failed"
    assert state.backoff == 2.0


@pytest.mark.asyncio
async def test_start_enforcement_handles_missing_sender_and_disconnected(cp_v16):
    """Starting enforcement handles missing sender and disconnected startup."""
    assert (
        await cp_v16._start_charge_rate_enforcement(
            limit_amps=16,
            conn_id=1,
            send_once=None,
        )
        is False
    )

    sent = []

    async def fake_send_once(**kwargs):
        sent.append(kwargs)
        return True

    cp_v16.status = STATE_UNAVAILABLE
    cp_v16._connection.state = State.CLOSED
    ok = await cp_v16._start_charge_rate_enforcement(
        limit_amps=16,
        conn_id=1,
        send_once=fake_send_once,
    )

    state = cp_v16._get_charge_rate_enforcement_state(1)
    assert ok is True
    assert sent == []
    assert state.last_status == "disconnected"

    if state.task is not None:
        state.task.cancel()


@pytest.mark.asyncio
async def test_cancel_all_enforcement_tasks(cp_v16):
    """Cancelling without connector cancels all active enforcement tasks."""
    async def sleeper():
        await asyncio.sleep(60)

    state = cp_v16._get_charge_rate_enforcement_state(1)
    state.task = asyncio.create_task(sleeper())

    cp_v16.cancel_charge_rate_enforcement()
    await asyncio.sleep(0)

    assert state.task is None
    assert state.last_status == "cancelled"


@pytest.mark.asyncio
async def test_enforcement_loop_exits_when_task_replaced(cp_v16, monkeypatch):
    """The loop exits when its state no longer points at the current task."""
    original_sleep = cp_mod.asyncio.sleep
    state = cp_v16._get_charge_rate_enforcement_state(1)
    state.target_amps = 16.0
    state.min_update_interval = 1

    async def fake_send_once(**_kwargs):
        return True

    async def fast_sleep(_delay):
        state.task = None
        await original_sleep(0)

    monkeypatch.setattr(cp_mod.asyncio, "sleep", fast_sleep)

    task = asyncio.create_task(cp_v16._charge_rate_enforcement_loop(1, fake_send_once))
    state.task = task
    await task


@pytest.mark.asyncio
async def test_reconnect_pauses_enforcement_until_post_connect(cp_v16, monkeypatch):
    """Reconnect resets post-connect readiness before the OCPP run loop starts."""
    observed = {}
    cp_v16.tasks = []
    cp_v16.post_connect_success = True

    async def fake_stop():
        cp_v16.status = STATE_UNAVAILABLE

    async def fake_run(tasks):
        observed["post_connect_success"] = cp_v16.post_connect_success
        for task in tasks:
            task.close()

    monkeypatch.setattr(cp_v16, "stop", fake_stop)
    monkeypatch.setattr(cp_v16, "run", fake_run)

    await cp_v16.reconnect(SimpleNamespace(state=State.OPEN))

    assert observed["post_connect_success"] is False
