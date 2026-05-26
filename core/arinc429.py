"""
arinc429.py
-----------
Core ARINC 429 encoding, decoding, and bus simulation.

ARINC 429 Word Structure (32 bits):
  Bits 1-8   : Label (octal, transmitted LSB first)
  Bits 9-10  : SDI  (Source/Destination Identifier)
  Bits 11-29 : Data (BNR or BCD or Discrete)
  Bits 30-31 : SSM  (Sign/Status Matrix)
  Bit  32    : Parity (odd parity)
"""

import struct
import time
import queue
import threading
from enum import IntEnum
from dataclasses import dataclass
from typing import Optional


# ── SSM Codes ────────────────────────────────────────────────────────────────

class SSM(IntEnum):
    FAILURE_WARNING  = 0b00   # Fault / NCD
    NO_COMPUTED_DATA = 0b01   # Test / NCD
    FUNCTIONAL_TEST  = 0b10   # Caution
    NORMAL_OPERATION = 0b11   # Normal


# ── ARINC 429 Labels used by this LRU (octal) ────────────────────────────────

class Label(IntEnum):
    WEATHER_CELL   = 0o270   # 184 decimal  – weather cell data
    PHANTOM_CELL   = 0o271   # 185 decimal  – injected false echo
    RANGE_SETTING  = 0o272   # 186 decimal  – selected range (nm)
    STATUS_WORD    = 0o273   # 187 decimal  – radar mode / status
    TILT_ANGLE     = 0o274   # 188 decimal  – antenna tilt (degrees)
    GAIN_SETTING   = 0o275   # 189 decimal  – gain (0-100 %)
    FAULT_WORD     = 0o377   # 255 decimal  – fault / maintenance word


# ── Single ARINC 429 Word ─────────────────────────────────────────────────────

@dataclass
class ARINC429Word:
    label:  int          # 8-bit label (octal value stored as int)
    sdi:    int          # 2-bit SDI
    data:   int          # 19-bit raw data field
    ssm:    SSM          # 2-bit SSM
    parity: int          # 1-bit odd parity
    raw:    int = 0      # full 32-bit word
    timestamp: float = 0.0
    corrupt: bool = False

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()

    @property
    def hex(self) -> str:
        return f"0x{self.raw:08X}"

    @property
    def binary(self) -> str:
        return f"{self.raw:032b}"

    def describe(self) -> str:
        return (
            f"[{self.hex}]  "
            f"LBL={self.label:03o}  "
            f"SDI={self.sdi:02b}  "
            f"DATA={self.data:019b}  "
            f"SSM={self.ssm.name}  "
            f"P={self.parity}"
        )


# ── Encoder ───────────────────────────────────────────────────────────────────

class ARINC429Encoder:
    """Encodes engineering values into 32-bit ARINC 429 words."""

    @staticmethod
    def _odd_parity(word_31bit: int) -> int:
        """Return the odd-parity bit for bits[0:31]."""
        return 1 - (bin(word_31bit).count('1') % 2)

    @classmethod
    def encode_bnr(
        cls,
        label: int,
        sdi: int,
        value: float,
        resolution: float,
        ssm: SSM = SSM.NORMAL_OPERATION,
        sign_magnitude: bool = True,
    ) -> ARINC429Word:
        """
        Encode a floating-point value as a BNR (Binary Number Representation) word.

        Parameters
        ----------
        label      : 8-bit octal label
        sdi        : 2-bit source/destination identifier (0-3)
        value      : engineering value to encode
        resolution : LSB resolution (e.g. 0.1 for tenths of a degree)
        ssm        : sign/status matrix
        """
        # Clamp data to 19 bits signed (-2^18 .. +2^18-1)
        max_val = (2**18 - 1) * resolution
        value = max(-max_val, min(max_val, value))

        raw_int = int(round(value / resolution))

        if sign_magnitude:
            sign_bit = 1 if raw_int < 0 else 0
            magnitude = abs(raw_int) & 0x3FFFF   # 18 bits magnitude
            data_field = (sign_bit << 18) | magnitude
        else:
            # Two's complement in 19 bits
            if raw_int < 0:
                raw_int = (1 << 19) + raw_int
            data_field = raw_int & 0x7FFFF

        return cls._build_word(label, sdi, data_field, ssm)

    @classmethod
    def encode_discrete(
        cls,
        label: int,
        sdi: int,
        bits: dict,   # {bit_position (11-29): value (0 or 1)}
        ssm: SSM = SSM.NORMAL_OPERATION,
    ) -> ARINC429Word:
        """Encode a discrete word (individual bit flags)."""
        data_field = 0
        for pos, val in bits.items():
            # bit positions 11-29 → data field bit 0-18
            field_bit = pos - 11
            if 0 <= field_bit <= 18:
                data_field |= (int(bool(val)) << field_bit)
        return cls._build_word(label, sdi, data_field, ssm)

    @classmethod
    def _build_word(cls, label: int, sdi: int, data_field: int, ssm: SSM) -> ARINC429Word:
        """Assemble and return a complete ARINC429Word."""
        # ARINC 429 transmits label bits reversed (bit 8 first → LSB first)
        label_reversed = int(f"{label:08b}"[::-1], 2)

        word_31 = (
            (label_reversed & 0xFF)
            | ((sdi & 0x3) << 8)
            | ((data_field & 0x7FFFF) << 10)
            | ((int(ssm) & 0x3) << 29)
        )
        parity = cls._odd_parity(word_31)
        raw = word_31 | (parity << 31)

        return ARINC429Word(
            label=label,
            sdi=sdi & 0x3,
            data=data_field & 0x7FFFF,
            ssm=ssm,
            parity=parity,
            raw=raw & 0xFFFFFFFF,
        )


