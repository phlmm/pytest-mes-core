# MES Core Transport Layer (`pytest_mes_core.transports`)

This module manages all physical Layer 1 / Layer 3 communications with the Device Under Test (DUT).

The core philosophy of this domain is **Dependency Inversion**. High-level testing protocols (like Ethernet throughput, eFuse burning, or CAN bus validation) must **never** know whether they are communicating over a Gigabit Ethernet SSH socket, a 115200-baud UART cable, or an Android Debug Bridge (ADB).

By isolating the physical layer here, we guarantee that our test logic remains entirely decoupled from the silicon's physical connectivity state.

---

## Core Architecture (`base.py`)

Every class in this module revolves around the structural contracts defined in `base.py`.

### 1. The `DutTransport` Protocol
All transports must implement the `DutTransport` protocol. This contract enforces a strict state machine and synchronous command execution:

* `is_connected` (Property): Dynamically reports if the underlying socket/file descriptor is alive.
* `connect()`: Initializes the hardware and achieves prompt/shell synchronization.
* `disconnect()`: Safely tears down the interface (Zero-Leakage).
* `safe_run(cmd, timeout_s, **kwargs)`: Executes a payload and returns the immutable `CommandResult`.

> ### Architectural Design: Structural Subtyping
> You will notice that our clients (`EphemeralSSHClient`, `EphemeralSerialClient`) **do not explicitly inherit** from `DutTransport` (e.g., you won't see `class EphemeralSSHClient(DutTransport):`).
>
> Because `DutTransport` is defined as a Python `typing.Protocol`, we utilize **Structural Subtyping** (static Duck Typing). As long as a class provides the exact methods and properties defined in the Protocol, Python's type checker automatically recognizes it as a valid `DutTransport`.
>
> **Why we do this:** Total decoupling. If we find an open-source library on GitHub for flashing JTAG (e.g., `jtag-flasher`), and its client object happens to have `connect()`, `disconnect()`, and `safe_run()` methods, we can inject that 3rd-party object directly into our `FailoverTransport` matrix. It will work flawlessly without us needing to force the 3rd-party library to inherit from our internal base classes.

### 2. The `CommandResult` Dataclass
Transports do not return raw strings or 3rd-party objects (like Fabric's `Result`). They return a `@dataclass(frozen=True)` called `CommandResult`.
* **Immutability:** `frozen=True` prevents downstream protocols from accidentally mutating the test data.
* **Built-in Telemetry:** The transport layer uses `time.perf_counter()` to populate `duration_s`. Downstream protocols do not need to time their own execution; the physical layer records exactly how long the bytes took to process.

### 3. Domain Exceptions
We do not bleed third-party exceptions (e.g., `paramiko.SSHException`, `serial.SerialException`) into the Pytest suite. Transports map all hardware faults to strict Domain Exceptions:
* `TransportConnectionError`: The physical pipe shattered (e.g., cable unplugged, kernel panic, EMI drop).
* `TransportTimeoutError`: The pipe is fine, but the DUT application hung and failed to return a prompt within the execution window.

---

## Available Transports

### `EphemeralSSHClient` (`ssh.py`)
The high-speed, in-band transport. Used for transferring large payloads (eMMC testing) or high-bandwidth daemon polling. It strips away strict host-key checking to accommodate ephemeral factory IP assignments.

### `EphemeralSerialClient` (`serial.py`)
The unkillable, out-of-band transport. Interacts directly with `/dev/ttyUSB` to navigate U-Boot and Linux shell prompts without an active IP stack. Uses C-optimized `read_until` buffers to prevent Host PC CPU starvation when parsing massive kernel logs.

### `FailoverTransport` (`failover.py`)
The reactive failover matrix. This class wraps both the SSH and Serial clients.
* It masquerades as a standard `DutTransport` to downstream protocols.
* If the primary transport (SSH) throws a `TransportConnectionError` mid-test, the matrix catches it, logs a critical warning, and seamlessly routes the command to the fallback transport (Serial) without crashing the Pytest suite.

---

## Utilities

### `HostSideBuffer` (`chunking.py`)
An asynchronous data vacuum that continuously tails remote log files (e.g., `/var/log/syslog`).
* **Thread-Safe:** Uses `threading.Lock` to guarantee data integrity between the background daemon and the Pytest evaluation thread.
* **Transport Agnostic:** Accepts the `DutTransport` interface. It can vacuum logs over SSH, or seamlessly fallback to Serial if wrapped in the `FailoverTransport`.

---

## Adding a New Transport

If future hardware requires a new communication protocol (e.g., JTAG, ADB, or a custom REST API), you do not need to modify the `protocols/` domain.

1.  Create a new file in this directory (e.g., `adb.py`).
2.  Create a class that implements the `DutTransport` protocol lifecycle methods (`connect`, `disconnect`, `is_connected`).
3.  Implement `safe_run()`.
4.  Ensure `safe_run()` catches ADB-specific exceptions and translates them to `TransportConnectionError` or `TransportTimeoutError`.
5.  Return the standard `CommandResult` dataclass.
6.  Inject it into the Pytest fixtures in `conftest.py`—the rest of the framework will accept it automatically.
