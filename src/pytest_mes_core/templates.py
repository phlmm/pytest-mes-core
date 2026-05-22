import anyio
# src/pytest_mes_core/templates.py
from pathlib import Path

SAMPLE_TOML = """# ==========================================
# MES STATION HARDWARE CONFIGURATION
# ==========================================
# HARDWARE ABSTRACTION RULES:
# enabled = true | false  -> Does this hardware physically exist on this jig?
# required = true | false -> If it fails to connect, should it abort the entire test suite?

[station_meta]
facility = "Sofia, Bulgaria (EMS Line 1)"
jig_id = "JIG-01-GOLDEN"

[telemetry]
exporter_type = "jsonl"
log_directory = "/var/log/mes_core"

# ==========================================
# HOST PC: SAFETY & INSTRUMENTATION
# ==========================================

[e_stop]
enabled = true
required = true
gpiochip = 0
line = 26
active_low = true
polling_interval_s = 0.05

[psu_hardware]
enabled = true
required = true
vendor = "keysight"
visa_resource = "TCPIP0::192.168.1.100::inst0::INSTR"

# ==========================================
# THE MASTER TRANSPORT ROUTER (FAILOVER MATRIX)
# ==========================================

[ssh_targets.primary]
enabled = true
required = true
ip_address = "192.168.11.1"
user = "root"
connect_timeout_s = 5.0

[host_serial.debug_port]
enabled = true
required = false   # If the FTDI cable is broken, SSH will still run the tests
port = "/dev/ttyUSB0"
baudrate = 115200
timeout_s = 1.0

# ==========================================
# PROVISIONING: BOOTSTRAP, JTAG, & ICSP
# ==========================================

[bootstrap]
enabled = true
required = true
gpiochip = 1
boot_pins = [17, 18, 27, 22] # BOOT_MODE[0..3]
reset_pin = 23
reset_active_low = true
[bootstrap.boot_modes]
recovery = [0, 0, 0, 1]      # USB Serial Downloader
emmc     = [0, 1, 1, 0]      # Normal boot from eMMC

# Example: Critical component that MUST flash successfully
[jtag_targets.main_coprocessor]
enabled = true
required = true    # FATAL if the J-Link probe is unplugged or dead
interface_cfg = "interface/jlink.cfg"
target_cfg = "target/stm32h7x.cfg"
rpc_port = 4444
firmware_path = "/opt/mes/firmware/main_copro_v1.0.bin"

# Example: Optional component that is nice to have, but not show-stopping
[microchip_targets.optional_power_sequencer]
enabled = true
required = false   # If PICkit is broken, log warning, but keep testing the main MCU
device = "PIC18F45K22"
tool_serial = "BUR221234567"
firmware_path = "/opt/mes/firmware/pwr_seq_v1.2.hex"

# ==========================================
# DUT: HARDWARE VALIDATION PROTOCOLS
# ==========================================

[time_sync]
enabled = true
required = true
max_drift_s = 5.0
daemon_type = "chrony"
rtc_paths = ["/sys/class/rtc/rtc0"]

[i2c_eeprom.mac_address_chip]
enabled = true
required = true
bus = 1
address = "0x50"
test_register = "0x00"
num_bytes = 16
page_size_bytes = 16
write_delay_s = 0.02

# Example: Bypassed Component (Completely ignored by the framework)
[ethernet.eth1_secondary]
enabled = false    # This specific jig doesn't have a second Ethernet cable installed
required = true    # Ignored because enabled = false
interface = "eth1"
expected_speed_mbps = 1000
host_iperf_ip = "192.168.11.101"

[ethernet.eth0_gigabit]
enabled = true
required = true
interface = "eth0"
expected_speed_mbps = 1000
host_iperf_ip = "192.168.11.100"
iperf_duration_s = 3
iperf_min_mbps = 850.0

[can_bus.vehicle_iso15118]
enabled = true
required = true
dut_interface = "can0"
test_id = 0x123
payload = [222, 173, 190, 239]
timeout_s = 2.0

[adc.temperature_probe]
enabled = true
required = true
sensor_name = "ads1015"
channel = 1
samples = 10
min_v = 0.5
max_v = 4.2

[efuse]
enabled = true
required = true
nvmem_path = "/sys/bus/nvmem/devices/imx-ocotp0/nvmem"
dmesg_grep_pattern = "ocotp|nvmem|fuse|imx8mp|sec_boot"
require_32bit_alignment = true

# ==========================================
# BACKGROUND SYSFS TELEMETRY PROFILES
# ==========================================

[sysfs_profilers.halt_stress_monitor]
enabled = true
required = false
polling_interval_s = 1.0

[sysfs_profilers.halt_stress_monitor.targets.cpu_freq]
path = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"
scale = 0.001
unit = "MHz"

[sysfs_profilers.halt_stress_monitor.targets.soc_temp]
path = "/sys/class/thermal/thermal_zone0/temp"
scale = 0.001
unit = "C"

# ==========================================
# FAILURE FORENSICS (POST-MORTEM)
# ==========================================

[post_mortem_dumps]
ethernet = [
    "ethtool -S eth0",              # Dump PHY MAC statistics
    "ethtool -d eth0",              # Dump raw PHY silicon registers
    "dmesg | tail -n 50"            # Grab the last 50 lines of the kernel buffer
]
# If a test executing a custom vendor binary fails, extract the user-space crash log
user_space_crash = [
    "coredumpctl info --no-pager | tail -n 50",   # Grabs the most recent segfault backtrace
    "journalctl -p err..emerg -n 20 --no-pager"   # Grabs recent systemd errors
]

# If a test causes a hard reboot (e.g., driver load test), extract the kernel panic
kernel_panic = [
    "cat /sys/fs/pstore/dmesg-ramoops-0",         # The golden kernel panic trace
    "cat /sys/fs/pstore/console-ramoops-0"
]
"""

def generate_sample_config(filepath: Path = Path("/etc/mes/station.toml")) -> None:
    """Generates the boilerplate station configuration for new factory lines."""
    if not filepath.parent.exists():
        try:
            filepath.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            print(f"[ERROR] Permission denied creating {filepath.parent}. Try running with sudo or specify a local path.")
            return

    if not filepath.exists():
        try:
            with open(filepath, "w") as f:
                f.write(SAMPLE_TOML)
            print(f"[SUCCESS] MES Core generated sample configuration at: {filepath.absolute()}")
            print("[INFO] Edit this file to match your physical Jig wiring before running the test suite.")
        except Exception as e:
            print(f"[ERROR] Failed to write config: {e}")
    else:
        print(f"[WARNING] File {filepath.absolute()} already exists. Refusing to overwrite to protect existing jigs.")
