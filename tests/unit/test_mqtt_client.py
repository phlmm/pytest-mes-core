from __future__ import annotations
'\nUnit tests for MqttClient TLS wiring and safe_run timing edge cases.\n\npaho.mqtt.client.Client is mocked — no real broker required. Follows the\nmocking style used in test_serial_client.py / test_failover_unit.py: patch\nthe constructor, inject a MagicMock, and inspect call args.\n'
import queue
import time
from unittest.mock import MagicMock, patch
import pytest
from pytest_mes_core.config.protocols import HostMqttConfig
from pytest_mes_core.transports.mqtt_client import MqttClient

def _make_client(**cfg_kwargs) -> tuple[MqttClient, MagicMock]:
    cfg = HostMqttConfig(broker_ip='10.0.0.5', **cfg_kwargs)
    with patch('pytest_mes_core.transports.mqtt_client.mqtt.Client') as mock_cls:
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance
        client = MqttClient(cfg)
    return (client, mock_instance)

class TestTlsVerification:

    def test_tls_insecure_default_preserves_existing_behavior(self):
        """With tls=True and the tls_insecure default (True), the client must
        keep the historical CERT_NONE + tls_insecure_set(True) wiring."""
        client, mock_instance = _make_client(tls=True)
        import ssl
        _, kwargs = mock_instance.tls_set.call_args
        assert kwargs['cert_reqs'] == ssl.CERT_NONE
        mock_instance.tls_insecure_set.assert_called_once_with(True)

    def test_tls_insecure_false_enables_verification(self):
        """With tls_insecure=False, the broker certificate must actually be verified:
        CERT_REQUIRED + tls_insecure_set(False)."""
        client, mock_instance = _make_client(tls=True, tls_insecure=False, ca_cert_path='/nonexistent/ca.pem')
        import ssl
        _, kwargs = mock_instance.tls_set.call_args
        assert kwargs['cert_reqs'] == ssl.CERT_REQUIRED
        mock_instance.tls_insecure_set.assert_called_once_with(False)

    def test_no_tls_set_call_when_tls_disabled(self):
        client, mock_instance = _make_client(tls=False)
        mock_instance.tls_set.assert_not_called()
        mock_instance.tls_insecure_set.assert_not_called()

class TestOnMessageLogging:

    def test_on_message_does_not_print_and_forwards_to_subscribers(self, capsys):
        client, _ = _make_client()
        q = client.subscribe(maxsize=0)
        msg = MagicMock()
        msg.topic = 'mes/c2/resp'
        msg.payload = b'hello'
        client._on_message(None, None, msg)
        captured = capsys.readouterr()
        assert '[MQTT Client]' not in captured.out
        assert 'mqtt_rx' in captured.out
        assert q.get_nowait() == b'hello'

    def test_on_disconnect_does_not_print(self, capsys):
        client, _ = _make_client()
        client._on_disconnect(None, None, None, 7, None)
        captured = capsys.readouterr()
        assert 'PAHO_MQTT' not in captured.err
        assert 'PAHO_MQTT' not in captured.out

class TestSafeRunTimingGuard:

    def test_safe_run_wait_time_never_raises_value_error_near_deadline(self):
        """Regression: `min(0.1, t_end - time.perf_counter())` could go
        negative/zero between the loop's `while` check and this computation,
        making `queue.get(timeout=wait_time)` raise ValueError instead of the
        intended queue.Empty/timeout behavior."""
        client, _ = _make_client()
        client._connected_event.set()
        real_queue_cls = queue.Queue

        class SlowQueue(real_queue_cls):

            def get(self, *args, **kwargs):
                time.sleep(0.09)
                raise queue.Empty()
        with patch.object(MqttClient, 'subscribe', return_value=SlowQueue()):
            with patch.object(client, 'write_line'):
                with patch.object(client, 'flush_buffers'):
                    with pytest.raises(Exception) as exc_info:
                        client.safe_run('echo hi', timeout_s=0.1)
        from pytest_mes_core.transports.base import TransportTimeoutError
        assert isinstance(exc_info.value, TransportTimeoutError)