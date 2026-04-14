# tests/integration/test_docker_failover.py
import shutil
import socket
import threading
import subprocess
import pytest
from typing import Generator

from pytest_mes_core.config import SshTargetConfig
from pytest_mes_core.transports.ssh import EphemeralSSHClient
from pytest_mes_core.transports.failover import FailoverTransport
from pytest_mes_core.transports.base import CommandResult
from tests.mocks.virtual_transport import MockTransport

# 1. Skip this entire suite if the CI runner doesn't have Docker installed
if not shutil.which("docker"):
    pytest.skip("Docker is required for physical TCP failover testing.", allow_module_level=True)


def wait_for_ssh_banner(host: str, port: int, timeout_s: float = 30.0) -> None:
    """
    Deterministically wait for the SSH daemon to broadcast its protocol banner.
    Bypasses Docker port-binding lies by enforcing application-layer readiness.
    """
    import time
    start_time = time.monotonic()
    while time.monotonic() - start_time < timeout_s:
        try:
            with socket.create_connection((host, port), timeout=1.0) as sock:
                banner = sock.recv(1024).decode('utf-8', errors='replace')
                if "SSH-2.0" in banner:
                    return  # The daemon is fully initialized
        except (ConnectionRefusedError, ConnectionResetError, socket.timeout, UnicodeDecodeError):
            pass
        time.sleep(0.5)  # Exponential backoff/polling delay

    raise TimeoutError(f"SSH banner not detected on {host}:{port} within {timeout_s}s")


@pytest.fixture(scope="module")
def ssh_container() -> Generator[str, None, None]:
    """Spawns an ephemeral Digital Twin SSH server using Docker."""
    container_name = "mes_sshd_sim"
    host_port = 2222

    # 1. Clean slate (Zero-Leakage State Management)
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

    # 2. Boot a lightweight SSH daemon
    # 2. Boot a lightweight SSH daemon
    boot_res = subprocess.run([
        "docker", "run", "-d", "--name", container_name,
        "-p", f"{host_port}:2222",
        "-e", "USER_NAME=mes_operator",      # <-- FIXED: Do not use 'root'
        "-e", "USER_PASSWORD=mes_password",
        "-e", "PASSWORD_ACCESS=true",
        "-e", "SUDO_ACCESS=true",            # <-- ADDED: Grant sudo for hardware config
        "lscr.io/linuxserver/openssh-server:latest"
    ], capture_output=True, text=True)

    if boot_res.returncode != 0:
        raise RuntimeError(f"FATAL: Docker daemon rejected container spawn. stderr: {boot_res.stderr}")

    try:
        # 3. Quick sanity check: Did it die instantly?
        import time
        time.sleep(1.0)
        status_res = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", container_name],
            capture_output=True, text=True
        )
        status = status_res.stdout.strip()

        if status != "running":
            logs = subprocess.run(["docker", "logs", container_name], capture_output=True, text=True)
            raise RuntimeError(f"Digital Twin crashed on boot (Status: {status}).\nLogs:\n{logs.stderr}\n{logs.stdout}")

        # 4. DETERMINISTIC WAIT: Poll until SSH keys are generated and daemon is serving
        wait_for_ssh_banner("127.0.0.1", host_port)

        yield container_name

    except TimeoutError as e:
        # 5. FORENSIC DUMP: The container stayed 'running' but the SSH daemon locked up.
        logs = subprocess.run(["docker", "logs", container_name], capture_output=True, text=True)
        raise RuntimeError(f"Digital Twin network timeout.\nDocker Logs:\n{logs.stderr}\n{logs.stdout}") from e

    finally:
        # 6. ZERO-LEAKAGE: Annihilate the container regardless of test panics
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)


def test_physical_tcp_shatter_triggers_failover(ssh_container: str) -> None:
    """
    PROVES THE HOLY GRAIL OF FACTORY TESTING:
    If the board physically loses power mid-command, the SSH pipe shatters,
    the framework catches it without hanging, and the Serial port takes over.
    """
    # 1. Primary Transport (Physical TCP Socket to Docker)
    cfg = SshTargetConfig(
        ip_address="127.0.0.1",
        port=2222,
        user="mes_operator",                 # <-- FIXED: Match the provisioned user
        password="mes_password",
        connect_timeout_s=10.0
    )
    primary_ssh = EphemeralSSHClient(cfg)

    # 2. Out-of-Band Fallback (Mocked Serial Port)
    fallback_serial = MockTransport({
        r"\n": CommandResult(command="\n", stdout="", stderr="", exited=0, ok=True, duration_s=0.1),
        r"echo SURVIVED": CommandResult(command="echo SURVIVED", stdout="SERIAL SURVIVED", stderr="", exited=0, ok=True, duration_s=0.1)
    })

    # 3. Arm the Matrix
    matrix = FailoverTransport(primary=primary_ssh, fallback=fallback_serial)

    # Because we verified the banner in the fixture, this will connect instantly.
    matrix.connect()
    assert matrix.is_failed_over is False

    # 4. Prove Primary is routing TCP traffic natively
    res_primary = matrix.safe_run("echo ALIVE")
    assert res_primary.ok is True
    assert "ALIVE" in res_primary.stdout

    # ==============================================================
    # 5. THE ASSASSINATION (Simulate Toradex 12V Power Loss)
    # ==============================================================
    def pull_the_plug() -> None:
        import time
        # Give the SSH command 1 second to firmly establish its blocking read
        time.sleep(1.0)
        # `docker kill` sends SIGKILL. No TCP FIN packet is sent.
        subprocess.run(["docker", "kill", ssh_container], capture_output=True)

    assassin = threading.Thread(target=pull_the_plug)
    assassin.start()

    # 6. Execute a long-running command.
    # At t=1s, the container vanishes. Paramiko will throw an EOFError.
    # The Matrix MUST catch it and seamlessly reroute to Serial.
    res_survival = matrix.safe_run("sleep 10 && echo SURVIVED", timeout_s=15.0)

    # 7. The Mathematical Proof
    assert matrix.is_failed_over is True
    assert res_survival.ok is True
    assert res_survival.stdout == "SERIAL SURVIVED"

    assassin.join()
