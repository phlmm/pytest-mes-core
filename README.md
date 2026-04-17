# Pytest MES Core (`pytest-mes-core`)

**Enterprise-Grade Manufacturing Execution System (MES) Core for Hardware Validation**

`pytest-mes-core` is a highly defensive, physics-aware, and strictly typed hardware testing framework built on top of `pytest`. It is designed to run 24/7 on factory floors, orchestrating high-voltage power supplies, managing zero-leakage SSH connections, routing telemetry, and executing highly accelerated life tests (HALT) on embedded Linux systems.

## Core Architectural Pillars

1. **The Indisputable BOM:** Hardware configurations are strictly enforced via Pydantic and TOML. If a station lacks a barcode scanner or defines an invalid I2C address, the framework aborts before drawing a single amp of power.
2. **Physical State Machine (FSM):** The framework does not assume hardware state. An embedded Linux FSM dynamically probes the DUT, intercepts bootloaders (U-Boot), enforces partitions, and routes the silicon to the required physical state (e.g., `POWER_OFF`, `OS_USERLAND`, `RECOVERY`) before yielding to the test logic.
3. **The Failover Transport Matrix:** If the DUT kernel panics or the SSH network stack shatters mid-test, the framework catches the broken pipe, permanently fails over to an out-of-band Serial Console (UART), and intelligently resumes testing if the command is marked idempotent.
4. **Hardware-Aware Auto-Healing:** Transient EMI noise happens. Using the `@pytest.mark.hardware_retry(retries=2)` decorator, the framework catches test failures, marks the hardware state as `DIRTY`, forces a violent hard-power reboot to clear RAM, and reruns the test.
5. **Composite Telemetry & Forensics:** A composite router simultaneously emits data to four discrete sinks:
   * **Machine Sink:** Immutable, strict `JSONL` streams for Datadog/Grafana.
   * **Operator Sink:** Tiny `TXT` receipts summarizing First-Pass Yield (FPY).
   * **Developer Sink:** Rich `Markdown` reports containing full Python tracebacks for IDEs.
   * **Customer Sink:** Self-contained `HTML` End-of-Line (EOL) Certificates.
   *(If a test fails, automated forensic dumps—like `dmesg` or `i2cdetect`—are automatically harvested and attached to these reports).*

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
  wget https://raw.githubusercontent.com/nxp-imx/mfgtools/master/uuu/uuu.rules -O /tmp/uuu.rules
  sudo cp /tmp/uuu.rules /etc/udev/rules.d/99-uuu.rules
  sudo udevadm control --reload-rules
  sudo udevadm trigger
  ```

---

## Installation & Development

The framework uses `hatchling` and isolates C-extension dependencies to allow cross-platform development. Engineers can install the core framework on Windows/macOS to write tests, while factory jigs install the full hardware suite.

```shell
# 1. Purge legacy namespace hijackers (if migrating from old setups)
pip uninstall -y serial can

# 2. Install the framework via the modern build system
# For Lab PCs/Jigs (Installs physical Linux drivers like evdev/gpiod):
pip install -e ".[all]"

# For Developer Laptops (Core framework only, bypasses Linux C-compilers):
pip install -e .

# 3. Ensure the hermetic testing simulators are installed for Unit Tests
pip install pytest-mock pyvisa-sim

# 4. Execute the proofs
pytest tests/
```

---

## Writing a Factory Test

Tests require zero boilerplate. Pytest fixtures dynamically manage the hardware context and telemetry wrappers.

```python
import pytest
from pytest_mes_core.state_machine import DutState

# 1. Enforce the physical silicon state before the test runs
@pytest.mark.requires_state(DutState.OS_USERLAND)
# 2. Allow up to 2 dirty hard-reboots if factory EMI corrupts the bus
@pytest.mark.hardware_retry(2)
def test_secure_element_i2c(dut_transport, telemetry_sink):

    # safe_run() mathematically parses exit codes across SSH or UART
    res = dut_transport.safe_run("i2cget -y 1 0x42", check_exit_code=True)

    # 3. Attach deep context directly to the active JSONL telemetry session
    telemetry_sink.context.custom_data["crypto_chip_id"] = res.stdout

    assert res.stdout == "0xABCD"
```

### Execution
Run the test suite on the factory floor, defining the operator and the hardware BOM:
```bash
pytest projects/evse_board/ \
    -p mes_core \
    --operator-id=FILIP-01 \
    --env-config=projects/custom/station_env.toml
```
