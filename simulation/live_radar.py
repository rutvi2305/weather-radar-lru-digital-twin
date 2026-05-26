"""
live_radar.py
-------------
Live radar engine — ZERO hardcoded weather data.

All weather cells come from the database (added by user/mentor via the UI).
Aircraft params and pilot controls come from the database too.
The engine runs a continuous sweep and encodes everything into ARINC 429 words.
"""

import time
import math
import threading
import queue
from typing import List, Optional

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.arinc429         import ARINC429Bus, ARINC429Encoder, ARINC429Decoder, Label, SSM
from faults.fault_injector import FaultInjector, FaultType


# ── Label names for display ───────────────────────────────────────────────────
LABEL_NAMES = {
    0o270: "WEATHER_CELL",
    0o271: "PHANTOM_CELL",
    0o272: "RANGE",
    0o273: "STATUS",
    0o274: "TILT",
    0o275: "GAIN",
    0o377: "FAULT",
}

# ── Intensity levels ──────────────────────────────────────────────────────────
def dbz_to_level(dbz: float) -> str:
    if dbz < 20:  return "NONE"
    if dbz < 30:  return "LIGHT"
    if dbz < 40:  return "MODERATE"
    if dbz < 50:  return "HEAVY"
    if dbz < 55:  return "INTENSE"
    if dbz < 70:  return "EXTREME"
    return "HAIL"

def dbz_to_color(dbz: float) -> str:
    if dbz < 20:  return "#00b4d8"
    if dbz < 30:  return "#00e050"
    if dbz < 40:  return "#e6dc14"
    if dbz < 50:  return "#f08214"
    if dbz < 55:  return "#dc2828"
    if dbz < 70:  return "#dc28dc"
    return "#ffffff"


