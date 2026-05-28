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

## Module Ecosystem

The `pytest-mes-core` framework is divided into distinct, strictly-typed modules to handle different layers of hardware testing:

* **`pytest_mes_core.config`**: Parses the physical Station Bill of Materials (BOM) from TOML. Validates that all expected test jig hardware exists before execution.
* **`pytest_mes_core.state_machine`**: Dual physical Finite State Machines. `EmbeddedLinuxStateMachine` transitions ELinux targets between OS and U-Boot, while `BareMetalStateMachine` handles MCUs (HALTED, ENERGIZED, OTA).
* **`pytest_mes_core.transports`**: Contains the `FailoverTransport` matrix for ELinux, and `PyOcdTransport` for Bare-Metal SWD/JTAG debug probes.
* **`pytest_mes_core.protocols`**: Target-side validation logic. Includes high-level Pydantic-based `RPC` clients alongside abstractions for `I2C`, `CAN`, `SPI`, and `Ethernet`.
* **`pytest_mes_core.host_adapters` & `instruments`**: Host-side drivers for barcode scanners, SCPI power supplies, JTAG debuggers, and CAN bus adapters.
* **`pytest_mes_core.provisioning`**: Bootstraps blank silicon (e.g., using NXP `uuu` for Linux, or `Stm32Provisioner` for mass-flashing `.bin` payloads to MCUs).
* **`pytest_mes_core.telemetry`**: A composite telemetry router outputting to JSONL, Markdown, and TXT receipts. Intercepts failures to automatically harvest `dmesg` and `coredumpctl`.

---

## Installation & Development

The framework uses `hatchling` and isolates C-extension dependencies to allow cross-platform development. Engineers can install the core framework on Windows/macOS to write tests, while factory jigs install the full hardware suite.

### Setting up a Clean Environment (Recommended)

### Using as a Dependency in Another Project

To install the framework directly from Git into another project's virtual environment:

```bash
# On Linux (installs physical hardware drivers):
pip install "pytest-mes-core[all] @ git+https://github.com/phlmm/pytest-mes-core.git"

# On Windows/macOS (installs mock dependencies only):
pip install "pytest-mes-core[dev,docs,instruments] @ git+https://github.com/phlmm/pytest-mes-core.git"
```

### Local Development Setup

To run the unit tests and work on the module locally, it is highly recommended to use a clean Python virtual environment.

```bash
# 1. Create a clean virtual environment
python3 -m venv .venv

# 2. Activate the virtual environment
# On Linux/macOS:
source .venv/bin/activate
# On Windows:
# .venv\Scripts\activate

# 3. Upgrade pip, setuptools, and wheel
pip install --upgrade pip setuptools wheel

# 4. Install the framework via the modern build system
# For Lab PCs/Jigs (Installs physical Linux drivers like evdev/gpiod):
pip install -e ".[linux-hardware,instruments]"

# For Developer Laptops running the unit tests (Installs all mock dependencies):
# On Linux:
pip install -e ".[all]"
# On Windows/macOS (avoids Linux-only hardware drivers):
pip install -e ".[dev,docs,instruments]"

# 5. Execute the proof tests
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

### Strictly-Typed RPC Framework (Pydantic to C-Structs)

When communicating with heterogeneous targets (like Bare-Metal MCUs over UART or ELinux daemons over TCP), `pytest-mes-core` provides a unified `async_invoke` RPC API. It automatically compiles strictly-typed `Pydantic` schemas down into dense binary C-Structs (for MCUs) or JSON payloads (for ELinux).

```python
import pytest
from pytest_mes_core.protocols.rpc_schemas import RpcMessage
from pydantic import Field

# 1. Define the physical binary layout
class OtaTriggerRequest(RpcMessage):
    METHOD_ID = 0x02
    STRUCT_FORMAT = "<I I" # Maps to two Little-Endian uint32 variables in C firmware
    image_size_bytes: int
    image_crc32: int

class OtaTriggerResponse(RpcMessage):
    METHOD_ID = 0x02
    STRUCT_FORMAT = "<B" # Maps to a uint8
    accepted: bool

