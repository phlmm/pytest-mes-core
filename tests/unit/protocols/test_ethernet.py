import pytest
from pytest_mes_core.protocols.ethernet import EthernetValidator
from pytest_mes_core.config import EthernetConfig
from pytest_mes_core.transports.base import CommandResult
from tests.mocks.virtual_transport import MockTransport

@pytest.fixture
def eth_config():
    return EthernetConfig(interface="eth0", expected_speed_mbps=1000, iperf_min_mbps=850.0)

def test_measure_throughput_success(eth_config):
    """
    Pass Criteria: The math strictly converts 945,000,000 bps to 945.0 Mbps
    and passes the test because 945.0 >= 850.0.
    """
    mock_iperf_json = '{"end": {"sum_sent": {"bits_per_second": 945000000.0, "retransmits": 12}, "cpu_utilization_percent": {"host_total": 4.2}}}'

    v_transport = MockTransport({
        r"iperf3 -c": CommandResult(command="iperf", stdout=mock_iperf_json, stderr="", exited=0, ok=True, duration_s=5.0)
    })

    result = EthernetValidator.measure_throughput(v_transport, eth_config)

    assert result.passed is True
    assert result.metrics["throughput_mbps"] == 945.0
    assert result.context["retransmits"] == 12

def test_measure_throughput_degraded_silicon(eth_config):
    """
    Pass Criteria: A physically degraded PHY yielding 10 Mbps correctly forces
    the result.passed state to False without crashing the framework.
    """
    mock_iperf_json = '{"end": {"sum_sent": {"bits_per_second": 10000000.0, "retransmits": 500}, "cpu_utilization_percent": {"host_total": 1.1}}}'

    v_transport = MockTransport({
        r"iperf3 -c": CommandResult(command="iperf", stdout=mock_iperf_json, stderr="", exited=0, ok=True, duration_s=5.0)
    })

    result = EthernetValidator.measure_throughput(v_transport, eth_config)

    assert result.passed is False
    assert "Low throughput: 10.0 Mbps" in result.error_msg
    assert result.metrics["throughput_mbps"] == 10.0

def test_iperf3_json_parsing_success():
    # 1. Program the Digital Twin to output exactly what a real Toradex board would
    mock_iperf_json = '{"end": {"sum_sent": {"bits_per_second": 945000000.0, "retransmits": 12}, "cpu_utilization_percent": {"host_total": 4.2}}}'

    v_transport = MockTransport({
        r"iperf3 -c": CommandResult("iperf", stdout=mock_iperf_json, stderr="", exited=0, ok=True, duration_s=5.0)
    })
    cfg = EthernetConfig(interface="eth0", expected_speed_mbps=1000, iperf_min_mbps=850.0)

    # 2. Execute Protocol
    result = EthernetValidator.measure_throughput(v_transport, cfg)

    # 3. Assert Framework Logic
    assert result.passed is True
    assert result.metrics["throughput_mbps"] == 945.0  # Proves bps -> Mbps math is correct
    assert result.context["retransmits"] == 12
    assert "iperf3 -c 192.168.100.1" in v_transport.command_history[-1] # Proves the correct bash command was sent!
