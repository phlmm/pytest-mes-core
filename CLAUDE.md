# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`pytest-mes-core` is a pytest plugin implementing an MES (Manufacturing Execution System) framework for factory-floor hardware testing: it powers test jigs that flash, boot, probe, and validate embedded Linux boards and bare-metal MCUs. Python >= 3.11, `src/` layout, hatchling build.

## Commands

```bash
# Install for development (Linux; on Windows/macOS use .[dev,docs,instruments] to skip evdev/gpiod)
pip install -e ".[all]"

# Run the test suite
pytest

# Run a single test file / test
pytest tests/unit/test_state_machine.py
pytest tests/unit/test_failover_unit.py -k test_name

# Lint and type check
ruff check src tests        # line-length 120, py311
mypy src                    # strict mode is enforced in pyproject.toml

# Docs
mkdocs serve

# Web GUI: FastAPI backend + Vite/React frontend (two processes)
mes-gui                                  # uvicorn on :8000 (needs pip install -e ".[gui]")
cd gui/frontend && npm run dev           # Vite dev server on :5173
cd gui/frontend && npm run lint          # eslint for the frontend
```

Integration tests in `tests/integration/` self-skip when their prerequisites (e.g. Docker for `test_docker_failover.py`) are missing, so a plain `pytest` run is safe anywhere.

Example factory-floor invocation (how downstream users run the framework, not how this repo is tested):

```bash
pytest projects/some_board/ -p mes_core --operator-id=FILIP-01 --env-config=station_env.toml
```

## Critical pytest quirk: the plugin is disabled for this repo's own tests

The framework registers itself globally via the `pytest11` entry point (`plugin.py`), and `core_config.py` makes `--operator-id` **required**. To keep the repo's own unit tests runnable, `pyproject.toml` sets `addopts = "-p no:mes_core ..."` which disables the plugin. Consequences:

- Tests that exercise plugin behavior (fixtures, CLI options, telemetry hooks) can't do so directly — they use the `pytester` fixture (enabled in `tests/conftest.py`) to spawn subprocess pytest runs with the plugin re-enabled. See `tests/integration/test_plugin.py`.
- If you add a CLI option or fixture to `plugins/`, verify it via a `pytester` test, not a normal unit test.

## Architecture

The framework enforces a strict **Host vs. Target** boundary: the Host PC runs the Python test logic and lab instruments; the Target (DUT) only ever executes lightweight POSIX shell commands sent over a transport.

Layered stack, bottom to top:

1. **`transports/`** — physical links to the DUT. Everything implements the `DutTransport` structural `Protocol` (`transports/base.py`) and returns a frozen `CommandResult` dataclass. Key pieces:
   - `EphemeralSSHClient` and `EphemeralSerialClient` — primary links.
   - `FailoverTransport` (`failover.py`) — wraps primary (SSH) + fallback (serial/UART). On a broken pipe it permanently fails over, a background thread probes for primary recovery, and a watchdog panic callback actively severs SSH to force fast failover.
   - MCU-side transports: `pyocd_client.py`, `probe_rs_client.py` (SWD/JTAG), `mqtt_client.py`, `serial_client.py`.
   - Every transport exposes dual sync/async APIs (`safe_run` / `async_safe_run`); async concurrency is `anyio`-based throughout the codebase.

2. **`protocols/`** — target-side validation logic built on top of a transport (I2C, CAN, SPI, GPIO, eFuse, U-Boot env, swupdate…). `protocols/rpc/` is a typed RPC layer: Pydantic `RpcMessage` schemas compile to binary C-structs (COBS-framed, for MCUs over UART) or JSON (for ELinux daemons).

3. **State machines** — the framework never assumes hardware state; it probes and routes the DUT to the required physical state before tests run.
   - `state_machine.py` — `EmbeddedLinuxStateMachine` (`transitions` AsyncMachine): POWER_OFF → U-Boot interception → OS_USERLAND, kernel-panic detection, boot profiling, graphviz FSM crash visualization when available.
   - `mcu_state_machine.py` — `BareMetalStateMachine` for MCUs (POWER_OFF / ENERGIZED / HALTED / RUNNING / OTA_UPDATE / FAILED_OTA), driving a PSU + SWD probe.

