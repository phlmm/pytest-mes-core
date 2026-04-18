import pytest
from unittest.mock import MagicMock
from pytest_mes_core.protocols.memory import RamValidator
from pytest_mes_core.transports import CommandResult, TransportTimeoutError

def test_ram_validator_no_edac_success():
    mock_dut = MagicMock()
    
    def safe_run_side_effect(cmd, *args, **kwargs):
        if cmd.startswith("test -e /sys/devices/system/edac/mc/mc0/ce_count"):
            return CommandResult(command=cmd, ok=False, exited=1, stdout="", stderr="", duration_s=0.1)
        elif cmd.startswith("memtester"):
            return CommandResult(command=cmd, ok=True, exited=0, stdout="Done.", stderr="", duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)

    mock_dut.safe_run.side_effect = safe_run_side_effect

    res = RamValidator.verify_ram_health(mock_dut, size_mb=10, loops=1)
    
    assert res.passed
    assert res.metrics["memtester_passed"] == 1.0
    mock_dut.safe_run.assert_any_call("memtester 10M 1", timeout_s=60.0)

def test_ram_validator_with_edac_correctable_warning():
    mock_dut = MagicMock()
    
    def safe_run_side_effect(cmd, *args, **kwargs):
        if cmd.startswith("test -e /sys/devices/system/edac/mc/mc0/ce_count"):
            return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)
        elif cmd.startswith("echo 0 >"):
            return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)
        elif cmd.startswith("memtester"):
            return CommandResult(command=cmd, ok=True, exited=0, stdout="Done.", stderr="", duration_s=0.1)
        elif cmd == "cat /sys/devices/system/edac/mc/mc0/ce_count":
            return CommandResult(command=cmd, ok=True, exited=0, stdout="5\n", stderr="", duration_s=0.1)
        elif cmd == "cat /sys/devices/system/edac/mc/mc0/ue_count":
            return CommandResult(command=cmd, ok=True, exited=0, stdout="0\n", stderr="", duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)

    mock_dut.safe_run.side_effect = safe_run_side_effect

    res = RamValidator.verify_ram_health(mock_dut, size_mb=10, loops=1)
    
    # Correctable errors are just warnings, the test should still pass
    assert res.passed
    assert res.metrics["edac_ce_count"] == 5.0
    assert res.metrics["edac_ue_count"] == 0.0

def test_ram_validator_with_edac_uncorrectable_fatal():
    mock_dut = MagicMock()
    
    def safe_run_side_effect(cmd, *args, **kwargs):
        if cmd.startswith("test -e /sys/devices/system/edac/mc/mc0/ce_count"):
            return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)
        elif cmd.startswith("echo 0 >"):
            return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)
        elif cmd.startswith("memtester"):
            return CommandResult(command=cmd, ok=True, exited=0, stdout="Done.", stderr="", duration_s=0.1)
        elif cmd == "cat /sys/devices/system/edac/mc/mc0/ce_count":
            return CommandResult(command=cmd, ok=True, exited=0, stdout="0\n", stderr="", duration_s=0.1)
        elif cmd == "cat /sys/devices/system/edac/mc/mc0/ue_count":
            return CommandResult(command=cmd, ok=True, exited=0, stdout="1\n", stderr="", duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)

    mock_dut.safe_run.side_effect = safe_run_side_effect

    res = RamValidator.verify_ram_health(mock_dut, size_mb=10, loops=1)
    
    assert not res.passed
    assert res.error_msg == "Uncorrectable ECC Errors detected."

