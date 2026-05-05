import structlog
"""
Hardware Transports Plugin

This module manages the physical connections to the Device Under Test (DUT).
It provisions Power Supplies, Serial TTYs, and SSH sockets, wrapping them in
robust failover mechanisms. This ensures tests can communicate with the hardware
regardless of whether it is sitting at a U-Boot prompt or a fully booted Linux OS.
"""
import pytest
import logging
from pathlib import Path
from typing import Optional, Any, Generator
from dataclasses import dataclass
from pytest_mes_core.config import StationEnvironment
from pytest_mes_core.instruments.power_supplies import ScpiPowerSupply
from pytest_mes_core.transports import EphemeralSSHClient, EphemeralSerialClient, FailoverTransport, TransportConnectionError, TransportTimeoutError
logger = structlog.get_logger('mes_core.hardware')

@pytest.fixture(scope='session')
def psu_hardware(request: pytest.FixtureRequest, mes_env: StationEnvironment) -> Generator[Optional[ScpiPowerSupply], None, None]:
    """
    Initializes and manages the Programmable Power Supply (PSU) for the jig.

    This fixture reads the SCPI configuration from the TOML environment. If a PSU
    is defined and enabled, it connects via TCP/RS232, yields the instrument for
    the State Machine to control, and guarantees the connection is closed during teardown.

    Returns:
        Generator[Optional[ScpiPowerSupply], None, None]: The PSU controller, or None if disabled.

    Example:
        def test_sleep_current_draw(psu_hardware, dut_transport):
            if not psu_hardware:
                pytest.skip("Test requires a physical PSU to measure current.")

            dut_transport.safe_run("rtcwake -m mem -s 10")
            current_amps = psu_hardware.measure_current()
            assert current_amps < 0.050, "Sleep current exceeds 50mA limit!"
    """
    if not mes_env.psu_hardware or not mes_env.psu_hardware.enabled:
        yield None
        return
    if mes_env.psu_hardware.vendor == "fnirsi":
        from pytest_mes_core.instruments.fnirsi_dps150 import FnirsiDPS150
        psu = FnirsiDPS150(port=mes_env.psu_hardware.serial_port, baudrate=mes_env.psu_hardware.baudrate)
        psu.connect()
        if hasattr(mes_env.psu_hardware, 'ovp_limit') and mes_env.psu_hardware.ovp_limit is not None:
            psu.set_ovp_limit(mes_env.psu_hardware.ovp_limit)
        if hasattr(mes_env.psu_hardware, 'ocp_limit') and mes_env.psu_hardware.ocp_limit is not None:
            psu.set_ocp_limit(mes_env.psu_hardware.ocp_limit)
        if hasattr(mes_env.psu_hardware, 'default_voltage') and mes_env.psu_hardware.default_voltage is not None:
            psu.set_voltage(mes_env.psu_hardware.default_voltage)
        if hasattr(mes_env.psu_hardware, 'default_current') and mes_env.psu_hardware.default_current is not None:
            psu.set_current(mes_env.psu_hardware.default_current)
    else:
        psu = ScpiPowerSupply(mes_env.psu_hardware)
        psu.connect()

    if hasattr(mes_env.psu_hardware, 'enable_data_logging') and mes_env.psu_hardware.enable_data_logging:
        if hasattr(psu, 'start_data_logger'):
            psu.start_data_logger()
    try:
        yield psu
    finally:
        if hasattr(mes_env.psu_hardware, 'enable_data_logging') and mes_env.psu_hardware.enable_data_logging:
            if hasattr(psu, 'download_data_log'):
                spool_dir = getattr(request.config, '_mes_telemetry_spool_dir', None)
                target_dir = getattr(request.config, '_mes_telemetry_target_dir', None)
                if spool_dir:
                    psu.download_data_log(spool_dir / 'instrument_logs')
                elif target_dir:
                    psu.download_data_log(target_dir / 'instrument_logs')
                else:
                    psu.download_data_log(Path('artifacts/evse_telemetry/instrument_logs'))
        psu.close()

@pytest.fixture(scope='session')
def ssh_client(mes_env: StationEnvironment) -> Optional[EphemeralSSHClient]:
    """
    Initializes the high-speed Ethernet/SSH transport client.

    Returns:
        Optional[EphemeralSSHClient]: The SSH client configured with the DUT's IP, or None.
    """
    if 'primary' in mes_env.ssh_targets and mes_env.ssh_targets['primary'].enabled:
        return EphemeralSSHClient(mes_env.ssh_targets['primary'])
    return None

@pytest.fixture(scope='session')
def serial_client(mes_env: StationEnvironment) -> Optional[EphemeralSerialClient]:
    """
    Initializes the low-level UART/Serial transport client.

    Returns:
        Optional[EphemeralSerialClient]: The Serial client configured with the debug COM port, or None.
    """
    if 'debug_port' in mes_env.host_serial and mes_env.host_serial['debug_port'].enabled:
        return EphemeralSerialClient(mes_env.host_serial['debug_port'])
    return None