4. **`events.py`** — a `pluggy`-based event bus (`mes_core` hook markers). UART boot monitoring emits typed events (`PromptDetected`, `PanicDetected`, `MilestoneReached`, …) dispatched both to a catch-all hook (`on_uart_event`) and typed hooks. This is how the watchdog, boot profiler, and telemetry decouple from the FSM.

5. **`config/`** — the "Indisputable BOM". `StationEnvironment` (`config/station.py`) parses the station TOML into strict Pydantic models (`extra='forbid'`); every jig peripheral (PSU, e-stop, uarts, i2c, jtag/pyocd/probe-rs targets, MQTT, …) is a validated section. Invalid config aborts before any hardware is powered.

6. **`plugins/`** — the pytest integration layer, loaded via `plugin.py`:
   - `core_config.py` — CLI options (`--operator-id`, `--env-config`, `--mock-hardware`, `--hold-on-fail`, …) and the `mes_env` fixture.
   - `hardware.py` — session-scoped fixtures for PSU, serial, SSH, and the `dut_transport` failover wrapper.
   - `orchestrator.py` — binds the FSM to the pytest lifecycle; implements the `@pytest.mark.requires_state(DutState.X)` and `@pytest.mark.hardware_retry(n)` markers (retry = mark hardware DIRTY, hard power-cycle, rerun).
   - `telemetry_hooks.py` — attaches telemetry records to test items.

7. **`telemetry/`** — composite router (`composite.py`) fanning out to four sinks: JSONL (machine), Markdown (developer), TXT receipt (operator), HTML EOL certificate (customer). `post_mortem.py` harvests forensics (`dmesg`, `coredumpctl`, `i2cdetect`) on failure and attaches them to reports.

8. **`provisioning/`** — bootstrapping blank silicon: `tezi_uuu.py` (NXP `uuu`/Toradex Easy Installer), `mcu_flasher.py`, `jtag.py`, `microchip.py`, PKI/secure-fetch helpers.

9. **`host_adapters/` and `instruments/`** — host-side peripherals: HID barcode scanners, SD-mux, e-stop/safety GPIO, OpenOCD, Renode simulation, SCPI power supplies (plus `virtual_psu.py` and `pyvisa-sim` via `tests/fixtures/psu_sim.yaml` for tests).

## Conventions

- Strict typing everywhere — `mypy --strict` is the configured bar; transports use structural `Protocol`s rather than inheritance.
- Logging is `structlog` with `mes_core.*` logger names.
- Sync and async variants coexist deliberately ("dual-pipeline"): when adding transport/FSM functionality, provide both `foo()` and `async_foo()`; async code uses `anyio`, never raw asyncio.
- Async wrappers must mirror the sync signature exactly (same params, same defaults) and bridge with `functools.partial(...)` into `anyio.to_thread.run_sync`. Never write `async def async_foo(self, *args, **kwargs): return await anyio.to_thread.run_sync(self.foo, *args, **kwargs)` — `run_sync` does not forward kwargs, so that pattern raises `TypeError` on first kwarg use (an entire generation of these was purged in one sweep; don't reintroduce them).
- Commands carrying secrets (keys, passwords) must be sent with `safe_run(..., sensitive=True)` — both serial and SSH transports mask the payload in TX debug logs and in the DUT-side forensic journal.
- `UartEventStream` yields an `IdleTick` event after ~1 s of RX silence; consumer loops match events with `isinstance` chains, so unknown event types must always fall through harmlessly.
- `TestRecord.outcome` (`passed`/`failed`/`skipped`/`unknown`) is the authoritative result for telemetry sinks — `passed` is `False` for skips, so never derive failure counts from `not record.passed`.
- Custom markers must be registered in `pyproject.toml` `[tool.pytest.ini_options] markers` to avoid warnings.
- Hardware-dependent imports (evdev, gpiod, pyocd…) are optional extras — keep them lazy/guarded so the core imports on any OS.
