"""Tests for RemoteStartTransaction behaviour when already charging.

Some chargers reject a RemoteStartTransaction while a session is already
active on the connector (for example a locally-authorised session started at
the unit). Toggling the charge control switch on in that state should be a
success no-op instead of surfacing the charger's "Rejected" response.

The connector is considered already charging when it either reports a charging
connector status (Charging/SuspendedEV/SuspendedEVSE) or is actively importing
current via MeterValues - the latter covers chargers that stream MeterValues
for a local session without reporting an OCPP status or transaction.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ocpp.v16.enums import ChargePointStatus, Measurand, RemoteStartStopStatus

from custom_components.ocpp.enums import HAChargerStatuses as cstat
from custom_components.ocpp.ocppv16 import ChargePoint as CPv16


def _make_cp(*, status=None, current=None):
    cp = MagicMock(spec=CPv16)
    cp._remote_id_tag = "TAG"
    cp.call = AsyncMock(
        return_value=SimpleNamespace(status=RemoteStartStopStatus.accepted)
    )
    cp.notify_ha = AsyncMock()
    cp._metrics = {
        (1, cstat.status_connector.value): SimpleNamespace(value=status),
        (1, Measurand.current_import.value): SimpleNamespace(value=current),
    }
    return cp


@pytest.mark.parametrize(
    "status",
    [
        ChargePointStatus.charging.value,
        ChargePointStatus.suspended_ev.value,
        ChargePointStatus.suspended_evse.value,
    ],
)
async def test_noop_when_status_is_charging(status):
    """Skip RemoteStartTransaction when the connector status is a charging state."""
    cp = _make_cp(status=status)

    result = await CPv16.start_transaction(cp, 1)

    assert result is True
    cp.call.assert_not_awaited()


async def test_noop_when_importing_current_without_status():
    """Skip RemoteStartTransaction when current flows but status is unknown.

    The JuiceBox streams MeterValues for a local session without reporting an
    OCPP status or opening a transaction, so current import is the only signal.
    """
    cp = _make_cp(status=None, current=9.2)

    result = await CPv16.start_transaction(cp, 1)

    assert result is True
    cp.call.assert_not_awaited()


async def test_sends_when_idle_and_no_current():
    """Still send RemoteStartTransaction when idle with no current flowing."""
    cp = _make_cp(status=ChargePointStatus.available.value, current=0.0)

    result = await CPv16.start_transaction(cp, 1)

    assert result is True
    cp.call.assert_awaited_once()


async def test_sends_when_status_and_current_unknown():
    """Still send RemoteStartTransaction when nothing is known about the connector."""
    cp = _make_cp(status=None, current=None)

    result = await CPv16.start_transaction(cp, 1)

    assert result is True
    cp.call.assert_awaited_once()
