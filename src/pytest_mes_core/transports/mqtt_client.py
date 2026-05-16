import queue
import time
import threading
import uuid
import json
import structlog
import anyio
import paho.mqtt.client as mqtt

from pytest_mes_core.config.protocols import HostMqttConfig
from pytest_mes_core.transports.base import CommandResult, TransportConnectionError, TransportTimeoutError

logger = structlog.get_logger('mes_core.transports.mqtt')

class MqttClient:
    """
    MQTT Transport for STM32 and other C2-over-MQTT devices.
    Liskov-compliant with DutTransport.
    Publishes commands to a specific topic and subscribes to a response stream.
    """
    def __init__(self, cfg: HostMqttConfig):
        self.cfg = cfg
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.cfg.client_id)
        if self.cfg.username:
            password = self.cfg.password.get_secret_value() if self.cfg.password else None
            self._client.username_pw_set(self.cfg.username, password)

        if self.cfg.tls:
            import ssl
            self._client.tls_set(cert_reqs=ssl.CERT_NONE)
            self._client.tls_insecure_set(True)

        self._connected_event = threading.Event()
        self._subscribers = []
        self._sub_lock = threading.Lock()
        
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            self._connected_event.set()
            self._client.subscribe(self.cfg.subscribe_topic)
            logger.debug("mqtt_connected", broker=self.cfg.broker_ip)
        else:
            logger.error("mqtt_connect_failed", reason_code=reason_code)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        self._connected_event.clear()
        logger.debug("mqtt_disconnected", reason_code=reason_code)

    def _on_message(self, client, userdata, msg):
        payload = msg.payload
        print(f"\n[MQTT Client] Received msg on {msg.topic}, len={len(payload)}")
        with self._sub_lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    pass

    @property
    def is_connected(self) -> bool:
        return self._connected_event.is_set()

    def connect(self) -> None:
        try:
            # Use connect_async + loop_start so the network thread owns the
            # entire TCP + MQTT handshake.  The previous pattern (sync
            # connect() then loop_start()) could leave an orphaned TCP socket
            # if anything failed between the TCP handshake and loop_start(),
            # causing mosquitto to log a ghost "Client <unknown>" connection.
            self._client.connect_async(self.cfg.broker_ip, self.cfg.port, keepalive=60)
            self._client.loop_start()
            if not self._connected_event.wait(timeout=5.0):
                self._client.loop_stop()
                raise TransportConnectionError(
                    f"Failed to connect to MQTT broker at "
                    f"{self.cfg.broker_ip}:{self.cfg.port}"
                )
        except TransportConnectionError:
            raise
        except Exception as e:
            self._client.loop_stop()
            raise TransportConnectionError(f"Failed to connect to MQTT broker: {e}")

    def disconnect(self) -> None:
        if self._connected_event.is_set():
            self._client.disconnect()
        self._client.loop_stop()
        self._connected_event.clear()

    async def async_connect(self) -> None:
        await anyio.to_thread.run_sync(self.connect)

    async def async_disconnect(self) -> None:
        await anyio.to_thread.run_sync(self.disconnect)

    def subscribe(self, maxsize: int = 1024) -> queue.Queue:
        q = queue.Queue(maxsize=maxsize)
        with self._sub_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._sub_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def write_line(self, line: str, sensitive: bool = False) -> None:
        if not self.is_connected:
            raise TransportConnectionError("MQTT is disconnected")
        if not sensitive:
            logger.debug("mqtt_tx", topic=self.cfg.publish_topic, payload=line)
        else:
            logger.debug("mqtt_tx", topic=self.cfg.publish_topic, payload="********")
        
        info = self._client.publish(self.cfg.publish_topic, line.encode('utf-8'), qos=1)
        info.wait_for_publish(timeout=2.0)
        if not info.is_published():
            raise TransportConnectionError("Failed to publish MQTT message")

    def raw_write(self, data: bytes) -> None:
        if not self.is_connected:
            raise TransportConnectionError("MQTT is disconnected")
        info = self._client.publish(self.cfg.publish_topic, data, qos=1)
        info.wait_for_publish(timeout=2.0)
        if not info.is_published():
            raise TransportConnectionError("Failed to publish MQTT raw message")

    def flush_buffers(self) -> None:
        with self._sub_lock:
            for q in self._subscribers:
                while not q.empty():
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break

    def safe_run(self, cmd: str, timeout_s: float = 30.0, check_exit_code: bool = False, auto_retry: bool = False, **kwargs) -> CommandResult:
        if not self.is_connected:
            raise TransportConnectionError("MQTT is disconnected")
            
        q = self.subscribe(maxsize=0)
        self.flush_buffers()
        
        t0 = time.perf_counter()
        self.write_line(cmd)
        
        stdout_chunks = []
        t_end = time.perf_counter() + timeout_s
        got_first = False
        
        try:
            while time.perf_counter() < t_end:
                try:
                    wait_time = 0.5 if got_first else min(0.1, t_end - time.perf_counter())
                    chunk = q.get(timeout=wait_time)
                    stdout_chunks.append(chunk.decode('utf-8', errors='replace'))
                    got_first = True
                except queue.Empty:
                    if got_first:
                        break
            
            duration = round(time.perf_counter() - t0, 3)
            stdout_clean = "".join(stdout_chunks).strip()
            
            if not got_first:
                raise TransportTimeoutError(f"MQTT Command '{cmd}' timed out after {timeout_s}s waiting for response")
                
            result = CommandResult(command=cmd, stdout=stdout_clean, stderr='', exited=0, ok=True, duration_s=duration)
            return result
        finally:
            self.unsubscribe(q)

    async def async_safe_run(self, cmd: str, timeout_s: float = 30.0, check_exit_code: bool = False, auto_retry: bool = False, **kwargs) -> CommandResult:
        from functools import partial
        return await anyio.to_thread.run_sync(
            partial(self.safe_run, cmd, timeout_s=timeout_s, check_exit_code=check_exit_code, auto_retry=auto_retry, **kwargs)
        )

    def publish_json(self, payload: dict) -> None:
        """Fire-and-forget JSON publish."""
        self.write_line(json.dumps(payload))

    async def async_publish_json(self, payload: dict) -> None:
        await anyio.to_thread.run_sync(self.publish_json, payload)

    def wait_for_event(self, event_code: str, timeout_s: float = 10.0) -> dict:
        """Waits for a specific JSON event from the subscribed topic."""
        q = self.subscribe(maxsize=0)
        t_end = time.perf_counter() + timeout_s
        try:
            while time.perf_counter() < t_end:
                try:
                    chunk = q.get(timeout=0.1)
                    payload_str = chunk.decode('utf-8', errors='replace')
                    try:
                        data = json.loads(payload_str)
                        if data.get("eventCode") == event_code:
                            return data
                    except json.JSONDecodeError:
                        pass
                except queue.Empty:
                    pass
            raise TransportTimeoutError(f"Timed out waiting for MQTT event '{event_code}'")
        finally:
            self.unsubscribe(q)

    async def async_wait_for_event(self, event_code: str, timeout_s: float = 10.0) -> dict:
        from functools import partial
        return await anyio.to_thread.run_sync(
            partial(self.wait_for_event, event_code, timeout_s)
        )
