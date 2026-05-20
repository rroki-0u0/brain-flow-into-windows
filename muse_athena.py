"""
MuseAthenaBoard: Muse S Athena (MS-03) board adapter.

Drop-in replacement for BrainFlow's BoardShim that talks to the Muse S
directly via bleak + the OpenMuse BLE protocol.  This bypasses BrainFlow's
native BLE stack and lets the device run in its ultra-low-power EEG-only
streaming preset (p50, EEG4 only - no optics / ACC / gyro / battery) by
default for the longest battery life.  Set ``MUSE_PRESET`` (e.g.
``p1041`` for full sensors) to override.

Ported from BrainFlowsIntoVRChat/muse_athena.py.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import bleak
import numpy as np
from brainflow.board_shim import BoardIds, BrainFlowPresets
from brainflow.exit_codes import BrainFlowError, BrainFlowExitCodes

from openmuse.OpenMuse.backends import BleakBackend
from openmuse.OpenMuse.decode import make_timestamps, parse_message
from openmuse.OpenMuse.muse import MuseS

logger = logging.getLogger(__name__)


# ── Board layout constants (mirroring BrainFlow's MUSE_S_BOARD descriptors) ──

# DEFAULT_PRESET: EEG  (num_rows=8, eeg_channels=[1,2,3,4], timestamp=6)
_EEG_NUM_ROWS = 8
_EEG_CHANNELS = [1, 2, 3, 4]
_EEG_TS_CHAN = 6
_EEG_RATE = 256

# ANCILLARY_PRESET: PPG  (num_rows=6, ppg_channels=[1,2,3], timestamp=4)
_PPG_NUM_ROWS = 6
_PPG_CHANNELS = [1, 2, 3]   # index 0=RED, 1=IR, 2=AMB
_PPG_TS_CHAN = 4
_PPG_RATE = 64

# Optics16 channel indices (from OpenMuse decode.py OPTICS_CHANNELS order):
# LO_NIR, RO_NIR, LO_IR, RO_IR, LI_NIR, RI_NIR, LI_IR, RI_IR,
# LO_RED, RO_RED, LO_AMB, RO_AMB, LI_RED, RI_RED, LI_AMB, RI_AMB
_OPTICS_IR_IDX = [2, 3, 6, 7]
_OPTICS_RED_IDX = [8, 9, 12, 13]
_OPTICS_AMB_IDX = [10, 11, 14, 15]


class MuseAthenaBoard:
    """
    Muse S Athena (MS-03) board adapter.

    Mostly BoardShim-compatible: ``prepare_session``, ``start_stream``,
    ``stop_stream``, ``release_session``, ``get_current_board_data``.

    EEG (DEFAULT_PRESET):      256 Hz, 4 channels (TP9, AF7, AF8, TP10)
    PPG (ANCILLARY_PRESET):     64 Hz, 3 channels (RED, IR, AMB)
    """

    # Default preset: EEG4 only - ultra-low-power streaming, longest
    # battery life on Muse S Athena. No Optics / ACC / Gyro / Battery
    # stream. Override per-instance via the ``preset`` constructor
    # argument, e.g. pass ``preset="p1041"`` to also enable Optics16 etc.
    PRESET = "p50"

    # Supported presets we know about. The board will still try unknown
    # values verbatim - this list is just informational / for logging.
    KNOWN_PRESETS = {
        "p50":   "EEG4 only (default ultra-low-power)",
        "p1041": "EEG8 + Optics16 + ACCGYRO + Battery (full sensors)",
        "p1034": "EEG8 + Optics8 (full sensors, bright LED)",
        "p20":   "EEG4 + ACCGYRO (no optics)",
        "p21":   "EEG4 + PPG (BrainFlow native default)",
    }

    # Presets that emit EEG only - we can skip OTHER characteristic
    # subscription and downstream PPG processing entirely. Per the OpenMuse
    # preset table, p20/p21/p50/p51/p60/p61 all stream EEG4 without optics.
    EEG_ONLY_PRESETS = frozenset({"p20", "p21", "p50", "p51", "p60", "p61"})

    def __init__(self, preset: Optional[str] = None) -> None:
        self._preset = (preset or self.PRESET).strip() or self.PRESET
        self._address: Optional[str] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ble_thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._connected_evt = threading.Event()
        self._error_evt = threading.Event()
        self._error_msg: Optional[str] = None

        # Thread-safe ring buffers
        # EEG: each entry = (wall_timestamp, np.array([TP9, AF7, AF8, TP10]))
        # PPG: each entry = (wall_timestamp, np.array([red, ir, amb]))
        self._eeg_buf: deque = deque(maxlen=_EEG_RATE * 120)
        self._ppg_buf: deque = deque(maxlen=_PPG_RATE * 120)
        self._lock = threading.Lock()

        # make_timestamps state: (base_time, wrap_offset, last_abs_tick, sample_counter)
        self._eeg_ts_state = [None, 0, 0, 0]
        self._ppg_ts_state = [None, 0, 0, 0]

    # ── BoardShim-compatible API ──────────────────────────────────────────────

    def get_board_id(self):
        return BoardIds.MUSE_S_BOARD

    def prepare_session(self) -> None:
        """Scan for and identify a Muse S Athena device via BLE."""
        backend = BleakBackend()
        devices = backend.scan(timeout=10)
        muses = [d for d in devices if d.get("name") and "muse" in d["name"].lower()]
        if not muses:
            raise BrainFlowError(
                "No Muse device found",
                BrainFlowExitCodes.BOARD_NOT_READY_ERROR.value,
            )
        for d in muses:
            logger.info("[MuseAthena] Found: %s @ %s", d["name"], d["address"])
        self._address = muses[0]["address"]
        logger.info(
            "[MuseAthena] Connecting to: %s @ %s",
            muses[0]["name"], self._address,
        )

    def start_stream(self, streamer_params=None) -> None:
        """Start BLE data collection in a background asyncio thread."""
        self._stop_evt.clear()
        self._connected_evt.clear()
        self._error_evt.clear()
        self._error_msg = None
        # Reset timestamping state so resumed sessions start clean.
        self._eeg_ts_state = [None, 0, 0, 0]
        self._ppg_ts_state = [None, 0, 0, 0]

        self._loop = asyncio.new_event_loop()
        self._ble_thread = threading.Thread(
            target=self._run_loop, daemon=True, name="MuseAthenaBLE",
        )
        self._ble_thread.start()

        if not self._connected_evt.wait(timeout=20):
            # Connection failed or timed out
            self._stop_evt.set()
            if self._error_msg:
                raise BrainFlowError(
                    f"BLE connection failed: {self._error_msg}",
                    BrainFlowExitCodes.BOARD_NOT_READY_ERROR.value,
                )
            raise BrainFlowError(
                "BLE connection timed out",
                BrainFlowExitCodes.BOARD_NOT_READY_ERROR.value,
            )

    def stop_stream(self) -> None:
        self._stop_evt.set()
        if self._ble_thread and self._ble_thread.is_alive():
            self._ble_thread.join(timeout=5)
        self._ble_thread = None

    def release_session(self) -> None:
        self.stop_stream()
        self._address = None

    def config_board(self, cmd: str) -> None:
        """No-op: preset and start commands are sent during start_stream."""
        pass

    def get_current_board_data(
        self,
        num_samples: int,
        preset=BrainFlowPresets.DEFAULT_PRESET,
    ) -> np.ndarray:
        with self._lock:
            if preset == BrainFlowPresets.ANCILLARY_PRESET:
                return self._read_ppg_buf(num_samples)
            return self._read_eeg_buf(num_samples)

    # ── Liveness helpers (for auto-reconnect) ─────────────────────────────────

    @property
    def is_alive(self) -> bool:
        """True while the BLE thread is connected and streaming."""
        if self._stop_evt.is_set():
            return False
        thread = self._ble_thread
        if thread is None or not thread.is_alive():
            return False
        return self._connected_evt.is_set() and not self._error_evt.is_set()

    @property
    def last_error(self) -> Optional[str]:
        return self._error_msg

    @property
    def preset(self) -> str:
        """The active streaming preset (e.g. ``p1041`` or ``p50``)."""
        return self._preset

    @property
    def is_eeg_only(self) -> bool:
        """True when the active preset streams EEG only (no optics/PPG)."""
        return self._preset in self.EEG_ONLY_PRESETS

    # ── Internal BLE loop ─────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._ble_task())
        except Exception as exc:
            self._error_msg = repr(exc)
            self._error_evt.set()
            logger.warning("[MuseAthena] BLE loop terminated: %r", exc)
        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            # Ensure waiters in start_stream don't block forever if the
            # task died before reaching MuseS.connect_and_initialize().
            self._connected_evt.set() if self._error_evt.is_set() else None

    async def _ble_task(self) -> None:
        def _on_data(sender, raw: bytearray):
            now = time.time()
            uuid_str = str(sender.uuid) if hasattr(sender, "uuid") else str(sender)
            ts_str = datetime.fromtimestamp(now, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            )
            parsed = parse_message(f"{ts_str}\t{uuid_str}\t{raw.hex()}")
            self._process_eeg(parsed.get("EEG", []), now)
            if not self.is_eeg_only:
                self._process_ppg(parsed.get("OPTICS", []), now)

        if self.is_eeg_only:
            subscribed_chars = (MuseS.EEG_UUID,)
        else:
            subscribed_chars = MuseS.DATA_CHARACTERISTICS
        callbacks = {uuid: _on_data for uuid in subscribed_chars}
        try:
            async with bleak.BleakClient(self._address, timeout=15.0) as client:
                await MuseS.connect_and_initialize(
                    client, self._preset, callbacks, verbose=False,
                )
                self._connected_evt.set()

                while not self._stop_evt.is_set() and client.is_connected:
                    await asyncio.sleep(0.05)

                if not client.is_connected and not self._stop_evt.is_set():
                    # Device dropped the link from its side.
                    self._error_msg = "BLE peripheral disconnected"
                    self._error_evt.set()

                try:
                    await MuseS.stop_streaming(client)
                except Exception:
                    # If the device is already gone, ignore.
                    pass
        except Exception as exc:
            self._error_msg = repr(exc)
            self._error_evt.set()
            # Wake up waiters in start_stream()
            self._connected_evt.set()
            raise

    # ── Data processing ───────────────────────────────────────────────────────

    def _process_eeg(self, subpackets, now: float) -> None:
        if not subpackets:
            return
        array, *self._eeg_ts_state = make_timestamps(subpackets, *self._eeg_ts_state)
        if array.size == 0:
            return
        device_latest = array[-1, 0]
        with self._lock:
            for row in array:
                wall_t = now - (device_latest - row[0])
                self._eeg_buf.append((wall_t, row[1:5].copy()))

    def _process_ppg(self, subpackets, now: float) -> None:
        if not subpackets:
            return
        array, *self._ppg_ts_state = make_timestamps(subpackets, *self._ppg_ts_state)
        if array.size == 0:
            return
        n_optics = array.shape[1] - 1
        if n_optics < 16:
            return
        device_latest = array[-1, 0]
        with self._lock:
            for row in array:
                wall_t = now - (device_latest - row[0])
                ch = row[1:]
                ppg = np.array([
                    np.mean(ch[_OPTICS_RED_IDX]),
                    np.mean(ch[_OPTICS_IR_IDX]),
                    np.mean(ch[_OPTICS_AMB_IDX]),
                ], dtype=np.float32)
                self._ppg_buf.append((wall_t, ppg))

    # ── Ring buffer readout ───────────────────────────────────────────────────

    def _read_eeg_buf(self, num_samples: int) -> np.ndarray:
        buf_len = len(self._eeg_buf)
        samples = list(itertools.islice(self._eeg_buf, max(0, buf_len - num_samples), None))
        n = len(samples)
        if not n:
            return np.zeros((_EEG_NUM_ROWS, 0), dtype=np.float64)

        timestamps, eeg_data = zip(*samples)
        out = np.zeros((_EEG_NUM_ROWS, n), dtype=np.float64)
        out[1:5, :] = np.array(eeg_data).T
        out[_EEG_TS_CHAN, :] = np.array(timestamps)
        return out

    def _read_ppg_buf(self, num_samples: int) -> np.ndarray:
        buf_len = len(self._ppg_buf)
        samples = list(itertools.islice(self._ppg_buf, max(0, buf_len - num_samples), None))
        n = len(samples)
        if not n:
            return np.zeros((_PPG_NUM_ROWS, 0), dtype=np.float64)

        timestamps, ppg_data = zip(*samples)
        out = np.zeros((_PPG_NUM_ROWS, n), dtype=np.float64)
        out[_PPG_CHANNELS, :] = np.array(ppg_data).T
        out[_PPG_TS_CHAN, :] = np.array(timestamps)
        return out