# ── Decoder ───────────────────────────────────────────────────────────────────

class ARINC429Decoder:
    """Decodes 32-bit ARINC 429 words back to engineering values."""

    @staticmethod
    def decode_word(raw: int) -> ARINC429Word:
        """Parse a raw 32-bit integer into an ARINC429Word."""
        parity      = (raw >> 31) & 0x1
        ssm_bits    = (raw >> 29) & 0x3
        data_field  = (raw >> 10) & 0x7FFFF
        sdi         = (raw >>  8) & 0x3
        label_rev   = raw & 0xFF

        # Reverse label bits back to octal
        label = int(f"{label_rev:08b}"[::-1], 2)

        # Validate parity
        word_31 = raw & 0x7FFFFFFF
        expected_parity = 1 - (bin(word_31).count('1') % 2)
        corrupt = (parity != expected_parity)

        try:
            ssm = SSM(ssm_bits)
        except ValueError:
            ssm = SSM.FAILURE_WARNING

        return ARINC429Word(
            label=label,
            sdi=sdi,
            data=data_field,
            ssm=ssm,
            parity=parity,
            raw=raw,
            corrupt=corrupt,
        )

    @staticmethod
    def extract_bnr(word: ARINC429Word, resolution: float) -> float:
        """Extract a signed BNR value from the data field."""
        sign_bit = (word.data >> 18) & 0x1
        magnitude = word.data & 0x3FFFF
        value = magnitude * resolution
        return -value if sign_bit else value

    @staticmethod
    def extract_discrete_bit(word: ARINC429Word, bit_position: int) -> int:
        """Extract a single discrete bit (ARINC bit numbering 11-29)."""
        field_bit = bit_position - 11
        return (word.data >> field_bit) & 0x1


# ── Simulated ARINC 429 Bus ───────────────────────────────────────────────────

class ARINC429Bus:
    """
    Thread-safe simulated ARINC 429 serial bus.
    Supports high-speed (100 kbps) and low-speed (12.5 kbps) modes.
    """

    HIGH_SPEED_BPS = 100_000
    LOW_SPEED_BPS  =  12_500

    def __init__(self, speed: str = "high", latency_ms: float = 0.0):
        self.speed = speed
        self.latency_ms = latency_ms       # injected latency
        self._tx_queue: queue.Queue = queue.Queue()
        self._rx_queue: queue.Queue = queue.Queue()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._word_log: list = []
        self._lock = threading.Lock()

    # ── Bit timing ───────────────────────────────────────────────────────────

    @property
    def word_period_s(self) -> float:
        bps = self.HIGH_SPEED_BPS if self.speed == "high" else self.LOW_SPEED_BPS
        return 32 / bps   # 32 bits per word

    # ── Start / Stop ─────────────────────────────────────────────────────────

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._bus_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    # ── Transmit / Receive ───────────────────────────────────────────────────

    def transmit(self, word: ARINC429Word):
        self._tx_queue.put(word)

    def receive(self, timeout: float = 0.1) -> Optional[ARINC429Word]:
        try:
            return self._rx_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def receive_all(self) -> list:
        words = []
        while not self._rx_queue.empty():
            try:
                words.append(self._rx_queue.get_nowait())
            except queue.Empty:
                break
        return words

    # ── Internal bus loop ────────────────────────────────────────────────────

    def _bus_loop(self):
        while self._running:
            try:
                word = self._tx_queue.get(timeout=0.05)
            except queue.Empty:
                continue

            # Simulate transmission time
            time.sleep(self.word_period_s)

            # Simulate latency injection
            if self.latency_ms > 0:
                time.sleep(self.latency_ms / 1000.0)

            # Log word
            with self._lock:
                self._word_log.append(word)
                if len(self._word_log) > 500:
                    self._word_log.pop(0)

            self._rx_queue.put(word)

    # ── Log access ───────────────────────────────────────────────────────────

    def get_log(self, last_n: int = 50) -> list:
        with self._lock:
            return list(self._word_log[-last_n:])

    def clear_log(self):
        with self._lock:
            self._word_log.clear()