@pytest.mark.anyio
async def test_mcu_rejects_invalid_ota(mcu_rpc_client):
    req = OtaTriggerRequest(image_size_bytes=1024, image_crc32=0xDEADBEEF)
    
    # 2. Automatically compiles to bytes, COBS-frames it, transmits, and validates response!
    response = await mcu_rpc_client.async_invoke(req, response_type=OtaTriggerResponse)
    
    assert isinstance(response, OtaTriggerResponse)
    assert response.accepted is False
```

### Writing an Asynchronous Factory Test

The framework supports a **Dual-Pipeline Architecture**, providing native `async`/`await` support for high-throughput, parallel hardware testing using `anyio`. This allows you to orchestrate multiple DUTs concurrently on a single test jig without blocking the event loop.

```python
import pytest
import anyio
from pytest_mes_core.state_machine import DutState
from pytest_mes_core.telemetry.profiler import AsyncHardwareProfiler

@pytest.mark.anyio
async def test_parallel_firmware_flash(dut_transport, fsm, psu_hardware):
    # 1. Start the hardware watchdog asynchronously
    await dut_transport.watchdog.async_start()
    
    # 2. Start the Continuous Profiler in the background
    async with AsyncHardwareProfiler(dut_transport, psu_hardware, interval_s=0.5) as profiler:
        # 3. Boot to bootloader asynchronously (yields CPU to other DUTs)
        await fsm.async_hw_boot_to_bootloader()
        
        # 4. Flash firmware while tracking power usage and thermals
        res = await dut_transport.async_safe_run("fastboot flash boot_a boot.img", timeout_s=120.0)
        assert res.ok
        
        # 5. Boot to OS
        await fsm.async_hw_boot_to_os()
    
    # 6. Retrieve continuous background telemetry!
    summary = profiler.summarize()
    print(f"Peak flash current: {summary['peak_current_a']}A")
    print(f"Peak CPU Temp: {summary['peak_temp_c']}°C")
    
    await dut_transport.watchdog.async_stop()
```

### Profiling an Entire Test Automatically

If you want to automatically collect power and thermal telemetry for the *entire duration of a test* without writing the `async with` block every time, you can create a custom Pytest fixture in your `conftest.py`. This fixture will yield the profiler to your test and seamlessly attach the peak metrics to your JSONL telemetry records when the test finishes.

```python
# conftest.py
import pytest
from pytest_mes_core.telemetry.profiler import AsyncHardwareProfiler

@pytest.fixture
async def hardware_profiler(dut_transport, psu_hardware, request):
    """Automatically profiles hardware metrics for the duration of a single test."""
    async with AsyncHardwareProfiler(dut_transport, psu_hardware, interval_s=0.5) as profiler:
        yield profiler
        
        # When the test finishes, attach the peak metrics to the MES JSONL report!
        record = getattr(request.node, 'mes_telemetry_record', None)
        if record:
            record.context.update(profiler.summarize())

# test_factory.py
@pytest.mark.anyio
async def test_full_system_stress(dut_transport, hardware_profiler):
    # The profiler is already running in the background!
    
    # 1. Engage heavy compute workload
    await dut_transport.async_safe_run("stress-ng --matrix 0 --timeout 60s")
    
    # 2. You can access live stats mid-test if needed
    summary = hardware_profiler.summarize()
    assert summary.get('peak_current_a', 0) < 3.0, "Board drew too much current!"
    assert summary.get('peak_temp_c', 0) < 85.0, "Thermal limits exceeded!"
```

*(Note: If you want to log power usage for the entire Pytest **session** rather than per-test, you can simply set `enable_data_logging = true` in your TOML config. The `psu_hardware` fixture will automatically configure the PSU's internal SCPI datalogger during setup and download the CSV payload at the end of the run).*

### Execution
Run the test suite on the factory floor, defining the operator and the hardware BOM:
```bash
pytest projects/evse_board/ -p mes_core --operator-id=FILIP-01 --env-config=projects/custom/station_env.toml
```
