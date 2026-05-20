"""
Brain Flow Overlay – main entry point.

Connects to a Muse S Athena EEG headband via BrainFlow, computes
real-time focus / relaxation metrics, and displays a HueShift
gradient border overlay on the selected Windows display.

Architecture
------------
* **Main thread** – Tkinter event-loop + overlay rendering.
* **Data thread** – BrainFlow data acquisition + metric computation.
* Communication via ``queue.Queue``.
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from typing import Any, Dict, Optional

from dotenv import load_dotenv

import brain_metrics
import config
from display_manager import DisplayManager
from muse_connector import MuseConnector
from overlay_window import OverlayWindow
from tray_app import TrayApp

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("brain_overlay")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
class BrainOverlayApp:
    """Top-level controller that wires all components together."""

    def __init__(self) -> None:
        load_dotenv()

        # Core components
        serial = os.getenv("MUSE_SERIAL_NUMBER", "")
        self._connector = MuseConnector(serial_number=serial)
        self._display_mgr = DisplayManager()

        # Tkinter root (must be created in the main thread)
        self._root = tk.Tk()
        self._root.withdraw()  # hide the default root window

        # Overlay on selected monitor
        self._overlay_root = tk.Toplevel(self._root)
        self._overlay = OverlayWindow(self._overlay_root, self._display_mgr.selected)

        # System tray
        self._tray = TrayApp(
            display_manager=self._display_mgr,
            on_border_change=self._on_border_change,
            on_display_change=self._on_display_change,
            on_toggle_overlay=self._on_toggle_overlay,
            on_reconnect=self._on_reconnect,
            on_quit=self._on_quit,
        )

        # Thread-safe metric queue
        self._metric_queue: queue.Queue[Optional[Dict[str, Any]]] = queue.Queue(maxsize=4)
        self._running = threading.Event()
        self._data_thread: Optional[threading.Thread] = None

        # Guards concurrent reconnect attempts triggered from the tray menu.
        self._reconnect_lock = threading.Lock()
        self._reconnecting = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start everything and enter the Tkinter main-loop."""
        logger.info("=== Brain Flow Overlay starting ===")

        # Start tray icon
        self._tray.start()

        # Attempt initial connection (non-blocking on failure)
        self._start_data_thread()

        # Schedule periodic UI updates
        self._schedule_overlay_tick()
        self._schedule_metric_poll()

        # Enter main-loop (blocks until quit)
        try:
            self._root.mainloop()
        finally:
            self._shutdown()

    # ------------------------------------------------------------------
    # Data thread
    # ------------------------------------------------------------------

    def _start_data_thread(self) -> None:
        """Connect to Muse S and begin reading data in a background thread."""
        if self._running.is_set():
            return

        self._running.set()
        self._data_thread = threading.Thread(target=self._data_loop, daemon=True)
        self._data_thread.start()

    def _data_loop(self) -> None:
        """Background loop: connect → read → compute → enqueue.

        Runs an outer supervisor loop that keeps the headband connected
        with exponential backoff until either the link is alive or the
        user quits / requests a manual reconnect (both clear
        ``self._running``).
        """
        backoff = 1.0          # seconds; doubles on each failure
        backoff_max = 30.0

        while self._running.is_set():
            # --------------------------------------------------------------
            # Phase 1: (Re)connect with exponential backoff.
            # --------------------------------------------------------------
            logger.info("Attempting to connect to Muse S ...")
            self._publish_disconnected()

            if not self._connector.connect():
                # Connection failure - back off and retry.
                logger.warning(
                    "Connect failed; retrying in %.1f s (exponential backoff).",
                    backoff,
                )
                self._sleep_interruptible(backoff)
                backoff = min(backoff * 2.0, backoff_max)
                continue

            # Connected! Reset backoff for the next disconnect cycle.
            backoff = 1.0
            logger.info("Connected. Warming up for initial data accumulation ...")
            self._sleep_interruptible(2.0)

            # --------------------------------------------------------------
            # Phase 2: Stream + compute metrics until link drops or quit.
            # --------------------------------------------------------------
            while self._running.is_set() and self._connector.is_connected:
                try:
                    data = self._connector.get_data()
                except Exception as exc:
                    logger.error("Unhandled error in get_data(): %r", exc)
                    data = None

                if data is not None:
                    try:
                        metrics = brain_metrics.calculate_metrics(
                            data, self._connector.board_id,
                        )
                    except Exception as exc:
                        logger.error("calculate_metrics failed: %r", exc)
                        metrics = None

                    if metrics:
                        try:
                            self._metric_queue.put_nowait(metrics)
                        except queue.Full:
                            try:
                                self._metric_queue.get_nowait()
                            except queue.Empty:
                                pass
                            self._metric_queue.put_nowait(metrics)

                self._sleep_interruptible(config.DATA_READ_INTERVAL_S)

            # Fell out of phase 2 - the link dropped or user quit.
            if not self._running.is_set():
                break

            logger.warning("Muse link lost; will reconnect with backoff.")
            try:
                self._connector.disconnect()
            except Exception as exc:
                logger.warning("Cleanup disconnect raised: %r", exc)
            self._publish_disconnected()
            # Loop continues - phase 1 will retry with current backoff.

        # NOTE: do not call self._connector.disconnect() here.
        # Shutdown is coordinated by _shutdown() / _on_reconnect(),
        # which join this thread first and then disconnect exactly once.

    # ------------------------------------------------------------------
    # Helpers for data thread
    # ------------------------------------------------------------------

    def _sleep_interruptible(self, total_seconds: float) -> None:
        """``time.sleep`` that wakes up early if ``self._running`` clears."""
        deadline = time.monotonic() + total_seconds
        while self._running.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.25))

    def _publish_disconnected(self) -> None:
        """Push a sentinel None into the metric queue so the UI shows offline."""
        try:
            self._metric_queue.put_nowait(None)
        except queue.Full:
            try:
                self._metric_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._metric_queue.put_nowait(None)
            except queue.Full:
                pass

    # ------------------------------------------------------------------
    # Tkinter scheduled callbacks
    # ------------------------------------------------------------------

    def _schedule_overlay_tick(self) -> None:
        """Smoothly animate the overlay hue every N ms."""
        self._overlay.tick()
        self._root.after(config.OVERLAY_UPDATE_INTERVAL_MS, self._schedule_overlay_tick)

    def _schedule_metric_poll(self) -> None:
        """Poll the metric queue and feed new values to the overlay + tray."""
        try:
            metrics = self._metric_queue.get_nowait()
            if metrics is None:
                # Disconnected
                self._overlay.set_disconnected()
                self._tray.update_status(connected=False)
            else:
                focus = metrics["focus"]
                relax = metrics["relaxation"]
                self._overlay.update_metrics(focus, relax)
                self._tray.update_status(connected=True, focus=focus, relaxation=relax)
                logger.debug("Focus=%.1f%%  Relax=%.1f%%", focus * 100, relax * 100)
        except queue.Empty:
            pass

        self._root.after(500, self._schedule_metric_poll)

    # ------------------------------------------------------------------
    # Tray callbacks
    # ------------------------------------------------------------------

    def _on_border_change(self, width: int) -> None:
        # Dispatch to main thread (called from pystray thread)
        self._root.after(0, lambda: self._overlay.set_border_width(width))

    def _on_display_change(self, index: int) -> None:
        def _apply():
            self._display_mgr.select(index)
            self._overlay.set_monitor(self._display_mgr.selected)
        self._root.after(0, _apply)

    def _on_toggle_overlay(self, visible: bool) -> None:
        self._root.after(0, lambda: self._overlay.set_visible(visible))

    def _on_reconnect(self) -> None:
        # Drop the request if a previous reconnect is still in flight; otherwise
        # rapid menu clicks could spawn overlapping threads that race on
        # _data_thread.join() and connector.disconnect().
        with self._reconnect_lock:
            if self._reconnecting:
                logger.info("Reconnect already in progress; ignoring request.")
                return
            self._reconnecting = True

        def _do_reconnect():
            try:
                # Stop existing data thread
                if self._running.is_set():
                    self._running.clear()
                    if self._data_thread:
                        self._data_thread.join(timeout=5)
                    self._connector.disconnect()
                # Restart
                self._start_data_thread()
            finally:
                with self._reconnect_lock:
                    self._reconnecting = False
        # Run reconnect in a separate thread to avoid blocking main loop
        threading.Thread(target=_do_reconnect, daemon=True).start()

    def _on_quit(self) -> None:
        logger.info("Quit requested.")
        self._running.clear()
        self._tray.stop()
        self._root.after(0, self._root.destroy)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _shutdown(self) -> None:
        self._running.clear()
        # Wait for the data thread to exit before releasing the BrainFlow
        # session so we don't race with an in-flight get_board_data().
        if self._data_thread is not None and self._data_thread.is_alive():
            self._data_thread.join(timeout=5)
        self._connector.disconnect()
        self._tray.stop()
        logger.info("=== Brain Flow Overlay stopped ===")


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
def main() -> None:
    app = BrainOverlayApp()
    app.run()


if __name__ == "__main__":
    main()
