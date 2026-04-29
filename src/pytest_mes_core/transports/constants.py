"""
Shared compiled regexes for UART stream processing.

Centralising these here ensures a single source of truth across:
- EphemeralSerialClient (RX daemon ANSI stripping)
- UartKernelWatchdog   (panic detection)
- UartEventStream      (panic detection)
- _probe_hardware_state (ANSI strip before prompt comparison)
"""
import re

# ---------------------------------------------------------------------------
# ANSI Control Sequence stripping
# ---------------------------------------------------------------------------
#: Matches ANSI/VT100 escape sequences (colour, cursor movement, erase, etc.).
#: Used on raw bytes from the UART RX daemon before pattern matching.
ANSI_ESCAPE_B: re.Pattern[bytes] = re.compile(rb'\x1b\[[0-9;]*[a-zA-Z]')

# ---------------------------------------------------------------------------
# Kernel / Secure Boot panic patterns
# ---------------------------------------------------------------------------
#: Matches any byte sequence that indicates an unrecoverable hardware fault.
#: Shared between UartKernelWatchdog and UartEventStream so the pattern set
#: is never out of sync.
PANIC_PATTERN_B: re.Pattern[bytes] = re.compile(
    rb'('
    rb'Kernel panic - not syncing'
    rb'|Unable to handle kernel paging request'
    rb'|Oops - undefined instruction'
    rb'|Out of memory: Killed process'
    rb'|BUG: soft lockup - CPU'
    rb'|rcu_preempt detected stalls'
    rb'|task blocked for more than 120 seconds'
    rb'|synchronous external abort'
    rb'|mmc[0-9]+: error'
    rb'|EXT4-fs error'
    rb'|UBIFS error'
    rb'|HAB Events'
    rb'|SEC_ERR'
    rb'|Signature Verification Failed'
    rb')'
)
