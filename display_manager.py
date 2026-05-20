"""
Multi-display manager using the *screeninfo* library.

Enumerates connected monitors and provides geometry (position + size)
so the overlay window can be placed on the correct display.
"""

import logging
from dataclasses import dataclass
from typing import List

from screeninfo import get_monitors, Monitor

logger = logging.getLogger(__name__)


@dataclass
class MonitorInfo:
    """Simplified monitor descriptor used by the rest of the app."""
    name: str
    x: int
    y: int
    width: int
    height: int
    is_primary: bool


class DisplayManager:
    """Discovers monitors and lets the user pick which one to overlay."""

    def __init__(self) -> None:
        self._monitors: List[MonitorInfo] = []
        self._selected_index: int = 0
        self.refresh()

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Re-enumerate connected monitors."""
        raw: List[Monitor] = get_monitors()
        self._monitors = []
        primary_idx = 0
        for i, m in enumerate(raw):
            info = MonitorInfo(
                name=m.name or f"Display {i + 1}",
                x=m.x,
                y=m.y,
                width=m.width,
                height=m.height,
                is_primary=m.is_primary if m.is_primary is not None else (i == 0),
            )
            self._monitors.append(info)
            if info.is_primary:
                primary_idx = i

        # Default to primary display
        if self._selected_index >= len(self._monitors):
            self._selected_index = primary_idx

        logger.info(
            "Detected %d monitor(s). Selected: %s",
            len(self._monitors),
            self.selected.name if self._monitors else "none",
        )

    @property
    def monitors(self) -> List[MonitorInfo]:
        return list(self._monitors)

    @property
    def selected(self) -> MonitorInfo:
        return self._monitors[self._selected_index]

    @property
    def selected_index(self) -> int:
        return self._selected_index

    def select(self, index: int) -> None:
        """Select a monitor by its 0-based index."""
        if 0 <= index < len(self._monitors):
            self._selected_index = index
            logger.info("Display switched to %s", self._monitors[index].name)
        else:
            logger.warning("Invalid monitor index %d (have %d)", index, len(self._monitors))
