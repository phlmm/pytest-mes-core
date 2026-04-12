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
* **Hardware Interfacing:** * Ensure the active user is in the `dialout` and `input` groups to read UART TTYS and USB Barcode Scanners.

### 2. Target Requirements (The DUT)
The embedded device under test (e.g., Toradex i.MX8 SoM).
The framework operates via SSH and expects a standard BusyBox or Debian userland. Your Yocto/Buildroot image **MUST** include the following packages:

| Protocol Domain | Required Target Binaries | Yocto Recipe / Package | Purpose |
| :--- | :--- | :--- | :--- |
| **Network & MAC** | `iperf3`, `ethtool` | `iperf3`, `ethtool` | Throughput validation and MAC physics. |
| **CAN Bus** | `cansend`, `candump`, `ip` | `can-utils`, `iproute2` | Automotive bus loopback testing. |
| **Silicon Storage** | `i2ctransfer`, `i2cdetect`, `flash_erase` | `i2c-tools`, `mtd-utils` | EEPROM & SPI NOR destructive validation. |
| **GPIO & Relays** | `gpiomon`, `gpioset`, `gpioget` | `libgpiod-tools` | Edge detection and optocoupler testing. |
| **Cryptography** | `openssl`, `sha256sum`, `base64` | `openssl`, `busybox` | Secure TEZI provisioning and X.509 injection. |
| **Bare-Metal MMIO** | `devmem` | `busybox` (CONFIG_DEVMEM=y) | Direct IOMUXC / Pad register verification. |

>  **Security Mandate (`devmem`):** The `devmem` utility provides direct physical memory access and is required for EOL silicon verification. **It MUST NOT be shipped in production user firmware.** Firmware teams must provide a `tezi-factory-image` for the test jig, which is later securely overwritten with the locked `tezi-production-image` before shipping.

---

## Installation & Environment Setup

To protect the Host PC's operating system from dependency conflicts, `pytest-mes-core` **MUST** be installed inside an isolated virtual environment.

We highly recommend using [Astral's `uv`](https://www.google.com/search?q=%5Bhttps://github.com/astral-sh/uv%5D\(https://github.com/astral-sh/uv\)) for blazingly fast, deterministic package resolution on the factory floor.

### Method A: The Modern Standard (`uv` - Recommended)

If `uv` is installed on the Host PC, use it to instantly build the environment and link the framework.

```bash
# 1. Clone the repository
git clone https://github.com/your-org/pytest-mes-core.git
cd pytest-mes-core

# 2. Create a lightning-fast virtual environment
uv venv

# 3. Activate the environment
source .venv/bin/activate

# 4. Install the framework in editable mode
uv pip install -e .
```

### Method B: The Legacy Standard (`venv` - Fallback)

If the Host PC is strictly air-gapped and lacks modern toolchains, use the standard Python library.

```bash
# 1. Clone the repository
git clone https://github.com/your-org/pytest-mes-core.git
cd pytest-mes-core

# 2. Create the standard virtual environment
python3 -m venv .venv

# 3. Activate the environment
source .venv/bin/activate

# 4. Install the framework in editable mode
pip install -e .
```

### Verifying the Installation

Once installed, verify that the Pytest runner has successfully intercepted the MES Core hooks. Run the following command anywhere on the Host PC (while the virtual environment is active):

```bash
pytest --help | grep "mes_core"
```

*Expected Output:* You should see the custom MES arguments (`--operator-id`, `--env-config`, `--generate-mes-config`) successfully injected into the Pytest CLI.

---

## Usage & Operations

### Step 1: Generate the Station Configuration
On a new factory PC, generate the boilerplate physical configuration:
```bash
pytest --generate-mes-config
```
This drops a `station_env.toml` file in your directory. Edit this file to match the physical reality of the Jig (IP addresses, GPIO pins, expected network speeds).

### Step 2: Execute the Factory Run
Operators must provide their Badge ID to execute a run. The framework reads the TOML, arms the physical E-Stop, powers the board, and begins execution.

```bash
pytest test_evse_functional.py --operator-id=OP-4092 --env-config=station_env.toml
```

### Step 3: Analyze Telemetry & Forensics
Upon completion (or violent crash), all outputs are safely stored in the `artifacts/` directory:

* `artifacts/telemetry/halt_batch_YYYYMMDD_HHMMSS.jsonl` -> Atomic, line-delimited JSON ready for Grafana.
* `artifacts/forensics/fail_test_ethernet_171542.log` -> Automated hardware dumps of failing subsystems.
* `artifacts/forensics/config_snapshot_XXXX.json` -> Cryptographic proof of the TOML settings used for that specific batch run.

---

##  The E-Stop Safety Guarantee
This framework bypasses the standard Python Event Loop for safety-critical operations. If the physical E-Stop GPIO pin is pressed, a background daemon instantly sends a `SIGINT` to the Pytest runner, forcing the `SafePowerController` to immediately de-energize the connected DC Power Supplies before the interpreter dies.

***

### Ready for the Proprietary Suite
With this `README.md` dropped into the root of `pytest-mes-core`, your core engine is fully documented and sealed.

You can now completely switch repositories to your `evse-eol-suite`. Shall we initialize that project and write the `conftest.py` that imports all these beautiful fixtures?
