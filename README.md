# Pytest MES Core (`pytest-mes-core`)

**Enterprise-Grade Manufacturing Execution System (MES) Core for Hardware Validation**

`pytest-mes-core` is a highly defensive, physics-aware, and strictly typed hardware testing framework built on top of `pytest`. It is designed to run 24/7 on factory floors, orchestrating high-voltage power supplies, managing zero-leakage SSH connections, and executing highly accelerated life tests (HALT) on embedded Linux systems.

## Core Architectural Pillars

1. **The Indisputable BOM:** Hardware configurations are strictly enforced via Pydantic and TOML. If a station lacks a barcode scanner or defines an invalid I2C address, the framework aborts before drawing a single amp of power.
2. **Zero-Leakage Teardowns:** Whether a test passes, panics, times out, or the physical E-Stop is smashed, power supplies will safely ramp down to 0V and SSH zombie processes are violently reaped.
3. **Immutable Telemetry:** All test physics (voltages, frequencies, durations) are atomically flushed to a flat `JSONL` file with strict OS-level locks, ready for instant Grafana/Pandas ingestion. No database drivers required on the edge PC.
4. **Context-Aware Forensics:** When a board fails, the framework intercepts the failure, auto-executes hardware-specific diagnostic commands (like `ethtool -d` or `i2cdetect`), and saves a forensic `.log` file for Root Cause Analysis.

---

## System Architecture

This framework enforces a strict **Host vs. Target** boundary. The Host PC runs the Python test logic and controls lab instruments. The Target (DUT) executes lightweight POSIX bash commands to manipulate its own silicon.

### 1. Host PC Requirements (The Test Jig)
The machine physically wired to the test jig running the Pytest runner.

* **OS:** Linux (Ubuntu 22.04 LTS or Debian 12 recommended for `udev` and `evdev` stability).
* **Python:** `>= 3.11`
* **System Packages:**
  * `libgpiod-dev` (For physical E-Stop monitoring)
  * `libvisa-dev` (For SCPI Power Supply control)
  * `mfgtools` **(Crucial: Provides the NXP `uuu` utility for TEZI provisioning)**
* **Hardware Interfacing & Permissions:** Ensure the active user is in the `dialout` and `input` groups to read UART TTYs and USB Barcode Scanners.
  * **USB Recovery Permissions:** The Host PC must have the NXP/Toradex `udev` rules installed. If you do not install these, `uuu` will be blocked by the OS and fail to enumerate the DUT.

  *To install the uuu udev rules on the Host:*
  ```bash
  wget [https://raw.githubusercontent.com/nxp-imx/mfgtools/master/uuu/uuu.rules](https://raw.githubusercontent.com/nxp-imx/mfgtools/master/uuu/uuu.rules) -O /tmp/uuu.rules
  sudo cp /tmp/uuu.rules /etc/udev/rules.d/99-uuu.rules
  sudo udevadm control --reload-rules
  sudo udevadm trigger


# Development
```shell
# 1. Purge the namespace hijackers
pip uninstall -y serial can

# 2. Let the build system do its job!
# The "-e ." command tells pip: "Read pyproject.toml and install everything listed in the dependencies array!"
# This will automatically and correctly install pyserial, python-can, fabric, tenacity, and pyvisa.
pip install -e .

# 3. Ensure the hermetic testing simulators are installed for the Unit Tests
pip install pytest-mock pyvisa-sim

# 4. Execute the proofs
pytest tests/unit/
```
