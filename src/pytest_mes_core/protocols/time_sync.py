import anyio
import structlog
import time
import json
import logging
from typing import Dict, Any
from pytest_mes_core.transports import DutTransport, TransportConnectionError, TransportTimeoutError
from pytest_mes_core.protocols import ValidatorResult
from pytest_mes_core.config import TimeSyncConfig, TimeDaemonType
logger = structlog.get_logger('mes_core.protocols.time')

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
        logger.info('initiating_temporal_audit_daemon_strategy_val', val=cfg.daemon_type.value.upper())
        context_data: Dict[str, Any] = {'temporal_warnings': []}
        metrics: Dict[str, float] = {}
        errors = []
        try:
            if cfg.verify_hardware_pps and cfg.pps_devices:
                logger.debug('auditing_hardware_pulse_per_second_pps_interrupts_on_pps_devices', pps_devices=cfg.pps_devices)
                for pps in cfg.pps_devices:
                    res_pps = dut.safe_run(f'timeout 2.5 ppstest {pps} 2>/dev/null', timeout_s=4.0)
                    if res_pps.ok and ('assert' in res_pps.stdout or 'clear' in res_pps.stdout):
                        context_data[f'{pps}_hardware_active'] = True
                        logger.debug('pps_active_on_pps', pps=pps)
                    else:
                        context_data[f'{pps}_hardware_active'] = False
                        logger.error('dead_physical_layer_no_pps_interrupts_detected_on_pps', pps=pps)
                        errors.append(f'Dead physical layer: No PPS interrupts on {pps}')
            logger.debug('interrogating_value_daemon_state', value=cfg.daemon_type.value)
            cls._audit_time_daemon(dut, cfg.daemon_type, context_data)
            host_epoch = time.time()
            res_sys = dut.safe_run('date +%s', timeout_s=3.0)
            if not res_sys.ok or not res_sys.stdout.strip().isdigit():
                err_msg = 'Failed to read POSIX system time from DUT.'
                logger.error('err_msg', err_msg=err_msg)
                return ValidatorResult(passed=False, error_msg=err_msg, context=context_data)
            dut_sys_epoch = float(res_sys.stdout.strip())
            sys_drift = abs(host_epoch - dut_sys_epoch)
            metrics['host_to_dut_drift_s'] = round(sys_drift, 3)
            logger.debug('host_epoch_host_epoch_dut_epoch_dut_sys_epoch_drift_sys_drift_s', host_epoch=host_epoch, dut_sys_epoch=dut_sys_epoch, sys_drift=sys_drift)
            valid_rtcs = []
            for rtc in cfg.rtc_paths:
                logger.debug('probing_physical_hardware_rtc_rtc', rtc=rtc)
                res_rtc = dut.safe_run(f'cat {rtc}/since_epoch 2>/dev/null || date -u +%s -d "$(cat {rtc}/time 2>/dev/null)"', timeout_s=2.0)
                rtc_name = rtc.split('/')[-2] if '/' in rtc else rtc
                if res_rtc.ok and res_rtc.stdout.strip().isdigit():
                    rtc_epoch = float(res_rtc.stdout.strip())
                    rtc_drift = abs(dut_sys_epoch - rtc_epoch)
                    metrics[f'{rtc_name}_to_sys_drift_s'] = round(rtc_drift, 3)
                    context_data[f'{rtc_name}_detected'] = True
                    valid_rtcs.append(rtc_name)
                    if rtc_epoch < 1000000:
                        warn_msg = f'RTC {rtc_name} read near Epoch 0 (1970). Coin-cell battery is dead or missing.'
                        logger.warning('=' * 60)
                        logger.warning('hardware_warning_dead_rtc_battery_detected')
                        logger.warning('warn_msg', warn_msg=warn_msg)
                        logger.warning('=' * 60)
                        context_data['temporal_warnings'].append(warn_msg)
                else:
                    logger.debug('rtc_rtc_name_is_unresponsive_or_unmapped', rtc_name=rtc_name)
                    context_data[f'{rtc_name}_detected'] = False
            if not valid_rtcs and cfg.rtc_paths:
                warn_msg = 'No functional physical RTCs detected. System will lose time on reboot.'
                logger.warning('warn_msg', warn_msg=warn_msg)
                context_data['temporal_warnings'].append(warn_msg)
            if sys_drift > cfg.max_drift_s:
                if not cfg.force_host_sync:
                    logger.error('time_drift_val_s_exceeds_max_tolerance_max_drift_s_s', val=round(sys_drift, 1), max_drift_s=cfg.max_drift_s)
                    errors.append(f'Time drift ({round(sys_drift, 1)}s) exceeds max tolerance ({cfg.max_drift_s}s).')
                else:
                    taint_msg = f'DUT drifted {round(sys_drift, 1)}s. Forcing artificial Host sync. BOARD TEMPORALLY TAINTED.'
                    logger.warning('=' * 60)
                    logger.warning('temporal_taint_warning')
                    logger.warning('taint_msg', taint_msg=taint_msg)
                    logger.warning('future_cryptographic_certs_may_be_generated_under_an_artificial_epoch')
                    logger.warning('=' * 60)
                    context_data['temporal_warnings'].append(taint_msg)
                    context_data['is_temporally_tainted'] = True
                    logger.debug('[Time] Reaping native time daemons to prevent NTP/Chrony fighting...')
                    kill_targets = 'chronyd ntpd systemd-timesyncd ptp4l phc2sys'
                    dut.safe_run(f'killall -9 {kill_targets} >/dev/null 2>&1 || true', timeout_s=3.0)
                    logger.debug('[Time] Injecting POSIX epoch directly into DUT kernel...')
                    host_str = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(host_epoch))
                    sync_res = dut.safe_run(f"date -u -s '{host_str}'", timeout_s=3.0)
                    if sync_res.ok:
                        if valid_rtcs:
                            logger.debug('[Time] Flushing POSIX time down to hardware RTC silicon (hwclock)...')
                            dut.safe_run('hwclock --systohc -u', timeout_s=5.0)
                    else:
                        errors.append('Failed to force POSIX time injection.')
            else:
                context_data['is_temporally_tainted'] = False
                logger.info('temporal_state_is_healthy_drift_val_s', val=round(sys_drift, 3))
            passed = len(errors) == 0
            return ValidatorResult(passed=passed, error_msg=' | '.join(errors) if not passed else '', metrics=metrics, context=context_data)
        except TransportTimeoutError:
            logger.critical('[Time] FATAL: DUT hung while evaluating temporal states.')
            return ValidatorResult(passed=False, error_msg='DUT hung while evaluating temporal states.', context=context_data)
        except TransportConnectionError as e:
            logger.critical('fatal_transport_pipe_shattered_during_time_sync_e', e=e)
            return ValidatorResult(passed=False, error_msg=f'Transport pipe shattered during time sync: {e}', context=context_data)

    @classmethod
    async def async_verify_and_sync_time(cls, dut, cfg, *args, **kwargs):
        return await anyio.to_thread.run_sync(cls.verify_and_sync_time, dut, cfg, *args, **kwargs)

    @classmethod
    def _audit_time_daemon(cls, dut: DutTransport, daemon: TimeDaemonType, ctx: Dict[str, Any]) -> None:
        """Strategy router for interrogating the specific time daemon running on the DUT."""
        try:
            if daemon == TimeDaemonType.CHRONY:
                res = dut.safe_run('chronyc tracking 2>/dev/null', timeout_s=3.0)
                ctx['daemon_health'] = cls._parse_chrony_output(res.stdout) if res.ok and res.stdout.strip() else 'Unresponsive'
            elif daemon == TimeDaemonType.NTPD:
                res = dut.safe_run('ntpq -p 2>/dev/null', timeout_s=3.0)
                ctx['daemon_health'] = res.stdout.strip() if res.ok else 'Unresponsive'
            elif daemon == TimeDaemonType.SYSTEMD:
                res = dut.safe_run('timedatectl timesync-status 2>/dev/null', timeout_s=3.0)
                ctx['daemon_health'] = res.stdout.strip() if res.ok else 'Unresponsive'
            elif daemon == TimeDaemonType.PTP:
                res = dut.safe_run("pmc -u -b 0 'GET PORT_DATA_SET' 2>/dev/null", timeout_s=3.0)
                ctx['daemon_health'] = res.stdout.strip() if res.ok else 'Unresponsive'
        except TransportConnectionError:
            ctx['daemon_health'] = 'Transport Shattered'

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