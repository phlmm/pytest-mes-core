# src/pytest_mes_core/templates.py
from pathlib import Path

SAMPLE_TOML = """# ==========================================
# MES STATION HARDWARE CONFIGURATION
# ==========================================

[station_meta]
facility = "Sofia, Bulgaria (EMS Line 1)"
jig_id = "JIG-01-GOLDEN"

# --- SAFETY CRITICAL ---
[e_stop]
gpiochip = 0
line = 26
active_low = true
polling_interval_s = 0.05

# --- HOST PC HARDWARE ---
[psu_hardware]
vendor = "keysight"
visa_resource = "TCPIP0::192.168.1.100::inst0::INSTR"

[hid_scanners.operator_gun]
device_name_substring = "Barcode Scanner"
scan_timeout_s = 30

[host_can.kvaser_0]
interface = "can0"
bitrate = 500000

# --- DUT: SERIAL & NETWORK PROVISIONING ---
[ssh_targets.dut_usb_gadget]
ip_address = "192.168.11.1"
user = "root"
connect_timeout_s = 5.0

[boot_profilers.som_uart]
port = "/dev/ttyUSB0"
baudrate = 115200
timeout_s = 45.0
[boot_profilers.som_uart.milestones]
t_uboot = "U-Boot 2022.04"
t_kernel = "Starting kernel"
t_emmc_mount = "EXT4-fs.*mounted filesystem"
t_login = "imx8mp-evse login:"

# ==========================================
# DUT: HARDWARE PROTOCOLS (DICTIONARIES)
# ==========================================

[i2c_eeprom.mac_address_chip]
bus = 1
address = "0x50"
test_register = "0x00"
num_bytes = 16
page_size_bytes = 16
write_delay_s = 0.02

[mtd_flash.spi_nor]
mtd_dev = "/dev/mtd1"
sector_offset_hex = "0x010000"
erase_blocks = 1
test_bytes = 256
page_size_bytes = 256

[ethernet.eth0_gigabit]
interface = "eth0"
expected_speed_mbps = 1000
host_iperf_ip = "192.168.11.100"
iperf_duration_s = 3
iperf_min_mbps = 850.0

[can_bus.vehicle_iso15118]
dut_interface = "can0"
test_id = 0x123
payload = [222, 173, 190, 239]
timeout_s = 2.0

[gpio_loopback.contactor_relay_test]
tx_gpiochip = 2
tx_line = 4
rx_gpiochip = 3
rx_line = 10
settling_time_s = 0.05

[adc.temperature_probe]
iio_device = 0
channel = 1
samples = 10

# --- NEW: Bare-Metal MMIO Registers ---
[mmio_registers.iomuxc_uart2_rxd]
address_hex = "0x3033024C"
data_width = 32
bit_mask_hex = "0x00000007"
expected_value_hex = "0x00000001"

# --- NEW: Custom Vendor Binaries ---
[custom_executables.rf_calibration]
binary_path = "/usr/bin/rf_cal"
arguments = "--mode=test"
expected_exit_code = 0
timeout_s = 30.0
log_file_path = "/tmp/rf_cal.log"

# ==========================================
# BACKGROUND SYSFS TELEMETRY PROFILES
# ==========================================

# Profile 1: Used during the 60-second Ethernet DMA / CPU Stress Test
[sysfs_profilers.halt_stress_monitor]
polling_interval_s = 1.0

# NOTE: Targets now use the scaled Unit configuration!
[sysfs_profilers.halt_stress_monitor.targets.cpu_freq]
path = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"
scale = 0.001
unit = "MHz"

[sysfs_profilers.halt_stress_monitor.targets.eth_drops]
path = "/sys/class/net/eth0/statistics/rx_dropped"
scale = 1.0
unit = "pkts"

[sysfs_profilers.halt_stress_monitor.targets.soc_temp]
path = "/sys/class/thermal/thermal_zone0/temp"
scale = 0.001
unit = "C"

# ==========================================
# FAILURE FORENSICS (POST-MORTEM)
# ==========================================

# If any test containing "ethernet" or "dma" in its python name fails, run these:
[post_mortem_dumps]
ethernet = [
    "ethtool -S eth0",              # Dump PHY MAC statistics
    "ethtool -d eth0",              # Dump raw PHY silicon registers
    "dmesg | tail -n 50"            # Grab the last 50 lines of the kernel buffer
]

# If any test containing "i2c" or "eeprom" fails, run this:
eeprom = [
    "i2cdetect -y 1",               # Scan the bus to see if the chip disappeared
    "cat /sys/kernel/debug/regmap/i2c-1/registers" # Dump the regmap
]

# If any test containing "can" fails, dump the FlexCAN registers:
can = [
    "ip -details -statistics link show can0",
    "devmem 0x308C0000 32"          # NXP i.MX8 FlexCAN Base Register Dump
]
"""

def generate_sample_config(filepath: Path = Path("station_env.toml")) -> None:
    """Generates the boilerplate station configuration."""
    if not filepath.exists():
        with open(filepath, "w") as f:
            f.write(SAMPLE_TOML)
        print(f"[MES Core] Generated sample configuration at: {filepath.absolute()}")
    else:
        print(f"[MES Core] File {filepath.absolute()} already exists. Refusing to overwrite.")
