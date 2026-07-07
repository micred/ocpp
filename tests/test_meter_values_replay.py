"""Tests for MeterValues replay guards.

Some chargers (e.g. the JuiceBox) queue transaction-related MeterValues while
offline and replay them later, interleaved with live samples and re-sent with
fresh message ids even after being acknowledged. Applying those frames in
arrival order overwrites live readings with stale (typically zero) values.

Two guards protect the live metrics:

- a per-connector timestamp watermark: buckets older than the newest already
  applied bucket are ignored;
- Transaction.End buckets that do not belong to the active transaction are
  ignored (the JuiceBox replays zeroed end-of-session snapshots mid-session).
"""

import asyncio
import contextlib
from datetime import datetime, timedelta, UTC

import pytest
import websockets

from custom_components.ocpp.api import CentralSystem

from ocpp.v16 import call

from .charge_point_test import wait_ready
from .test_charge_point_v16 import ChargePoint


def _bucket(timestamp: datetime, power_w: str, context: str = "Sample.Clock"):
    return {
        "timestamp": timestamp.isoformat(),
        "sampledValue": [
            {
                "measurand": "Power.Active.Import",
                "context": context,
                "unit": "W",
                "value": power_w,
            }
        ],
    }


@pytest.mark.timeout(20)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9420, "cp_id": "CP_replay_stale", "cms": "cms_replay_stale"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_replay_stale"])
@pytest.mark.parametrize("port", [9420])
async def test_stale_replayed_frame_is_ignored(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """A bucket older than the newest applied bucket must not overwrite metrics."""
    cs: CentralSystem = setup_config_entry
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        cp = ChargePoint(f"{cp_id}_client", ws)
        task = asyncio.create_task(cp.start())
        try:
            await cp.send_boot_notification()
            await wait_ready(cs.charge_points[cp_id])

            now = datetime.now(tz=UTC)
            await cp.call(
                call.MeterValues(connector_id=1, meter_value=[_bucket(now, "2120")])
            )
            assert cs.get_metric(cp_id, "Power.Active.Import") == pytest.approx(2.12)

            # Replay from the offline queue: 10 hours old, zeroed.
            await cp.call(
                call.MeterValues(
                    connector_id=1,
                    meter_value=[_bucket(now - timedelta(hours=10), "0.00")],
                )
            )
            assert cs.get_metric(cp_id, "Power.Active.Import") == pytest.approx(2.12)

            # A genuinely newer frame must still be applied.
            await cp.call(
                call.MeterValues(
                    connector_id=1,
                    meter_value=[_bucket(now + timedelta(seconds=30), "1980")],
                )
            )
            assert cs.get_metric(cp_id, "Power.Active.Import") == pytest.approx(1.98)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(20)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9421, "cp_id": "CP_replay_txend", "cms": "cms_replay_txend"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_replay_txend"])
@pytest.mark.parametrize("port", [9421])
async def test_transaction_end_without_matching_tx_is_ignored(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """A zeroed Transaction.End bucket with no matching transaction is dropped."""
    cs: CentralSystem = setup_config_entry
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        cp = ChargePoint(f"{cp_id}_client", ws)
        task = asyncio.create_task(cp.start())
        try:
            await cp.send_boot_notification()
            await wait_ready(cs.charge_points[cp_id])

            now = datetime.now(tz=UTC)
            await cp.call(
                call.MeterValues(connector_id=1, meter_value=[_bucket(now, "2120")])
            )
            assert cs.get_metric(cp_id, "Power.Active.Import") == pytest.approx(2.12)

            # JuiceBox-style mid-session snapshot: Transaction.End context,
            # transactionId 0, everything zeroed, newer timestamp (so the
            # timestamp watermark alone cannot catch it).
            await cp.call(
                call.MeterValues(
                    connector_id=1,
                    transaction_id=0,
                    meter_value=[
                        _bucket(
                            now + timedelta(seconds=10),
                            "0.00",
                            context="Transaction.End",
                        )
                    ],
                )
            )
            assert cs.get_metric(cp_id, "Power.Active.Import") == pytest.approx(2.12)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(20)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9422, "cp_id": "CP_replay_txok", "cms": "cms_replay_txok"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_replay_txok"])
@pytest.mark.parametrize("port", [9422])
async def test_transaction_end_with_matching_tx_is_processed(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """Transaction.End for the active transaction must still be applied."""
    cs: CentralSystem = setup_config_entry
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        cp = ChargePoint(f"{cp_id}_client", ws)
        task = asyncio.create_task(cp.start())
        try:
            await cp.send_boot_notification()
            await wait_ready(cs.charge_points[cp_id])

            now = datetime.now(tz=UTC)
            # First frame carries the transaction id: the server adopts it.
            await cp.call(
                call.MeterValues(
                    connector_id=1,
                    transaction_id=77,
                    meter_value=[_bucket(now, "2120")],
                )
            )
            assert cs.get_metric(cp_id, "Power.Active.Import") == pytest.approx(2.12)

            # Legitimate end-of-transaction snapshot for the same transaction.
            await cp.call(
                call.MeterValues(
                    connector_id=1,
                    transaction_id=77,
                    meter_value=[
                        _bucket(
                            now + timedelta(seconds=10),
                            "0.00",
                            context="Transaction.End",
                        )
                    ],
                )
            )
            assert cs.get_metric(cp_id, "Power.Active.Import") == pytest.approx(0.0)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()


@pytest.mark.timeout(20)
@pytest.mark.parametrize(
    "setup_config_entry",
    [{"port": 9423, "cp_id": "CP_replay_reboot", "cms": "cms_replay_reboot"}],
    indirect=True,
)
@pytest.mark.parametrize("cp_id", ["CP_replay_reboot"])
@pytest.mark.parametrize("port", [9423])
async def test_watermark_resets_on_boot_notification(
    hass, socket_enabled, cp_id, port, setup_config_entry
):
    """A reboot (device clock may change) must not lock the metrics out."""
    cs: CentralSystem = setup_config_entry
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/{cp_id}", subprotocols=["ocpp1.6"]
    ) as ws:
        cp = ChargePoint(f"{cp_id}_client", ws)
        task = asyncio.create_task(cp.start())
        try:
            await cp.send_boot_notification()
            await wait_ready(cs.charge_points[cp_id])

            now = datetime.now(tz=UTC)
            # Device clock running far in the future poisons the watermark.
            await cp.call(
                call.MeterValues(
                    connector_id=1,
                    meter_value=[_bucket(now + timedelta(days=365), "2120")],
                )
            )
            assert cs.get_metric(cp_id, "Power.Active.Import") == pytest.approx(2.12)

            # After a reboot the corrected clock would look "stale" forever
            # unless the watermark is reset.
            await cp.send_boot_notification()
            await cp.call(
                call.MeterValues(connector_id=1, meter_value=[_bucket(now, "1500")])
            )
            assert cs.get_metric(cp_id, "Power.Active.Import") == pytest.approx(1.5)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await ws.close()
