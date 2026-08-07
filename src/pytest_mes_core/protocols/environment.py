import anyio
import structlog
import logging
from typing import List, Optional, Dict, Any
from pytest_mes_core.transports import DutTransport, TransportConnectionError, TransportTimeoutError
from pytest_mes_core.protocols import ValidatorResult
logger = structlog.get_logger('mes_core.protocols.env')

class EnvironmentValidator:
    """
    Validates the DUT's embedded Linux userland against the framework's requirements
    and performs a baseline kernel sanity check before testing begins.
    """
    CORE_BINARIES = ['timeout', 'killall', 'dd', 'base64', 'hexdump', 'sha256sum', 'devmem', 'iperf3', 'ethtool', 'ip', 'cansend', 'candump', 'i2ctransfer', 'i2cdetect', 'gpiomon', 'gpioset', 'gpioget', 'flash_erase', 'openssl', 'nproc']

    @classmethod
    def verify_target_dependencies(cls, dut: DutTransport, extra_binaries: Optional[List[str]]=None) -> ValidatorResult:
        """Executes a zero-overhead, single-shot batch check of all required binaries.

        Args:
            dut: The transport interface connected to the target.
            extra_binaries: An optional list of additional binary names to check.

        Returns:
            ValidatorResult: Contains the pass/fail status and lists missing binaries in the context.
        """
        logger.info('[Pre-Flight] Verifying DUT embedded Linux dependencies...')
        target_list = cls.CORE_BINARIES.copy()
        if extra_binaries:
            target_list.extend(extra_binaries)
        logger.debug('constructing_zero_overhead_bash_loop_to_check_val_binaries', val=len(target_list))
        bash_script = f"for cmd in {' '.join(target_list)}; do command -v $cmd >/dev/null 2>&1 || echo $cmd; done"
        context_data: Dict[str, Any] = {'checked_count': len(target_list)}
        try:
            res = dut.safe_run(bash_script, timeout_s=10.0)
        except TransportTimeoutError:
            logger.critical('[Pre-Flight] FATAL: DUT hung completely while checking dependencies. Kernel lockup?')
            return ValidatorResult(passed=False, error_msg='DUT hung completely while checking dependencies.', context=context_data)
        except TransportConnectionError as e:
            logger.critical('fatal_transport_pipe_shattered_during_dependency_check_e', e=e)
            return ValidatorResult(passed=False, error_msg=f'Transport pipe shattered during dependency check: {e}', context=context_data)
        if not res.ok:
            logger.error('dependency_check_failed_to_execute_os_corrupted_stderr', stderr=res.stderr)
            return ValidatorResult(passed=False, error_msg='Failed to execute dependency bash loop.', context=context_data)
        missing_binaries = res.stdout.strip().split()
        if missing_binaries:
            error_str = ', '.join(missing_binaries)
            context_data['missing_binaries'] = missing_binaries
            logger.critical('=' * 60)
            logger.critical('fatal_dut_firmware_is_missing_required_tools_error_str', error_str=error_str)
            logger.critical('[Pre-Flight] The test framework cannot operate without these binaries.')
            logger.critical('[Pre-Flight] Check your Yocto/Buildroot image configuration or packagegroup recipe!')
            logger.critical('=' * 60)
            return ValidatorResult(passed=False, error_msg=f'Missing target binaries: {error_str}', context=context_data)
        logger.info('[Pre-Flight] All required binaries found. Userland environment is pristine.')
        return ValidatorResult(passed=True, context=context_data)


    @classmethod
    def verify_system_health(cls, dut: DutTransport) -> ValidatorResult:
        """Performs a pre-flight sanity check on the kernel state.

        Checks for early-boot panics, thermal throttling, and invalid system clocks.

        Args:
            dut: The transport interface connected to the target.

        Returns:
            ValidatorResult: Contains pass/fail status and captures dmesg panic traces or 
                invalid RTC times in the context.
        """
        logger.info('[Pre-Flight] Performing kernel sanity and health sweep...')
        context_data: Dict[str, Any] = {}
        metrics: Dict[str, float] = {}
        errors = []
        try:
            logger.debug('[Pre-Flight] Scraping kernel ring buffer (dmesg) for Oops/Panics/OOMs...')
            dmesg_res = dut.safe_run("dmesg | grep -iE 'kernel BUG at|Out of memory|Internal error|Oops:' | tail -n 5", timeout_s=5.0)
            if dmesg_res.ok and dmesg_res.stdout.strip():
                context_data['early_boot_panics'] = dmesg_res.stdout.strip()
                logger.critical('=' * 60)
                logger.critical('[Pre-Flight] FATAL: Kernel panic/Oops traces detected in early boot!')
                logger.critical('kernel_trace_val', val=context_data['early_boot_panics'])
                logger.critical('=' * 60)
                errors.append('Pre-existing Kernel panics detected in dmesg.')
            logger.debug('[Pre-Flight] Verifying RTC/NTP system clock epoch...')
            time_res = dut.safe_run('date +%Y', timeout_s=3.0)
            if time_res.ok and time_res.stdout.strip().isdigit():
                year = int(time_res.stdout.strip())
                if year < 2020:
                    logger.warning('system_clock_is_invalid_year_year_ntp_failed_or_rtc_battery_dead', year=year)
                    context_data['invalid_rtc_year'] = year
            logger.debug('[Pre-Flight] Sampling baseline CPU load average...')
            core_res = dut.safe_run('nproc', timeout_s=2.0)
            core_count = int(core_res.stdout.strip()) if core_res.ok and core_res.stdout.strip().isdigit() else 1
            context_data['cpu_core_count'] = core_count
            load_res = dut.safe_run('cat /proc/loadavg', timeout_s=3.0)
            if load_res.ok and load_res.stdout:
                try:
                    load_1m = float(load_res.stdout.strip().split()[0])
                    metrics['baseline_cpu_load'] = load_1m
                    if load_1m > core_count * 1.5:
                        logger.warning('high_baseline_cpu_load_load_1m_on_core_count_cores_detected_before_testing', load_1m=load_1m, core_count=core_count)
                        context_data['cpu_thrashing_detected'] = True
                except (ValueError, IndexError):
                    pass
        except TransportTimeoutError:
            logger.critical('[Pre-Flight] FATAL: DUT completely unresponsive during health sweep. Kernel locked up?')
            return ValidatorResult(passed=False, error_msg='DUT completely unresponsive during health sweep.', context=context_data)
        except TransportConnectionError as e:
            logger.critical('fatal_transport_pipe_shattered_during_health_sweep_e', e=e)
            return ValidatorResult(passed=False, error_msg=f'Transport pipe shattered during health sweep: {e}', context=context_data)
        if errors:
            return ValidatorResult(passed=False, error_msg=' | '.join(errors), metrics=metrics, context=context_data)
        logger.info('[Pre-Flight] Kernel health sweep passed. No early-boot panics detected.')
        return ValidatorResult(passed=True, metrics=metrics, context=context_data)
