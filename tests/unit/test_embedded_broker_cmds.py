import os

from pytest_mes_core.config.protocols import HostMqttConfig
from pytest_mes_core.plugins.hardware import _mosquitto_broker_cmd, _amqtt_broker_cmd


def _make_cfg(**overrides):
    defaults = dict(broker_ip="127.0.0.1", port=18830)
    defaults.update(overrides)
    return HostMqttConfig(**defaults)


def test_mosquitto_cmd_uses_configured_port_and_pid_unique_conf(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _make_cfg(port=18830)
    argv, conf_path = _mosquitto_broker_cmd(cfg)

    assert conf_path == f"/tmp/mes_mosquitto_{os.getpid()}.conf"
    with open(conf_path) as f:
        content = f.read()
    assert "18830" in content
    assert "-v" not in argv
    assert argv[0] == "mosquitto"
    os.remove(conf_path)


def test_amqtt_cmd_uses_configured_port_and_pid_unique_conf():
    cfg = _make_cfg(port=27182)
    argv, conf_path = _amqtt_broker_cmd(cfg, amqtt_bin="/fake/bin/amqtt")

    assert conf_path == f"/tmp/mes_amqtt_{os.getpid()}.yml"
    with open(conf_path) as f:
        content = f.read()
    assert "bind: 0.0.0.0:27182" in content
    assert "1883" not in content
    assert argv[0] == "/fake/bin/amqtt"
    os.remove(conf_path)
