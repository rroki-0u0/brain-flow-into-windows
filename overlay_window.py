"""
Transparent, click-through overlay window that renders a HueShift
gradient border around the edges of the selected display.

The gradient fades from **fully transparent** (screen-centre side) to
**opaque** (screen-edge side).  Colour is determined by the current
brain-state metrics (focus / relaxation → HSV Hue).

Implementation notes
--------------------
* Per-pixel alpha is achieved via Win32 ``UpdateLayeredWindow``
  with a premultiplied BGRA DIB section.  This replaces the previous
  Tk ``-transparentcolor`` colour-key approach, which only supports
  *binary* transparency and made the gradient mid-tones render as
  opaque black.
* Win32 ``WS_EX_TRANSPARENT | WS_EX_LAYERED`` makes the window
  click-through so it never steals focus or blocks mouse events.
* The Windows taskbar typically lives along the bottom edge, so the
  alpha mask deliberately omits the bottom edge — only top, left and
  right edges are highlighted.
* A pre-computed alpha mask is generated once per border-width /
  monitor change.  Each frame only re-blends the foreground colour,
  avoiding expensive per-pixel re-generation of the gradient shape.
"""

from __future__ import annotations

import colorsys
import logging
import tkinter as tk
from typing import Optional, Tuple

import numpy as np

import config
from display_manager import MonitorInfo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Win32 bindings (only needed on Windows).
# ---------------------------------------------------------------------------
try:
    import ctypes
    from ctypes import wintypes, byref, c_void_p
    import win32gui
    import win32con
    _HAS_WIN32 = True
except ImportError:  # pragma: no cover – module still importable elsewhere
    _HAS_WIN32 = False


if _HAS_WIN32:
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    # --- GetAncestor flag ---
    _GA_ROOT = 2

    # --- UpdateLayeredWindow flags ---
    _ULW_ALPHA = 0x00000002

    # --- BLENDFUNCTION fields ---
    _AC_SRC_OVER = 0x00
    _AC_SRC_ALPHA = 0x01

    # --- DIB / BITMAPINFOHEADER ---
    _BI_RGB = 0
    _DIB_RGB_COLORS = 0

    # --- ShowWindow nCmdShow values ---
    _SW_HIDE = 0
    _SW_SHOWNOACTIVATE = 4

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class _BITMAPINFO(ctypes.Structure):
        _fields_ = [
            ("bmiHeader", _BITMAPINFOHEADER),
            ("bmiColors", wintypes.DWORD * 3),
        ]

    class _POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class _SIZE(ctypes.Structure):
        _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]

    class _BLENDFUNCTION(ctypes.Structure):
        _fields_ = [
            ("BlendOp", ctypes.c_ubyte),
            ("BlendFlags", ctypes.c_ubyte),
            ("SourceConstantAlpha", ctypes.c_ubyte),
            ("AlphaFormat", ctypes.c_ubyte),
        ]

    # ------------------------------------------------------------------
    # Declare argtypes / restypes for every Win32 function we call so that
    # 64-bit HANDLE values (HDC, HBITMAP, HWND, HGDIOBJ) are passed as
    # pointer-sized integers instead of c_int (which would overflow on
    # 64-bit Python with "int too long to convert").
    # ------------------------------------------------------------------
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND

    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC

    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int

    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL

    user32.UpdateLayeredWindow.argtypes = [
        wintypes.HWND, wintypes.HDC,
        ctypes.POINTER(_POINT), ctypes.POINTER(_SIZE),
        wintypes.HDC, ctypes.POINTER(_POINT),
        wintypes.DWORD, ctypes.POINTER(_BLENDFUNCTION), wintypes.DWORD,
    ]
    user32.UpdateLayeredWindow.restype = wintypes.BOOL

    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC

    gdi32.CreateDIBSection.argtypes = [
        wintypes.HDC, ctypes.POINTER(_BITMAPINFO), wintypes.UINT,
        ctypes.POINTER(c_void_p), wintypes.HANDLE, wintypes.DWORD,
    ]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP

    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ

    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL

    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL

    gdi32.GdiFlush.argtypes = []
    gdi32.GdiFlush.restype = wintypes.BOOL


# ---------------------------------------------------------------------------
# Hue helper (shared between overlay and tray)
# ---------------------------------------------------------------------------
def _short_arc_lerp(h1: float, h2: float, t: float) -> float:
    """Interpolate from hue ``h1`` to hue ``h2`` (degrees) along the
    shorter arc of the hue circle, with ``t`` in [0, 1]."""
    diff = (h2 - h1 + 180) % 360 - 180  # signed shortest distance
    t = max(0.0, min(1.0, t))
    return (h1 + diff * t) % 360


