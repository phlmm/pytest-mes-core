# tests/unit/test_psu.py
import pytest
from unittest.mock import patch, MagicMock
from pytest_mes_core.config import KeysightPsuConfig
from pytest_mes_core.instruments.power_supplies import ScpiPowerSupply, SafePowerController, InstrumentShortCircuitError

@patch('pytest_mes_core.instruments.power_supplies.pyvisa.ResourceManager')
@patch('pytest_mes_core.instruments.power_supplies.time.sleep') # Bypass the physical ramp delays
def test_scpi_safe_power_ramp(mock_sleep, mock_rm):
    """Proves the SafePowerController steps voltage and guarantees teardown."""
    mock_instrument = MagicMock()
    # Simulate a healthy idle current draw of 150mA
    mock_instrument.query.return_value = "0.150"
    mock_rm.return_value.open_resource.return_value = mock_instrument

    cfg = KeysightPsuConfig(vendor="keysight", visa_resource="ASRL1::INSTR")
    psu = ScpiPowerSupply(cfg)
    psu.connect()

    # Target 5V. The logic should step 1V, 2V, 3V, 4V, 5V.
    with SafePowerController(psu, target_v=5.0, current_limit_a=3.0):
        pass # Context manager automatically tears down power

    # Assert physical ramping occurred
    mock_instrument.write.assert_any_call("SOUR:VOLT 1.000,(@1)")
    mock_instrument.write.assert_any_call("SOUR:VOLT 5.000,(@1)")
    # Prove ZERO-LEAKAGE executed on context exit
    mock_instrument.write.assert_called_with("OUTP OFF,(@1)")

@patch('pytest_mes_core.instruments.power_supplies.pyvisa.ResourceManager')
@patch('pytest_mes_core.instruments.power_supplies.time.sleep')
def test_safe_power_controller_short_circuit_abort(mock_sleep, mock_rm):
    """Proves the safety envelope protects the test jig from fires."""
    mock_instrument = MagicMock()
    # Simulate a dead short drawing 10 Amps!
    mock_instrument.query.return_value = "10.0"
    mock_rm.return_value.open_resource.return_value = mock_instrument

    cfg = KeysightPsuConfig(vendor="keysight", visa_resource="ASRL1::INSTR")
    psu = ScpiPowerSupply(cfg)
    psu.connect()

    with pytest.raises(InstrumentShortCircuitError, match="Board acting as a short circuit"):
        with SafePowerController(psu, target_v=12.0, current_limit_a=3.0):
            pass

    # Prove power was killed even during the exception bubble-up
    mock_instrument.write.assert_called_with("OUTP OFF,(@1)")