def test_ram_validator_memtester_crash():
    mock_dut = MagicMock()
    
    def safe_run_side_effect(cmd, *args, **kwargs):
        if cmd.startswith("test -e /sys/devices/system/edac/mc/mc0/ce_count"):
            return CommandResult(command=cmd, ok=False, exited=1, stdout="", stderr="", duration_s=0.1)
        elif cmd.startswith("memtester"):
            return CommandResult(command=cmd, ok=False, exited=137, stdout="OOM Killed", stderr="", duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)

    mock_dut.safe_run.side_effect = safe_run_side_effect

    res = RamValidator.verify_ram_health(mock_dut, size_mb=10, loops=1)
    
    assert not res.passed
    assert "memtester failed or killed by OOM" in res.error_msg

def test_ram_validator_transport_timeout_kernel_panic():
    mock_dut = MagicMock()
    
    def safe_run_side_effect(cmd, *args, **kwargs):
        if cmd.startswith("test -e /sys/devices/system/edac/mc/mc0/ce_count"):
            return CommandResult(command=cmd, ok=False, exited=1, stdout="", stderr="", duration_s=0.1)
        elif cmd.startswith("memtester"):
            raise TransportTimeoutError("Hardware stopped responding")
            
    mock_dut.safe_run.side_effect = safe_run_side_effect

    res = RamValidator.verify_ram_health(mock_dut, size_mb=10, loops=1)
    
    assert not res.passed
    assert "DUT frozen during RAM stress" in res.error_msg

def test_native_memory_validator_success():
    from pytest_mes_core.protocols.memory import NativeMemoryValidator
    mock_dut = MagicMock()

    def safe_run_side_effect(cmd, *args, **kwargs):
        if cmd.startswith("test -e"):
            return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)
        elif cmd.startswith("dd if=/dev/urandom"):
            return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)
        elif cmd.startswith("sh -c 'cat /tmp/test_payload.bin"):
            return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)
        elif cmd.startswith("dd if=/dev/mmcblk0"):
            return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)
        elif cmd.startswith("cmp -l"):
            return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)
        elif cmd.startswith("rm -f"):
            return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)

    mock_dut.safe_run.side_effect = safe_run_side_effect
    mock_dut.is_connected = True

    res = NativeMemoryValidator.verify_full_capacity(mock_dut, "/dev/mmcblk0", 1024, restore_backup=False)
    
    assert res.passed
    mock_dut.safe_run.assert_any_call("cmp -l /tmp/test_payload.bin /tmp/readback.bin", timeout_s=10.0)

def test_native_memory_validator_cmp_mismatch():
    from pytest_mes_core.protocols.memory import NativeMemoryValidator
    mock_dut = MagicMock()

    def safe_run_side_effect(cmd, *args, **kwargs):
        if cmd.startswith("test -e"):
            return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)
        elif cmd.startswith("cmp -l"):
            return CommandResult(command=cmd, ok=False, exited=1, stdout="1 255 0\n2 128 0", stderr="", duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)

    mock_dut.safe_run.side_effect = safe_run_side_effect
    mock_dut.is_connected = True

    res = NativeMemoryValidator.verify_full_capacity(mock_dut, "/dev/mmcblk0", 1024, restore_backup=False)
    
    assert not res.passed
    assert "2 byte mismatches detected" in res.error_msg

def test_native_memory_validator_with_backup_and_restore():
    from pytest_mes_core.protocols.memory import NativeMemoryValidator
    mock_dut = MagicMock()

    def safe_run_side_effect(cmd, *args, **kwargs):
        return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)

    mock_dut.safe_run.side_effect = safe_run_side_effect
    mock_dut.is_connected = True

    res = NativeMemoryValidator.verify_full_capacity(mock_dut, "/dev/mmcblk0", 1024, restore_backup=True)
    
    assert res.passed
    # Verify backup was taken
    mock_dut.safe_run.assert_any_call("dd if=/dev/mmcblk0 of=/tmp/original_backup.bin bs=1 count=1024 2>/dev/null", timeout_s=15.0)
    # Verify restore was executed
    mock_dut.safe_run.assert_any_call("sh -c 'cat /tmp/original_backup.bin > /dev/mmcblk0 && sync'", timeout_s=15.0)
