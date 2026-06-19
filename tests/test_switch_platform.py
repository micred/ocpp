"""Tests for the OCPP switch platform definitions."""

from custom_components.ocpp.switch import SWITCHES


def test_connector_availability_switch_key_is_spelled_correctly():
    """Expose connector availability with the expected entity key."""
    keys = {switch.key for switch in SWITCHES}

    assert "connector_availability" in keys
    assert "connnector_availability" not in keys