# ---------------------------------------------------------------------------
# Overlay
# ---------------------------------------------------------------------------
class OverlayWindow:
    """Full-screen transparent overlay with a glowing gradient border."""

    def __init__(self, root: tk.Tk, monitor: MonitorInfo) -> None:
        self._root = root
        self._monitor = monitor

        self._border_width: int = config.DEFAULT_BORDER_WIDTH
        self._current_hue: float = config.HUE_BALANCE
        self._target_hue: float = config.HUE_BALANCE
        self._last_rendered_hue: Optional[float] = None
        self._visible: bool = True

        # Pre-computed alpha mask, rebuilt when border / monitor changes.
        self._alpha_mask: Optional[np.ndarray] = None  # shape (H, W), float32 0-1

        # Win32 / DIB state
        self._root_hwnd: Optional[int] = None
        self._screen_dc = None
        self._mem_dc = None
        self._dib = None
        self._old_obj = None
        self._dib_bits_ptr: Optional[int] = None
        self._dib_size: Tuple[int, int] = (0, 0)

        # --- Tkinter window setup ---
        # The Tk window only exists to give us a real HWND that we then
        # take over with UpdateLayeredWindow. We intentionally do NOT
        # set ``-transparentcolor`` (which would force binary colour-key
        # transparency and conflict with UpdateLayeredWindow).
        self._root.overrideredirect(True)
        self._root.attributes("-topmost", True)
        self._apply_geometry(monitor)
        # Force the window to materialise so winfo_id() returns a real HWND.
        self._root.update_idletasks()

        # Set up Win32 styles + DIB
        self._init_win32()

        # Build mask + initial render
        self._rebuild_alpha_mask()
        self._render()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_metrics(self, focus: float, relaxation: float) -> None:
        """Set the target hue based on new focus / relaxation values."""
        self._target_hue = self._compute_hue(focus, relaxation)

    def set_border_width(self, width: int) -> None:
        """Change the border thickness and re-render."""
        self._border_width = max(config.MIN_BORDER_WIDTH, min(width, config.MAX_BORDER_WIDTH))
        self._rebuild_alpha_mask()
        self._last_rendered_hue = None
        self._render()

    def set_monitor(self, monitor: MonitorInfo) -> None:
        """Move the overlay to a different display (re-allocates the DIB)."""
        self._monitor = monitor
        self._apply_geometry(monitor)
        self._free_dib()
        self._alloc_dib()
        self._rebuild_alpha_mask()
        self._last_rendered_hue = None
        self._render()

    def set_visible(self, visible: bool) -> None:
        if visible == self._visible:
            return
        self._visible = visible
        if not _HAS_WIN32 or self._root_hwnd is None:
            return
        if visible:
            user32.ShowWindow(self._root_hwnd, _SW_SHOWNOACTIVATE)
            self._last_rendered_hue = None
            self._render()
        else:
            user32.ShowWindow(self._root_hwnd, _SW_HIDE)

    def set_disconnected(self) -> None:
        """Show a neutral grey border when the headband is disconnected."""
        self._target_hue = config.HUE_DISCONNECTED

    def tick(self) -> None:
        """Called every ``OVERLAY_UPDATE_INTERVAL_MS`` ms to animate hue."""
        if not self._visible:
            return

        if self._target_hue == config.HUE_DISCONNECTED:
            self._current_hue = config.HUE_DISCONNECTED
        else:
            if self._current_hue == config.HUE_DISCONNECTED:
                self._current_hue = self._target_hue
            else:
                diff = (self._target_hue - self._current_hue + 180) % 360 - 180
                self._current_hue = (self._current_hue + diff * config.HUE_TRANSITION_SPEED) % 360

        if (
            self._last_rendered_hue is None
            or abs(self._current_hue - (self._last_rendered_hue or 0)) > 0.3
        ):
            self._render()

    @property
    def border_width(self) -> int:
        return self._border_width

    # ------------------------------------------------------------------
    # Win32 lifecycle
    # ------------------------------------------------------------------

    def _init_win32(self) -> None:
        if not _HAS_WIN32:
            logger.warning("pywin32 not available – overlay disabled.")
            return

        inner_hwnd = self._root.winfo_id()
        root_hwnd = user32.GetAncestor(inner_hwnd, _GA_ROOT) or inner_hwnd
        self._root_hwnd = root_hwnd

        ex_style = win32gui.GetWindowLong(root_hwnd, win32con.GWL_EXSTYLE)
        new_style = (
            ex_style
            | win32con.WS_EX_LAYERED
            | win32con.WS_EX_TRANSPARENT   # mouse passes through
            | win32con.WS_EX_TOOLWINDOW    # hide from Alt-Tab / taskbar
            | win32con.WS_EX_TOPMOST
            | win32con.WS_EX_NOACTIVATE    # never steal focus
        )
        win32gui.SetWindowLong(root_hwnd, win32con.GWL_EXSTYLE, new_style)

        # Allocate the DIB section we render into.
        self._alloc_dib()
        logger.info(
            "Overlay layered window ready (root HWND=0x%X, inner=0x%X, size=%dx%d).",
            root_hwnd, inner_hwnd, self._monitor.width, self._monitor.height,
        )

    def _alloc_dib(self) -> None:
        """Allocate a 32-bit top-down DIB section sized to the current monitor."""
        if not _HAS_WIN32:
            return
        w, h = self._monitor.width, self._monitor.height

        self._screen_dc = user32.GetDC(0)
        self._mem_dc = gdi32.CreateCompatibleDC(self._screen_dc)

        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h            # negative → top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = _BI_RGB

        bits_ptr = c_void_p()
        gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        gdi32.CreateDIBSection.argtypes = [
            wintypes.HDC, ctypes.POINTER(_BITMAPINFO), wintypes.UINT,
            ctypes.POINTER(c_void_p), wintypes.HANDLE, wintypes.DWORD,
        ]
        self._dib = gdi32.CreateDIBSection(
            self._screen_dc, byref(bmi), _DIB_RGB_COLORS,
            byref(bits_ptr), None, 0,
        )
        self._dib_bits_ptr = bits_ptr.value
        self._old_obj = gdi32.SelectObject(self._mem_dc, self._dib)
        self._dib_size = (w, h)

    def _free_dib(self) -> None:
        if not _HAS_WIN32:
            return
        if self._mem_dc and self._old_obj:
            gdi32.SelectObject(self._mem_dc, self._old_obj)
        if self._dib:
            gdi32.DeleteObject(self._dib)
        if self._mem_dc:
            gdi32.DeleteDC(self._mem_dc)
        if self._screen_dc:
            user32.ReleaseDC(0, self._screen_dc)
        self._dib = None
        self._old_obj = None
        self._mem_dc = None
        self._screen_dc = None
        self._dib_bits_ptr = None

    # ------------------------------------------------------------------
    # Geometry
    # ------------------------------------------------------------------

    def _apply_geometry(self, m: MonitorInfo) -> None:
        self._root.geometry(f"{m.width}x{m.height}+{m.x}+{m.y}")

    # ------------------------------------------------------------------
    # Alpha mask
    # ------------------------------------------------------------------

    def _rebuild_alpha_mask(self) -> None:
        """Pre-compute a float alpha mask (0 = transparent, 1 = opaque).

        Gradient: smooth-step from inner edge of border (0) to outer
        edge at the screen perimeter (1). The **bottom** edge is
        deliberately excluded so the overlay does not cover the
        Windows taskbar.
        """
        w, h = self._monitor.width, self._monitor.height
        bw = self._border_width

        rows = np.arange(h, dtype=np.float32)
        cols = np.arange(w, dtype=np.float32)

        dist_top = rows                          # (H,)
        dist_left = cols                          # (W,)
        dist_right = (w - 1) - cols              # (W,)

        # NOTE: dist_bottom is intentionally NOT included so the bottom
        # edge (where the Windows taskbar lives) is left transparent.
        vert_dist = dist_top[:, np.newaxis]      # (H, 1) – top only
        horiz_dist = np.minimum(dist_left, dist_right)[np.newaxis, :]  # (1, W)
        min_dist = np.minimum(vert_dist, horiz_dist)  # (H, W)

        in_border = min_dist < bw
        t = np.where(in_border, 1.0 - min_dist / max(bw, 1), 0.0)
        t = np.clip(t, 0.0, 1.0)
        mask = t * t * (3.0 - 2.0 * t)  # smoothstep

        # Scale to the user-configured peak alpha so the edge is opaque
        # but never quite fully so (looks softer on bright wallpapers).
        peak = config.MAX_ALPHA / 255.0
        self._alpha_mask = (mask * peak).astype(np.float32)
        logger.debug("Alpha mask rebuilt: %dx%d, border=%dpx", w, h, bw)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render(self) -> None:
        """Build a premultiplied BGRA buffer and push it via UpdateLayeredWindow."""
        if not _HAS_WIN32 or self._root_hwnd is None or self._dib_bits_ptr is None:
            return
        if self._alpha_mask is None:
            return

        w, h = self._monitor.width, self._monitor.height
        if (w, h) != self._dib_size:
            # Monitor resolution changed – reallocate.
            self._free_dib()
            self._alloc_dib()
            if self._dib_bits_ptr is None:
                return

        # --- Foreground colour (RGB 0-255) ---
        if self._current_hue == config.HUE_DISCONNECTED:
            r_f, g_f, b_f = 110.0, 110.0, 110.0
        else:
            hue_norm = (self._current_hue % 360) / 360.0
            r, g, b = colorsys.hsv_to_rgb(
                hue_norm, config.GRADIENT_SATURATION, config.GRADIENT_VALUE,
            )
            r_f, g_f, b_f = r * 255.0, g * 255.0, b * 255.0

        alpha_f = self._alpha_mask                          # (H, W) float 0-1
        a_u8 = np.clip(alpha_f * 255.0, 0, 255).astype(np.uint8)

        # Premultiplied colour channels (required by UpdateLayeredWindow).
        b_pre = np.clip(b_f * alpha_f, 0, 255).astype(np.uint8)
        g_pre = np.clip(g_f * alpha_f, 0, 255).astype(np.uint8)
        r_pre = np.clip(r_f * alpha_f, 0, 255).astype(np.uint8)

        # BGRA layout, packed top-down to match the DIB orientation.
        bgra = np.dstack([b_pre, g_pre, r_pre, a_u8])
        buf = np.ascontiguousarray(bgra)

        # Copy bits into the DIB and flush the GDI pipeline so
        # UpdateLayeredWindow sees the latest content.
        ctypes.memmove(self._dib_bits_ptr, buf.ctypes.data, w * h * 4)
        gdi32.GdiFlush()

        blend = _BLENDFUNCTION(_AC_SRC_OVER, 0, 255, _AC_SRC_ALPHA)
        pt_src = _POINT(0, 0)
        size = _SIZE(w, h)
        pt_dst = _POINT(self._monitor.x, self._monitor.y)

        user32.UpdateLayeredWindow.argtypes = [
            wintypes.HWND, wintypes.HDC,
            ctypes.POINTER(_POINT), ctypes.POINTER(_SIZE),
            wintypes.HDC, ctypes.POINTER(_POINT),
            wintypes.DWORD, ctypes.POINTER(_BLENDFUNCTION), wintypes.DWORD,
        ]
        user32.UpdateLayeredWindow.restype = wintypes.BOOL
        ok = user32.UpdateLayeredWindow(
            self._root_hwnd, self._screen_dc,
            byref(pt_dst), byref(size),
            self._mem_dc, byref(pt_src),
            0, byref(blend), _ULW_ALPHA,
        )
        if not ok:
            err = ctypes.get_last_error()
            logger.warning("UpdateLayeredWindow failed (err=%d).", err)

        self._last_rendered_hue = self._current_hue

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_hue(focus: float, relaxation: float) -> float:
        """Map the focus/relax mix to a single HSV hue (degrees).

        Three-anchor piecewise interpolation along the shorter arc of
        the hue circle:

            relax_weight 0.0 (pure focus)   -> HUE_FOCUS   (red,   0 deg)
            relax_weight 0.5 (balanced)     -> HUE_BALANCE (blue, 240 deg)
            relax_weight 1.0 (pure relax)   -> HUE_RELAX   (green, 120 deg)

        Each segment moves on the shortest arc, so the path is
        red -> magenta/purple -> blue -> cyan -> green.
        """
        total = focus + relaxation
        if total == 0:
            return config.HUE_BALANCE
        w = relaxation / total
        if w <= 0.5:
            return _short_arc_lerp(config.HUE_FOCUS, config.HUE_BALANCE, w * 2.0)
        return _short_arc_lerp(config.HUE_BALANCE, config.HUE_RELAX, (w - 0.5) * 2.0)

    @staticmethod
    def _hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
        hex_color = hex_color.lstrip("#")
        return (
            int(hex_color[0:2], 16),
            int(hex_color[2:4], 16),
            int(hex_color[4:6], 16),
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def destroy(self) -> None:
        """Release DIB / DC resources. Safe to call multiple times."""
        self._free_dib()
