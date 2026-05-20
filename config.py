"""
Application configuration constants for Brain Flow Overlay.
"""

# --- Border / Overlay ---
DEFAULT_BORDER_WIDTH: int = 20      # Default border thickness in pixels
MIN_BORDER_WIDTH: int = 5           # Minimum border thickness
MAX_BORDER_WIDTH: int = 100         # Maximum border thickness

# --- Timing ---
DATA_READ_INTERVAL_S: float = 1.0           # Seconds between EEG data reads
OVERLAY_UPDATE_INTERVAL_MS: int = 50        # Milliseconds between overlay redraws (animation)
HUE_TRANSITION_SPEED: float = 0.08          # Interpolation factor per frame (0–1, higher = faster)

# --- BrainFlow ---
# Board IDs to try, in order. Athena firmware first, then classic Muse S.
# Resolved at runtime via BrainFlow's BoardIds enum.
MUSE_BOARD_IDS: list = [22, 39]      # Placeholder; overridden at import time

def _resolve_board_ids() -> list:
    """Resolve board IDs from BrainFlow enum, falling back to known ints."""
    try:
        from brainflow.board_shim import BoardIds
        ids = []
        # Try Athena-specific board first (newer firmware).
        # NOTE: the correct BrainFlow enum name is MUSE_S_ATHENA_BOARD.
        # Keep the legacy misspelling as a fallback for older BrainFlow versions.
        if hasattr(BoardIds, "MUSE_S_ATHENA_BOARD"):
            ids.append(BoardIds.MUSE_S_ATHENA_BOARD)
        elif hasattr(BoardIds, "MUSE_S_ANTHENA_BOARD"):
            ids.append(BoardIds.MUSE_S_ANTHENA_BOARD)
        # Fallback to standard Muse S
        ids.append(BoardIds.MUSE_S_BOARD)
        return ids
    except ImportError:
        return [22, 39]  # best-effort fallback

MUSE_BOARD_IDS = _resolve_board_ids()
MIN_SAMPLES: int = 256              # Minimum samples required for PSD calculation

# --- EEG Band Definitions (Hz) ---
BAND_DELTA = (1.0, 4.0)
BAND_THETA = (4.0, 8.0)
BAND_ALPHA = (8.0, 13.0)
BAND_BETA  = (13.0, 30.0)
BAND_GAMMA = (30.0, 50.0)

# --- Bandpass Filter ---
# NOTE: BrainFlow 5.x changed perform_bandpass to take (start_freq, stop_freq)
# instead of the legacy (center_freq, bandwidth). Passing the legacy values to
# the new API yielded a 25.5-49 Hz passband (only high beta + gamma survived),
# which made the focus ratio beta/(alpha+theta) explode and clamp to 1.0.
BANDPASS_START_FREQ: float = 1.0
BANDPASS_STOP_FREQ: float = 50.0
BANDPASS_ORDER: int = 4

# --- PSD (Welch) ---
PSD_NFFT: int = 256
PSD_OVERLAP: int = 128

# --- Metric Normalization ---
FOCUS_DIVISOR: float = 2.0          # beta/(alpha+theta) is divided by this, then clamped to 0–1
RELAX_DIVISOR: float = 2.0          # alpha/(beta+gamma) is divided by this, then clamped to 0–1

# --- HueShift Color Mapping (HSV Hue in degrees, 0–360) ---
# Three anchor hues drive a piecewise interpolation on the focus/relax mix:
#   relax_weight 0.0 (pure focus)   -> HUE_FOCUS   = red
#   relax_weight 0.5 (balanced)     -> HUE_BALANCE = blue
#   relax_weight 1.0 (pure relax)   -> HUE_RELAX   = green
HUE_FOCUS: float = 0.0             # Red when focused (relax_weight = 0)
HUE_BALANCE: float = 240.0         # Blue when balanced (relax_weight = 0.5)
HUE_RELAX: float = 120.0           # Green when relaxed (relax_weight = 1)
HUE_DISCONNECTED: float = -1.0     # Sentinel: use grey when disconnected

# --- Overlay Visual ---
GRADIENT_SATURATION: float = 0.85
GRADIENT_VALUE: float = 0.95
MAX_ALPHA: int = 200                # Maximum alpha at the screen edge (0–255)

# --- Transparent Color (chroma key) ---
# This exact color will be rendered invisible by Tkinter.
# Use a color that will never appear in the gradient.
TRANSPARENT_COLOR: str = "#010101"