@pytest.fixture(scope='session')
def dut_transport(request: pytest.FixtureRequest, mes_env: StationEnvironment, ssh_client: Optional[EphemeralSSHClient], serial_client: Optional[EphemeralSerialClient]) -> Generator[Any, None, None]:
    """
    The Master Hardware Transport Abstraction.

    This is the primary fixture test engineers should use to interact with the board.
    It wraps both the SSH and Serial clients into a 'FailoverTransport'.

    If the board is in OS_USERLAND, the FailoverTransport routes commands over high-speed SSH.
    If the network stack crashes or the board is sitting in the Bootloader, it seamlessly
    routes the commands over the raw UART byte-stream using the exact same API.

    Returns:
        Generator[FailoverTransport | EphemeralSSHClient | EphemeralSerialClient, None, None]:
        The active transport mechanism.

    Example:
        def test_read_temperature(dut_transport):
            # The engineer doesn't care if this goes over SSH or Serial.
            # safe_run handles the routing and exit-code validation automatically.
            res = dut_transport.safe_run("cat /sys/class/thermal/thermal_zone0/temp", check_exit_code=True)
            temp_c = int(res.stdout.strip()) / 1000.0
            assert temp_c < 85.0
    """
    if request.config.getoption('--mock-hardware'):
        logger.warning('=' * 60)
        logger.warning('[WARNING] --mock-hardware ENABLED. Bypassing physical connections!')
        logger.warning('=' * 60)
        import sys
        from pathlib import Path
        workspace_root = Path(__file__).parent.parent.parent.parent
        if str(workspace_root) not in sys.path:
            sys.path.insert(0, str(workspace_root))
        from tests.mocks.virtual_transport import MockTransport
        transport = MockTransport()
        transport.connect()
        yield transport
        transport.disconnect()
        return
    if ssh_client and serial_client:
        transport = FailoverTransport(primary=ssh_client, fallback=serial_client)
    elif ssh_client:
        transport = ssh_client
    elif serial_client:
        transport = serial_client
    else:
        pytest.skip('No enabled transport targets found in the TOML configuration.')
        return
    fsm_active = hasattr(mes_env, 'state_machine') and mes_env.state_machine and mes_env.state_machine.enabled
    if not fsm_active:
        try:
            transport.connect()
            logger.info('[Fixture] DUT Transport Matrix connected successfully (Legacy Mode).')
        except (TransportConnectionError, TransportTimeoutError) as e:
            logger.warning('dut_transport_offline_during_setup_reason_e', e=e)
        except Exception as e:
            logger.error('unexpected_transport_failure_e', e=e)
    else:
        logger.info('[Fixture] FSM is active. Deferring Transport socket binding to State Machine.')
    try:
        yield transport
    finally:
        transport.disconnect()

import sys
import os
import socket
import subprocess
import time

def _is_port_open(ip: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((ip, port)) == 0

@pytest.fixture(scope='session', autouse=True)
def embedded_mqtt_broker(mes_env: StationEnvironment):
    """
    Spins up an embedded mosquitto broker if the target is localhost
    and the port isn't already bound.
    """
    if not mes_env.host_mqtt or "primary" not in mes_env.host_mqtt:
        yield None
        return
        
    cfg = mes_env.host_mqtt["primary"]
    if cfg.broker_ip not in ("127.0.0.1", "localhost", "169.254.5.50"):
        yield None
        return
        
    if _is_port_open(cfg.broker_ip, cfg.port):
        yield None
        return
        
    try:
        # ── Preference: mosquitto (more robust) ──────────────────────────────
        conf_path = "/tmp/mes_mosquitto.conf"
        with open(conf_path, "w") as f:
            f.write(f"listener {cfg.port} 0.0.0.0\nallow_anonymous true\n")
        proc = subprocess.Popen(["mosquitto", "-v", "-c", conf_path])
        logger.info("Started embedded mosquitto broker.")
    except FileNotFoundError:
        try:
            # ── Fallback: amqtt ─────────────────────────────────────────────
            amqtt_bin = os.path.join(sys.prefix, "bin", "amqtt")
            if os.path.exists(amqtt_bin):
                conf_path = "/tmp/mes_amqtt.yml"
                with open(conf_path, "w") as f:
                    # Added sys_interval: 0 to fix crash on Python 3.14+
                    f.write("listeners:\n  default:\n    type: tcp\n    bind: 0.0.0.0:1883\n"
                            "sys_interval: 0\n"
                            "auth:\n  allow-anonymous: true\n  plugins:\n    - auth.anonymous\n")
                proc = subprocess.Popen([amqtt_bin, "-c", conf_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                logger.info("Started embedded amqtt broker.")
            else:
                logger.warning("Neither 'mosquitto' nor 'amqtt' found. Skipping embedded broker.")
                yield None
                return
        except Exception as e:
            logger.warning(f"Failed to start embedded broker: {e}")
            yield None
            return
    
    for _ in range(20):
        if _is_port_open(cfg.broker_ip, cfg.port):
            break
        time.sleep(0.1)
        
    yield proc
    
    proc.terminate()
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        proc.kill()