class LiveRadarEngine:
    """
    The live simulation engine.
    - Reads cells/params from the database (updated externally by UI)
    - Runs ARINC 429 encoding at 10 Hz
    - Pushes encoded words into a queue for the server to pick up
    - No hardcoded data — everything is live
    """

    TICK_HZ   = 10
    SWEEP_RPM = 3.0   # antenna rotations per minute

    def __init__(self, bus: ARINC429Bus, fault_injector: FaultInjector):
        self.bus      = bus
        self.fi       = fault_injector
        self._enc     = ARINC429Encoder()
        self._dec     = ARINC429Decoder()

        # Live data (updated externally via update_* methods)
        self._cells:   list = []          # list of dicts from DB
        self._aircraft: dict = {
            "heading_deg": 45.0, "pitch_deg": 0.0, "roll_deg": 0.0,
            "altitude_ft": 35000.0, "airspeed_kt": 480.0
        }
        self._controls: dict = {
            "mode": "WX", "tilt_deg": 0.0, "gain": 65,
            "range_nm": 160, "auto_tilt": 1
        }

        # State
        self._sweep_angle = 0.0
        self._words_sent  = 0
        self._words_dropped = 0
        self._corrupt_count = 0
        self._lock = threading.RLock()

        # Queue: encoded words waiting to be picked up by server
        self._word_queue: queue.Queue = queue.Queue(maxsize=2000)

        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ── External updates (called by server when DB changes) ───────────────────

    def update_cells(self, cells: list):
        with self._lock:
            self._cells = list(cells)

    def update_aircraft(self, params: dict):
        with self._lock:
            self._aircraft.update({
                k: v for k, v in params.items()
                if k in ("heading_deg", "pitch_deg", "roll_deg",
                         "altitude_ft", "airspeed_kt")
            })

    def update_controls(self, controls: dict):
        with self._lock:
            self._controls.update({
                k: v for k, v in controls.items()
                if k in ("mode", "tilt_deg", "gain", "range_nm", "auto_tilt")
            })

    # ── Start / Stop ──────────────────────────────────────────────────────────

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print("[ENGINE] Live radar engine started")

    def stop(self):
        self._running = False

    # ── Main tick loop ────────────────────────────────────────────────────────

    def _loop(self):
        period = 1.0 / self.TICK_HZ
        while self._running:
            t0 = time.time()
            try:
                self._tick()
            except Exception as e:
                print(f"[ENGINE] Tick error: {e}")
            elapsed = time.time() - t0
            time.sleep(max(0, period - elapsed))

    def _tick(self):
        with self._lock:
            cells    = list(self._cells)
            aircraft = dict(self._aircraft)
            controls = dict(self._controls)

        # Advance sweep
        deg_per_tick = self.SWEEP_RPM * 6.0 / self.TICK_HZ
        self._sweep_angle = (self._sweep_angle + deg_per_tick) % 360

        # Apply antenna-stuck fault
        effective_tilt = self._get_effective_tilt(controls, aircraft)

        # Apply phantom echoes to cell list
        all_cells = list(cells)
        if self.fi.is_active(FaultType.PHANTOM_ECHO):
            for pc in self.fi.get_phantom_cells():
                all_cells.append(pc)

        # Encode all ARINC words
        encoded = self._encode_frame(all_cells, controls, effective_tilt)

        # Apply word-level faults and transmit
        for word_dict in encoded:
            raw_word = self._rebuild_raw(word_dict)
            # Build ARINC word object for fault injection
            from core.arinc429 import ARINC429Word
            w = self._dec.decode_word(raw_word)
            w.raw = raw_word

            result = self.fi.apply_to_word(w)
            if result is None:
                self._words_dropped += 1
                continue

            if result.corrupt:
                self._corrupt_count += 1

            # Push to queue for server to emit via WebSocket
            word_payload = self._word_to_dict(result, word_dict['label_name'])
            try:
                self._word_queue.put_nowait(word_payload)
            except queue.Full:
                pass   # drop oldest if browser not reading fast enough

            self._words_sent += 1

    # ── Encoding ──────────────────────────────────────────────────────────────

    def _encode_frame(self, cells: list, controls: dict, tilt: float) -> list:
        """Encode one full frame of ARINC 429 words. Returns list of dicts."""
        words = []
        gain_factor = controls.get('gain', 65) / 65.0
        range_nm    = controls.get('range_nm', 160)

        # ── Cell words ───────────────────────────────────────────────────────
        for cell in cells:
            if cell.get('range_nm', 0) > range_nm:
                continue

            dbz = min(70.0, float(cell.get('dbz', 0)) * gain_factor)
            az  = float(cell.get('azimuth_deg', 0))
            rng = float(cell.get('range_nm', 0))
            phantom = bool(cell.get('phantom', False))

            # Pack az + range + dbz into 19 bits
            az_enc    = int((az  / 360.0) * 127) & 0x7F
            rng_enc   = int((rng / 320.0) *  63) & 0x3F
            dbz_enc   = int((dbz /  70.0) *  63) & 0x3F
            data_19   = (az_enc << 12) | (rng_enc << 6) | dbz_enc

            label = 0o271 if phantom else 0o270
            word  = self._enc._build_word(label, 0, data_19, SSM.NORMAL_OPERATION)
            words.append({
                'label':      label,
                'label_name': 'PHANTOM_CELL' if phantom else 'WEATHER_CELL',
                'raw':        word.raw,
                'data':       data_19,
                'sdi':        0,
                'ssm':        'NORMAL_OPERATION',
                'corrupt':    False,
                # decoded for display
                'decoded': {
                    'azimuth_deg': round(az, 1),
                    'range_nm':    round(rng, 1),
                    'dbz':         round(dbz, 1),
                    'intensity':   dbz_to_level(dbz),
                    'color':       dbz_to_color(dbz),
                    'cell_name':   cell.get('cell_name', 'Cell'),
                    'phantom':     phantom,
                }
            })

        # ── Status word (label 273) ──────────────────────────────────────────
        actual_mode   = controls.get('mode', 'WX')
        reported_mode = self.fi.get_reported_mode_str(actual_mode)
        mode_code     = {'WX':0, 'GMAP':1, 'TURB':2, 'STBY':3, 'TEST':4}.get(reported_mode, 0)
        mismatch      = reported_mode != actual_mode
        status_ssm    = SSM.FUNCTIONAL_TEST if mismatch else SSM.NORMAL_OPERATION
        sw = self._enc.encode_bnr(Label.STATUS_WORD, 0, float(mode_code), 1.0, status_ssm)
        words.append({
            'label': Label.STATUS_WORD, 'label_name': 'STATUS',
            'raw': sw.raw, 'data': sw.data, 'sdi': 0,
            'ssm': status_ssm.name, 'corrupt': False,
            'decoded': {
                'reported_mode': reported_mode,
                'actual_mode':   actual_mode,
                'mismatch':      mismatch,
            }
        })

        # ── Tilt word (label 274) ────────────────────────────────────────────
        stuck = self.fi.is_active(FaultType.ANTENNA_STUCK)
        tilt_ssm = SSM.FUNCTIONAL_TEST if stuck else SSM.NORMAL_OPERATION
        tw = self._enc.encode_bnr(Label.TILT_ANGLE, 0, tilt, 0.1, tilt_ssm)
        words.append({
            'label': Label.TILT_ANGLE, 'label_name': 'TILT',
            'raw': tw.raw, 'data': tw.data, 'sdi': 0,
            'ssm': tilt_ssm.name, 'corrupt': False,
            'decoded': {'tilt_deg': round(tilt, 2), 'stuck': stuck}
        })

        # ── Gain word (label 275) ────────────────────────────────────────────
        gw = self._enc.encode_bnr(Label.GAIN_SETTING, 0, float(controls.get('gain', 65)), 1.0)
        words.append({
            'label': Label.GAIN_SETTING, 'label_name': 'GAIN',
            'raw': gw.raw, 'data': gw.data, 'sdi': 0,
            'ssm': 'NORMAL_OPERATION', 'corrupt': False,
            'decoded': {'gain': controls.get('gain', 65)}
        })

        # ── Fault word (label 377) ───────────────────────────────────────────
        if self.fi.has_any_fault:
            fault_bits = {}
            fault_bit_map = {
                FaultType.BIT_FLIP: 0, FaultType.WORD_DROPOUT: 1,
                FaultType.LATENCY: 2,  FaultType.PHANTOM_ECHO: 3,
                FaultType.ANTENNA_STUCK: 4, FaultType.MODE_MISMATCH: 5,
            }
            for ft, bit in fault_bit_map.items():
                fault_bits[11 + bit] = 1 if self.fi.is_active(ft) else 0
            fw = self._enc.encode_discrete(Label.FAULT_WORD, 0, fault_bits, SSM.FAILURE_WARNING)
            words.append({
                'label': Label.FAULT_WORD, 'label_name': 'FAULT',
                'raw': fw.raw, 'data': fw.data, 'sdi': 0,
                'ssm': 'FAILURE_WARNING', 'corrupt': False,
                'decoded': {'active_faults': [f.name for f in self.fi.active_faults]}
            })

        return words

    def _get_effective_tilt(self, controls: dict, aircraft: dict) -> float:
        if self.fi.is_active(FaultType.ANTENNA_STUCK):
            return 5.0   # stuck
        tilt = float(controls.get('tilt_deg', 0.0))
        if controls.get('auto_tilt', 1):
            pitch = float(aircraft.get('pitch_deg', 0.0))
            tilt -= pitch * 0.85   # stabilization
        return round(tilt, 2)

    def _rebuild_raw(self, word_dict: dict) -> int:
        return word_dict['raw']

    def _word_to_dict(self, word, label_name: str) -> dict:
        return {
            'label':      word.label,
            'label_octal': f"{word.label:03o}",
            'label_name': label_name,
            'hex':        f"0x{word.raw:08X}",
            'binary':     f"{word.raw:032b}",
            'sdi':        word.sdi,
            'data':       word.data,
            'ssm':        word.ssm.name,
            'parity':     word.parity,
            'corrupt':    word.corrupt,
            'timestamp':  time.time(),
        }

    # ── Snapshot (for WebSocket push) ─────────────────────────────────────────

    def get_snapshot(self) -> dict:
        with self._lock:
            cells    = list(self._cells)
            aircraft = dict(self._aircraft)
            controls = dict(self._controls)

        gain_factor = controls.get('gain', 65) / 65.0
        range_nm    = controls.get('range_nm', 160)

        # Build decoded cell picture for the display
        decoded_cells = []
        for cell in cells:
            if cell.get('range_nm', 0) > range_nm:
                continue
            dbz = min(70.0, float(cell.get('dbz', 0)) * gain_factor)
            decoded_cells.append({
                'id':          cell.get('id'),
                'cell_name':   cell.get('cell_name', 'Cell'),
                'azimuth_deg': float(cell.get('azimuth_deg', 0)),
                'range_nm':    float(cell.get('range_nm', 0)),
                'dbz':         round(dbz, 1),
                'intensity':   dbz_to_level(dbz),
                'color':       dbz_to_color(dbz),
                'phantom':     False,
                'added_by':    cell.get('added_by', 'operator'),
            })
        if self.fi.is_active(FaultType.PHANTOM_ECHO):
            for pc in self.fi.get_phantom_cells():
                dbz = float(pc.get('dbz', 35))
                decoded_cells.append({
                    'id': pc.get('id', 99),
                    'cell_name': 'PHANTOM',
                    'azimuth_deg': float(pc.get('azimuth_deg', 0)),
                    'range_nm':    float(pc.get('range_nm', 0)),
                    'dbz':         dbz,
                    'intensity':   dbz_to_level(dbz),
                    'color':       '#a855f7',
                    'phantom':     True,
                    'added_by':    'FAULT_INJECTOR',
                })

        effective_tilt = self._get_effective_tilt(controls, aircraft)
        actual_mode    = controls.get('mode', 'WX')
        reported_mode  = self.fi.get_reported_mode_str(actual_mode)

        return {
            'sweep_angle':    round(self._sweep_angle, 1),
            'words_sent':     self._words_sent,
            'words_dropped':  self._words_dropped,
            'corrupt_count':  self._corrupt_count,
            'dropout_pct':    round(100 * self._words_dropped /
                                    max(1, self._words_sent + self._words_dropped), 1),
            'cells':          decoded_cells,
            'cell_count':     len(decoded_cells),
            'aircraft':       aircraft,
            'controls':       controls,
            'effective_tilt': effective_tilt,
            'actual_mode':    actual_mode,
            'reported_mode':  reported_mode,
            'mode_mismatch':  actual_mode != reported_mode,
            'timestamp':      time.time(),
        }

    # ── Word queue ────────────────────────────────────────────────────────────

    def drain_word_queue(self) -> list:
        words = []
        while not self._word_queue.empty():
            try:
                words.append(self._word_queue.get_nowait())
            except queue.Empty:
                break
        return words
