# src/pytest_mes_core/protocols/time_sync.py
import time
import json
import logging
from typing import Dict, Any

from pytest_mes_core.transports import (
    DutTransport,
    TransportConnectionError,
    TransportTimeoutError
)
from pytest_mes_core.protocols import ValidatorResult
from pytest_mes_core.config import TimeSyncConfig, TimeDaemonType

logger = logging.getLogger("mes_core.protocols.time")

class RtcTimeValidator:
    """
    Polymorphic Temporal & PPS Protocol.
    Supports chrony, ntpd, systemd-timesyncd, and linuxptp (IEEE 1588).
    Features a strict 'Temporal Taint' warning system for factory auditing.
    """

    @classmethod
    def verify_and_sync_time(cls, dut: DutTransport, cfg: TimeSyncConfig) -> ValidatorResult:
        """Audits hardware PPS, verifies time drift, and warns on temporal taint.

        Args:
            dut: The transport interface connected to the target.
            cfg: The time synchronization configuration.

        Returns:
            ValidatorResult: Pass/fail outcome with detailed drift metrics and taint warnings.
        """
        logger.info(f"[Time] Initiating Temporal Audit. Daemon Strategy: {cfg.daemon_type.value.upper()}")

        context_data: Dict[str, Any] = {"temporal_warnings": []}
        metrics: Dict[str, float] = {}
        errors = []

        try:
            # ==========================================
            # 1. KERNEL LEVEL: Physical PPS Interrupts
            # ==========================================
            if cfg.verify_hardware_pps and cfg.pps_devices:
                logger.debug(f"[Time] Auditing hardware Pulse-Per-Second (PPS) interrupts on {cfg.pps_devices}...")
                for pps in cfg.pps_devices:
                    res_pps = dut.safe_run(f"timeout 2.5 ppstest {pps} 2>/dev/null", timeout_s=4.0)
                    if res_pps.ok and ("assert" in res_pps.stdout or "clear" in res_pps.stdout):
                        context_data[f"{pps}_hardware_active"] = True
                        logger.debug(f"[Time] PPS active on {pps}.")
                    else:
                        context_data[f"{pps}_hardware_active"] = False
                        logger.error(f"[Time] Dead physical layer: No PPS interrupts detected on {pps}")
                        errors.append(f"Dead physical layer: No PPS interrupts on {pps}")

            # ==========================================
            # 2. APPLICATION LEVEL: Polymorphic Daemon Interrogation
            # ==========================================
            logger.debug(f"[Time] Interrogating {cfg.daemon_type.value} daemon state...")
            cls._audit_time_daemon(dut, cfg.daemon_type, context_data)

            # ==========================================
            # 3. KERNEL POSIX TIME vs HOST PC TIME
            # ==========================================
            # We grab the host epoch right before the network call to minimize transport latency jitter
            host_epoch = time.time()
            res_sys = dut.safe_run("date +%s", timeout_s=3.0)

            if not res_sys.ok or not res_sys.stdout.strip().isdigit():
                err_msg = "Failed to read POSIX system time from DUT."
                logger.error(f"[Time] {err_msg}")
                return ValidatorResult(passed=False, error_msg=err_msg, context=context_data)

            dut_sys_epoch = float(res_sys.stdout.strip())
            sys_drift = abs(host_epoch - dut_sys_epoch)
            metrics["host_to_dut_drift_s"] = round(sys_drift, 3)

            logger.debug(f"[Time] Host Epoch: {host_epoch:.2f} | DUT Epoch: {dut_sys_epoch:.2f} | Drift: {sys_drift:.3f}s")

            # ==========================================
            # 4. HARDWARE RTC POLLING & BATTERY CHECK
            # ==========================================
            valid_rtcs = []
            for rtc in cfg.rtc_paths:
                logger.debug(f"[Time] Probing physical hardware RTC: {rtc}...")
                res_rtc = dut.safe_run(f"cat {rtc}/since_epoch 2>/dev/null || date -u +%s -d \"$(cat {rtc}/time 2>/dev/null)\"", timeout_s=2.0)
                rtc_name = rtc.split('/')[-2] if '/' in rtc else rtc

                if res_rtc.ok and res_rtc.stdout.strip().isdigit():
                    rtc_epoch = float(res_rtc.stdout.strip())
                    rtc_drift = abs(dut_sys_epoch - rtc_epoch)
                    metrics[f"{rtc_name}_to_sys_drift_s"] = round(rtc_drift, 3)
                    context_data[f"{rtc_name}_detected"] = True
                    valid_rtcs.append(rtc_name)

                    # BATTERY CHECK: If the RTC is at Epoch 0 (1970) but the system time isn't, the battery is dead.
                    # We use < 1000000 (roughly 11 days after 1970) to catch minor boot increments
                    if rtc_epoch < 1000000:
                        warn_msg = f"RTC {rtc_name} read near Epoch 0 (1970). Coin-cell battery is dead or missing."
                        logger.warning("="*60)
                        logger.warning(f"[Time] ⚠️ HARDWARE WARNING: DEAD RTC BATTERY DETECTED!")
                        logger.warning(f"[Time] {warn_msg}")
                        logger.warning("="*60)
                        context_data["temporal_warnings"].append(warn_msg)
                else:
                    logger.debug(f"[Time] RTC {rtc_name} is unresponsive or unmapped.")
                    context_data[f"{rtc_name}_detected"] = False

            if not valid_rtcs and cfg.rtc_paths:
                warn_msg = "No functional physical RTCs detected. System will lose time on reboot."
                logger.warning(f"[Time] ⚠️ {warn_msg}")
                context_data["temporal_warnings"].append(warn_msg)

            # ==========================================
            # 5. TEMPORAL ENFORCEMENT & TAINT WARNING
            # ==========================================
            if sys_drift > cfg.max_drift_s:
                if not cfg.force_host_sync:
                    logger.error(f"[Time] Time drift ({round(sys_drift, 1)}s) exceeds max tolerance ({cfg.max_drift_s}s).")
                    errors.append(f"Time drift ({round(sys_drift, 1)}s) exceeds max tolerance ({cfg.max_drift_s}s).")
                else:
                    taint_msg = f"DUT drifted {round(sys_drift, 1)}s. Forcing artificial Host sync. BOARD TEMPORALLY TAINTED."
                    logger.warning("="*60)
                    logger.warning(f"[Time] 🚨 TEMPORAL TAINT WARNING!")
                    logger.warning(f"[Time] {taint_msg}")
                    logger.warning(f"[Time] Future cryptographic certs may be generated under an artificial epoch.")
                    logger.warning("="*60)

                    context_data["temporal_warnings"].append(taint_msg)
                    context_data["is_temporally_tainted"] = True

                    # Kill whatever daemon the TOML specified
                    logger.debug("[Time] Reaping native time daemons to prevent NTP/Chrony fighting...")
                    kill_targets = "chronyd ntpd systemd-timesyncd ptp4l phc2sys"
                    dut.safe_run(f"killall -9 {kill_targets} >/dev/null 2>&1 || true", timeout_s=3.0)

                    # Inject Host Time
                    logger.debug("[Time] Injecting POSIX epoch directly into DUT kernel...")
                    host_str = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(host_epoch))
                    sync_res = dut.safe_run(f"date -u -s '{host_str}'", timeout_s=3.0)

                    if sync_res.ok:
                        if valid_rtcs:
                            logger.debug("[Time] Flushing POSIX time down to hardware RTC silicon (hwclock)...")
                            dut.safe_run("hwclock --systohc -u", timeout_s=5.0)
                    else:
                        errors.append("Failed to force POSIX time injection.")
            else:
                context_data["is_temporally_tainted"] = False
                logger.info(f"[Time] Temporal state is healthy (Drift: {round(sys_drift, 3)}s).")

            passed = len(errors) == 0
            return ValidatorResult(
                passed=passed,
                error_msg=" | ".join(errors) if not passed else "",
                metrics=metrics,
                context=context_data
            )

        except TransportTimeoutError:
            logger.critical("[Time] FATAL: DUT hung while evaluating temporal states.")
            return ValidatorResult(passed=False, error_msg="DUT hung while evaluating temporal states.", context=context_data)
        except TransportConnectionError as e:
            logger.critical(f"[Time] FATAL: Transport pipe shattered during time sync: {e}")
            return ValidatorResult(passed=False, error_msg=f"Transport pipe shattered during time sync: {e}", context=context_data)

    @classmethod
    def _audit_time_daemon(cls, dut: DutTransport, daemon: TimeDaemonType, ctx: Dict[str, Any]) -> None:
        """Strategy router for interrogating the specific time daemon running on the DUT."""
        try:
            if daemon == TimeDaemonType.CHRONY:
                res = dut.safe_run("chronyc tracking 2>/dev/null", timeout_s=3.0)
                ctx["daemon_health"] = cls._parse_chrony_output(res.stdout) if res.ok and res.stdout.strip() else "Unresponsive"

            elif daemon == TimeDaemonType.NTPD:
                res = dut.safe_run("ntpq -p 2>/dev/null", timeout_s=3.0)
                ctx["daemon_health"] = res.stdout.strip() if res.ok else "Unresponsive"

            elif daemon == TimeDaemonType.SYSTEMD:
                res = dut.safe_run("timedatectl timesync-status 2>/dev/null", timeout_s=3.0)
                ctx["daemon_health"] = res.stdout.strip() if res.ok else "Unresponsive"

            elif daemon == TimeDaemonType.PTP:
                res = dut.safe_run("pmc -u -b 0 'GET PORT_DATA_SET' 2>/dev/null", timeout_s=3.0)
                ctx["daemon_health"] = res.stdout.strip() if res.ok else "Unresponsive"
        except TransportConnectionError:
            ctx["daemon_health"] = "Transport Shattered"

    @staticmethod
    def _parse_chrony_output(raw_output: str) -> Dict[str, Any]:
        """
        Dynamically handles JSON output (if customized on DUT) or falls back to
        safely parsing standard `chronyc tracking` key-value pairs.
        """
        try:
            return json.loads(raw_output)
        except json.JSONDecodeError:
            parsed = {}
            for line in raw_output.split('\n'):
                if ':' in line:
                    parts = line.split(':', 1)
                    key = parts[0].strip()
                    val = parts[1].strip()
                    parsed[key] = val
            return parsed
