import structlog
from pathlib import Path
from typing import Any, Dict, List, Optional
from contextlib import contextmanager
from pytest_mes_core.host_adapters import BaseHostAdapter, HostAdapterError
from pytest_mes_core.host_adapters import hardware_mutex, HostMutexTimeoutError

logger = structlog.get_logger('mes_core.host_adapters.microchip')

MICROCHIP_USB_VID = '04d8'
KNOWN_PROBES = {
    '9012': 'PK4',   # MPLAB PICkit 4
    '9018': 'PK5',   # MPLAB PICkit 5
    '9009': 'PK3',   # MPLAB PICkit 3
    '9014': 'ICD4',  # MPLAB ICD 4
    '9015': 'ICD5',  # MPLAB ICD 5
    '9010': 'SNAP',  # MPLAB Snap
}

def detect_microchip_probes() -> List[Dict[str, str]]:
    """Scans host USB bus via sysfs for connected Microchip debug probes (VID 04d8).

    Returns:
        List of dicts with keys: vid, pid, serial, product, tool_type, sysfs_path
    """
    probes: List[Dict[str, str]] = []
    usb_dir = Path('/sys/bus/usb/devices')
    if not usb_dir.exists():
        return probes
    for dev_path in usb_dir.iterdir():
        vid_file = dev_path / 'idVendor'
        pid_file = dev_path / 'idProduct'
        if vid_file.exists() and pid_file.exists():
            try:
                vid = vid_file.read_text().strip().lower()
                pid = pid_file.read_text().strip().lower()
                if vid == MICROCHIP_USB_VID:
                    serial = (dev_path / 'serial').read_text().strip() if (dev_path / 'serial').exists() else ''
                    product = (dev_path / 'product').read_text().strip() if (dev_path / 'product').exists() else ''
                    tool_type = KNOWN_PROBES.get(pid, 'UNKNOWN')
                    probes.append({
                        'vid': vid,
                        'pid': pid,
                        'serial': serial,
                        'product': product,
                        'tool_type': tool_type,
                        'sysfs_path': str(dev_path),
                    })
            except Exception:
                pass
    return probes

class HostPickitAdapter(BaseHostAdapter):
    """
    Manages the physical lock on a Microchip PICkit/ICD probe.
    Ensures parallel Pytest workers do not collide on the same USB interface.
    Supports auto-discovery, probe type resolution, and pre-flight validation.
    """

    def __init__(self, tool_serial: Optional[str] = None, mutex_timeout_s: float = 60.0, auto_detect: bool = True):
        self.mutex_timeout_s = mutex_timeout_s
        self._mutex_context = None
        self.probe_info: Optional[Dict[str, str]] = None

        detected = detect_microchip_probes()
        serial_requested = (tool_serial or '').strip()

        if serial_requested in ('', 'auto') and auto_detect:
            if len(detected) == 1:
                self.tool_serial = detected[0]['serial']
                self.probe_info = detected[0]
                logger.info(
                    'auto_detected_microchip_probe',
                    serial=self.tool_serial,
                    product=detected[0]['product'],
                    tool_type=detected[0]['tool_type']
                )
            elif len(detected) == 0:
                if not serial_requested:
                    raise HostAdapterError('No Microchip PICkit/ICD probes found on host USB bus and no tool_serial configured.')
                self.tool_serial = serial_requested
            else:
                probes_str = ', '.join(f"{p['product']} ({p['serial']})" for p in detected)
                raise HostAdapterError(
                    f'Multiple Microchip probes detected [{probes_str}]. Please configure tool_serial explicitly in station TOML.'
                )
        else:
            self.tool_serial = serial_requested
            if detected:
                match = next((p for p in detected if p['serial'] == self.tool_serial), None)
                if match:
                    self.probe_info = match
                    logger.debug('matched_configured_microchip_probe', serial=self.tool_serial, product=match['product'])
                else:
                    detected_serials = [p['serial'] for p in detected if p['serial']]
                    logger.warning(
                        'configured_probe_not_found_on_usb',
                        configured=self.tool_serial,
                        available=detected_serials
                    )

    def __enter__(self) -> 'HostPickitAdapter':
        """Acquires a cross-process mutex lock for the physical PICkit probe.

        Returns:
            HostPickitAdapter: The locked hardware adapter instance.

        Raises:
            HostAdapterError: If the probe cannot be locked within the timeout.
        """
        logger.debug('acquiring_hardware_lock_for_probe_tool_serial', tool_serial=self.tool_serial)
        try:
            self._mutex_context = hardware_mutex(resource_name=f'pickit_{self.tool_serial}', timeout_s=self.mutex_timeout_s)
            self._mutex_context.__enter__()
            logger.info('hardware_lock_successfully_acquired_for_probe_tool_serial', tool_serial=self.tool_serial)
            return self
        except HostMutexTimeoutError as e:
            err_msg = f'Failed to acquire PICkit {self.tool_serial}: {e}'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise HostAdapterError(err_msg)

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """ZERO-LEAKAGE: Release the OS-level hardware lock."""
        if self._mutex_context:
            logger.debug('zero_leakage_releasing_lock_on_tool_serial', tool_serial=self.tool_serial)
            self._mutex_context.__exit__(exc_type, exc_val, exc_tb)
            self._mutex_context = None

    @contextmanager
    def lock_usb_bus(self):
        """Allows tests to use 'with pickit.lock_usb_bus():' for better readability,
        while routing through the required __enter__/__exit__ methods.

        Yields:
            HostPickitAdapter: The locked hardware adapter instance.
        """
        with self:
            yield self

    @property
    def tool_type(self) -> str:
        if self.probe_info and 'tool_type' in self.probe_info:
            return self.probe_info['tool_type']
        return 'UNKNOWN'

