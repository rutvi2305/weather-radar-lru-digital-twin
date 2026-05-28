import random
import time
import copy
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional, List


class FaultType(Enum):
    BIT_FLIP      = auto()
    WORD_DROPOUT  = auto()
    LATENCY       = auto()
    PHANTOM_ECHO  = auto()
    ANTENNA_STUCK = auto()
    MODE_MISMATCH = auto()


@dataclass
class FaultEvent:
    fault_type:  FaultType
    timestamp:   float = field(default_factory=time.time)
    description: str = ""


class FaultInjector:

    PHANTOM_PRESETS = [
        {"id": 901, "azimuth_deg": 55.0,  "range_nm": 110.0, "dbz": 35.0,
         "cell_name": "PHANTOM-NE",  "phantom": True, "added_by": "FAULT_INJECTOR"},
        {"id": 902, "azimuth_deg": 200.0, "range_nm":  80.0, "dbz": 42.0,
         "cell_name": "PHANTOM-SSW", "phantom": True, "added_by": "FAULT_INJECTOR"},
    ]

    MODE_MISMATCH_MAP = {
        "WX":   "GMAP",
        "GMAP": "WX",
        "TURB": "STBY",
        "STBY": "WX",
    }

    def __init__(self):
        self._active: set = set()
        self._event_log: list = []
        self._stuck_tilt: float = 5.0
        self._dropout_rate: float = 0.40
        self._latency_ms:   float = 2100.0
        self._bit_flip_prob: float = 0.30

    def activate(self, fault: FaultType):
        self._active.add(fault)
        self._log(fault, f"{fault.name} activated")

    def deactivate(self, fault: FaultType):
        self._active.discard(fault)
        self._log(fault, f"{fault.name} deactivated")

    def is_active(self, fault: FaultType) -> bool:
        return fault in self._active

    @property
    def active_faults(self) -> list:
        return list(self._active)

    @property
    def has_any_fault(self) -> bool:
        return len(self._active) > 0

    def apply_to_word(self, word):
        if FaultType.WORD_DROPOUT in self._active:
            if random.random() < self._dropout_rate:
                return None
        if FaultType.BIT_FLIP in self._active:
            if random.random() < self._bit_flip_prob:
                word = self._flip_bits(word)
        return word

    def _flip_bits(self, word):
        w = copy.copy(word)
        n = random.randint(1, 3)
        for _ in range(n):
            bit = random.randint(10, 28)
            w.raw = (w.raw ^ (1 << bit)) & 0xFFFFFFFF
        w.data = (w.raw >> 10) & 0x7FFFF
        w.corrupt = True
        return w

    def get_reported_mode_str(self, actual_mode: str) -> str:
        if FaultType.MODE_MISMATCH in self._active:
            return self.MODE_MISMATCH_MAP.get(actual_mode, actual_mode)
        return actual_mode

    def get_phantom_cells(self) -> list:
        if FaultType.PHANTOM_ECHO in self._active:
            return list(self.PHANTOM_PRESETS)
        return []

    def get_bus_latency_ms(self) -> float:
        if FaultType.LATENCY in self._active:
            jitter = self._latency_ms * 0.2
            return self._latency_ms + random.uniform(-jitter, jitter)
        return 0.0

    @property
    def stuck_tilt(self) -> float:
        return self._stuck_tilt

    def _log(self, fault: FaultType, desc: str):
        self._event_log.append(FaultEvent(fault, time.time(), desc))
        if len(self._event_log) > 500:
            self._event_log.pop(0)

    def get_log(self, n: int = 50) -> list:
        return [
            {"fault": e.fault_type.name, "desc": e.description,
             "timestamp": e.timestamp}
            for e in self._event_log[-n:]
        ]

    def summary(self) -> str:
        if not self._active:
            return "No active faults"
        return "FAULTS: " + ", ".join(f.name for f in self._active)