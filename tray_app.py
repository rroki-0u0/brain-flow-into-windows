"""
System-tray resident application using *pystray*.

Provides a context menu for:
  - Viewing connection status and current metrics
  - Adjusting border width
  - Selecting which display to overlay
  - Toggling overlay visibility
  - Reconnecting / disconnecting the headband
  - Quitting the app
"""

from __future__ import annotations

import colorsys
import logging
import threading
from typing import TYPE_CHECKING, Callable, List, Optional

from PIL import Image, ImageDraw, ImageFont
from pystray import Icon, Menu, MenuItem

import config
from overlay_window import _short_arc_lerp

if TYPE_CHECKING:
    from display_manager import DisplayManager

logger = logging.getLogger(__name__)


class TrayApp:
    """System-tray icon with a settings context menu."""

    def __init__(
        self,
        display_manager: "DisplayManager",
        on_border_change: Callable[[int], None],
        on_display_change: Callable[[int], None],
        on_toggle_overlay: Callable[[bool], None],
        on_reconnect: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self._dm = display_manager
        self._on_border_change = on_border_change
        self._on_display_change = on_display_change
        self._on_toggle_overlay = on_toggle_overlay
        self._on_reconnect = on_reconnect
        self._on_quit = on_quit

        self._connected = False
        self._focus: float = 0.0
        self._relaxation: float = 0.0
        self._overlay_visible: bool = True
        self._current_border: int = config.DEFAULT_BORDER_WIDTH

        self._icon: Optional[Icon] = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch the tray icon on a background daemon thread."""
        self._icon = Icon(
            "BrainFlow Overlay",
            icon=self._create_icon(),
            title="Brain Flow Overlay",
            menu=self._build_menu(),
        )
        self._thread = threading.Thread(target=self._icon.run, daemon=True)
        self._thread.start()
        logger.info("System-tray icon started.")

    def stop(self) -> None:
        if self._icon:
            self._icon.stop()

    def update_status(
        self,
        connected: bool,
        focus: float = 0.0,
        relaxation: float = 0.0,
    ) -> None:
        """Update the cached status shown in the context menu."""
        self._connected = connected
        self._focus = focus
        self._relaxation = relaxation
        # Refresh the menu so labels update
        if self._icon:
            self._icon.menu = self._build_menu()
            self._icon.icon = self._create_icon()
            self._icon.update_menu()

    # ------------------------------------------------------------------
    # Menu builder
    # ------------------------------------------------------------------

    def _build_menu(self) -> Menu:
        # --- Status ---
        status_text = "Connected" if self._connected else "Disconnected"
        metric_text = f"Focus {self._focus:.0%}  |  Relax {self._relaxation:.0%}"

        # --- Border width sub-menu ---
        border_items = []
        for w in (10, 20, 30, 40, 60, 80, 100):
            border_items.append(
                MenuItem(
                    f"{'* ' if w == self._current_border else '  '}{w} px",
                    self._make_border_callback(w),
                )
            )

        # --- Display sub-menu ---
        display_items: List[MenuItem] = []
        for idx, m in enumerate(self._dm.monitors):
            marker = "* " if idx == self._dm.selected_index else "  "
            label = f"{marker}{m.name} ({m.width}x{m.height})"
            display_items.append(
                MenuItem(label, self._make_display_callback(idx))
            )

        # --- Toggle overlay ---
        toggle_label = "Hide Overlay" if self._overlay_visible else "Show Overlay"

        return Menu(
            MenuItem(status_text, None, enabled=False),
            MenuItem(metric_text, None, enabled=False),
            Menu.SEPARATOR,
            MenuItem("Border Width", Menu(*border_items)),
            MenuItem("Display", Menu(*display_items)),
            Menu.SEPARATOR,
            MenuItem(toggle_label, self._toggle_overlay),
            MenuItem("Reconnect", self._reconnect),
            Menu.SEPARATOR,
            MenuItem("Quit", self._quit),
        )

    # ------------------------------------------------------------------
    # Callbacks (must be picklable / closure-safe)
    # ------------------------------------------------------------------

    def _make_border_callback(self, width: int) -> Callable:
        def _cb(icon, item):
            self._current_border = width
            self._on_border_change(width)
            if self._icon:
                self._icon.menu = self._build_menu()
                self._icon.update_menu()
        return _cb

    def _make_display_callback(self, index: int) -> Callable:
        def _cb(icon, item):
            self._on_display_change(index)
            if self._icon:
                self._icon.menu = self._build_menu()
                self._icon.update_menu()
        return _cb

    def _toggle_overlay(self, icon, item) -> None:
        self._overlay_visible = not self._overlay_visible
        self._on_toggle_overlay(self._overlay_visible)
        if self._icon:
            self._icon.menu = self._build_menu()
            self._icon.update_menu()

    def _reconnect(self, icon, item) -> None:
        self._on_reconnect()

    def _quit(self, icon, item) -> None:
        self._on_quit()

    # ------------------------------------------------------------------
    # Icon rendering
    # ------------------------------------------------------------------

    def _create_icon(self) -> Image.Image:
        """Generate a 64x64 tray icon with the current HueShift value
        (0-100) rendered as a single large number on a coloured disc.

        HueShift = ``relaxation / (focus + relaxation) * 100`` and
        matches the overlay's hue on the focus<->relax axis.
        """
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # ----- Background colour: hue from Focus+Relax mix, or grey if offline -----
        if self._connected:
            hue = self._compute_hue_deg(self._focus, self._relaxation)
            r, g, b = colorsys.hsv_to_rgb(
                (hue % 360) / 360.0,
                config.GRADIENT_SATURATION,
                config.GRADIENT_VALUE,
            )
            fill = (int(r * 255), int(g * 255), int(b * 255), 255)
        else:
            fill = (120, 120, 120, 255)

        # Solid filled disc - readable at 16x16 as well as 32x32.
        draw.ellipse([1, 1, size - 2, size - 2], fill=fill)

        # ----- Single big HueShift number (0..100) -----
        if self._connected:
            total = self._focus + self._relaxation
            if total > 0:
                hueshift = int(round((self._relaxation / total) * 100))
            else:
                hueshift = 50  # balance
            label = str(max(0, min(100, hueshift)))
        else:
            label = "OFF"

        # Pick the largest font size that fits the disc width with margin.
        font = self._fit_font(draw, label, max_width=size - 8, max_height=size - 6)
        self._draw_centered_outlined(draw, label, font, y_offset=0, size=size)

        return img

    # ------------------------------------------------------------------
    # Icon helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_hue_deg(focus: float, relaxation: float) -> float:
        """Mirror of OverlayWindow._compute_hue() (3-anchor HueShift).

        relax_weight 0.0 -> red (HUE_FOCUS), 0.5 -> blue (HUE_BALANCE),
        1.0 -> green (HUE_RELAX), interpolated piecewise on the shorter arc.
        """
        total = focus + relaxation
        if total == 0:
            return config.HUE_BALANCE
        w = relaxation / total
        if w <= 0.5:
            return _short_arc_lerp(config.HUE_FOCUS, config.HUE_BALANCE, w * 2.0)
        return _short_arc_lerp(config.HUE_BALANCE, config.HUE_RELAX, (w - 0.5) * 2.0)

    @staticmethod
    def _load_overlay_font(px: int) -> ImageFont.ImageFont:
        """Best-effort TTF lookup with a fallback to PIL's default font."""
        for name in ("arialbd.ttf", "arial.ttf", "seguibd.ttf", "segoeui.ttf"):
            try:
                return ImageFont.truetype(name, px)
            except OSError:
                continue
        return ImageFont.load_default()

    @classmethod
    def _fit_font(
        cls,
        draw: ImageDraw.ImageDraw,
        text: str,
        *,
        max_width: int,
        max_height: int,
    ) -> ImageFont.ImageFont:
        """Pick the largest TrueType font that keeps ``text`` within bounds."""
        # Probe from large to small. Bold weights keep the number legible
        # even after the OS down-scales the 64x64 icon to 16x16 in the tray.
        for px in (56, 52, 48, 44, 40, 36, 32, 28, 24, 20, 16):
            font = cls._load_overlay_font(px)
            try:
                bbox = draw.textbbox((0, 0), text, font=font)
                w = bbox[2] - bbox[0]
                h = bbox[3] - bbox[1]
            except AttributeError:
                w, h = draw.textsize(text, font=font)
            if w <= max_width and h <= max_height:
                return font
        return cls._load_overlay_font(16)

    @staticmethod
    def _draw_centered_outlined(
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.ImageFont,
        *,
        y_offset: int,
        size: int,
    ) -> None:
        """Draw white text with a black 1-px outline, horizontally centred."""
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            anchor_x = bbox[0]
            anchor_y = bbox[1]
        except AttributeError:
            text_w, text_h = draw.textsize(text, font=font)
            anchor_x = anchor_y = 0

        x = (size - text_w) // 2 - anchor_x
        y = size // 2 + y_offset - text_h // 2 - anchor_y

        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1)):
            draw.text((x + dx, y + dy), text, fill=(0, 0, 0, 255), font=font)
        draw.text((x, y), text, fill=(255, 255, 255, 255), font=font)
