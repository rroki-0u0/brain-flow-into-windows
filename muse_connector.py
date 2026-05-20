"""
Muse S Athena BLE connector built on the MuseAthenaBoard adapter.

Talks to the headband over bleak + the OpenMuse BLE protocol with the
low-power Athena streaming preset (p1041 - EEG8 + Optics16 + ACCGYRO +
Battery), bypassing BrainFlow's native BLE backend so the device can
stay in its lower-power streaming mode while still exposing a
BoardShim-compatible data layout to the rest of the application.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import numpy as np
from brainflow.board_shim import BoardIds
from brainflow.exit_codes import BrainFlowError

import config
from muse_athena import MuseAthenaBoard

logger = logging.getLogger(__name__)


def _resolve_preset() -> Optional[str]:
    """Return the BLE preset selected via the ``MUSE_PRESET`` env var.

    Falls back to ``None`` (which makes ``MuseAthenaBoard`` use its class
    default ``p1034``). Set ``MUSE_PRESET=p1041`` for full sensor
    streaming (EEG + PPG + IMU).
    """
    preset = os.getenv("MUSE_PRESET", "").strip()
    return preset or None


# Default window of EEG samples to read each tick (1 s @ 256 Hz).
_DEFAULT_WINDOW_SAMPLES = 256


class MuseConnector:
    """Thin lifecycle wrapper around :class:`MuseAthenaBoard`."""

    def __init__(self, serial_number: str = "") -> None:
        # serial_number is accepted for backwards compatibility; the
        # bleak-based adapter scans by device name instead.
        self._serial_number = serial_number
        self._board: Optional[MuseAthenaBoard] = None
        self._connected = False
        self._window_samples = _DEFAULT_WINDOW_SAMPLES

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Scan for a Muse and start streaming. Returns True on success."""
        if self._connected:
            logger.warning("Already connected - skipping.")
            return True

        preset = _resolve_preset()
        board = MuseAthenaBoard(preset=preset)
        preset_desc = MuseAthenaBoard.KNOWN_PRESETS.get(board.preset, "custom")
        try:
            logger.info(
                "Scanning for Muse S (preset=%s - %s)...",
                board.preset, preset_desc,
            )
            board.prepare_session()
            board.start_stream()
        except BrainFlowError as exc:
            logger.warning("MuseAthenaBoard connect failed: %s", exc)
            try:
                board.release_session()
            except Exception:
                pass
            self._connected = False
            self._board = None
            return False
        except Exception as exc:
            logger.warning("Unexpected error during connect: %r", exc)
            try:
                board.release_session()
            except Exception:
                pass
            self._connected = False
            self._board = None
            return False

        self._board = board
        self._connected = True
        logger.info("Muse S Athena session started (preset=%s).", board.preset)
        return True

    def disconnect(self) -> None:
        """Stop streaming and release resources."""
        if self._board is None:
            self._connected = False
            return
        try:
            self._board.release_session()
            logger.info("Muse S session released.")
        except BrainFlowError as exc:
            logger.warning("Error during disconnect: %s", exc)
        except Exception as exc:
            logger.warning("Unexpected error during disconnect: %r", exc)
        finally:
            self._connected = False
            self._board = None

    def get_data(self) -> Optional[np.ndarray]:
        """Return the most recent EEG window (channels x samples).

        Returns ``None`` if the link has died or there are not yet enough
        samples for downstream metric computation.
        """
        if not self._connected or self._board is None:
            return None

        # Detect a silent link loss so the supervisor can trigger a reconnect.
        if not self._board.is_alive:
            err = self._board.last_error
            logger.warning(
                "MuseAthenaBoard link dropped (%s); will trigger reconnect.",
                err or "unknown reason",
            )
            self._connected = False
            return None

        try:
            data = self._board.get_current_board_data(self._window_samples)
        except Exception as exc:
            logger.error("Error reading data: %r", exc)
            self._connected = False
            return None

        if data.shape[1] < config.MIN_SAMPLES:
            return None
        return data

    @property
    def is_connected(self) -> bool:
        # Self-heal: if the underlying board died we surface that.
        if self._connected and self._board is not None and not self._board.is_alive:
            self._connected = False
        return self._connected

    @property
    def board_id(self) -> Optional[int]:
        if self._board is None:
            return None
        return int(self._board.get_board_id())

    @property
    def board(self) -> Optional[MuseAthenaBoard]:
        return self._board
