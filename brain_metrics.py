"""
Real-time EEG metric calculation for Muse S.

Computes *focus* and *relaxation* scores from raw EEG channel data
using spectral band-power ratios.
"""

import logging
from typing import Any, Dict, Optional

import numpy as np
from brainflow.board_shim import BoardShim
from brainflow.data_filter import DataFilter, FilterTypes, WindowOperations

import config

logger = logging.getLogger(__name__)


def calculate_metrics(data: np.ndarray, board_id: int) -> Optional[Dict[str, Any]]:
    """Compute focus / relaxation metrics from a raw board-data matrix.

    Parameters
    ----------
    data : np.ndarray
        2-D array returned by ``BoardShim.get_board_data()``
        (rows = channels, columns = samples).
    board_id : int
        The BrainFlow board ID used to connect (needed to look up
        sampling rate and EEG channel indices).

    Returns
    -------
    dict or None
        ``{"focus": float, "relaxation": float, "band_powers": dict}``
        where focus and relaxation are in ``[0, 1]``.
        Returns *None* when there are too few samples.
    """
    sampling_rate = BoardShim.get_sampling_rate(board_id)
    eeg_channels = BoardShim.get_eeg_channels(board_id)

    if data.shape[1] < config.MIN_SAMPLES:
        return None

    band_powers_list: list[Dict[str, float]] = []

    for channel in eeg_channels:
        # Copy so we don't mutate the original array
        channel_data = data[channel].copy()

        # Band-pass filter 1-50 Hz (Butterworth order 4).
        # BrainFlow 5.x signature: (data, fs, start_freq, stop_freq, order, type, ripple)
        DataFilter.perform_bandpass(
            channel_data,
            sampling_rate,
            config.BANDPASS_START_FREQ,
            config.BANDPASS_STOP_FREQ,
            config.BANDPASS_ORDER,
            FilterTypes.BUTTERWORTH,
            0,  # ripple – unused for Butterworth
        )

        # Power Spectral Density via Welch's method
        psd = DataFilter.get_psd_welch(
            channel_data,
            nfft=config.PSD_NFFT,
            overlap=config.PSD_OVERLAP,
            sampling_rate=sampling_rate,
            window=WindowOperations.HANNING,
        )

        # Extract band powers
        delta = DataFilter.get_band_power(psd, *config.BAND_DELTA)
        theta = DataFilter.get_band_power(psd, *config.BAND_THETA)
        alpha = DataFilter.get_band_power(psd, *config.BAND_ALPHA)
        beta  = DataFilter.get_band_power(psd, *config.BAND_BETA)
        gamma = DataFilter.get_band_power(psd, *config.BAND_GAMMA)

        band_powers_list.append({
            "delta": delta,
            "theta": theta,
            "alpha": alpha,
            "beta":  beta,
            "gamma": gamma,
        })

    # Average band powers across all EEG channels
    avg_powers: Dict[str, float] = {}
    for band in ("delta", "theta", "alpha", "beta", "gamma"):
        avg_powers[band] = float(np.mean([bp[band] for bp in band_powers_list]))

    total_power = sum(avg_powers.values())
    if total_power == 0:
        return None

    # Focus: beta / (alpha + theta), normalised to 0-1
    denom_focus = avg_powers["alpha"] + avg_powers["theta"]
    focus_ratio = avg_powers["beta"] / denom_focus if denom_focus > 0 else 0.0
    focus = min(1.0, focus_ratio / config.FOCUS_DIVISOR)

    # Relaxation: alpha / (beta + gamma), normalised to 0-1
    denom_relax = avg_powers["beta"] + avg_powers["gamma"]
    relax_ratio = avg_powers["alpha"] / denom_relax if denom_relax > 0 else 0.0
    relaxation = min(1.0, relax_ratio / config.RELAX_DIVISOR)

    logger.debug(
        "bands d=%.2f t=%.2f a=%.2f b=%.2f g=%.2f -> focus=%.3f relax=%.3f",
        avg_powers["delta"], avg_powers["theta"], avg_powers["alpha"],
        avg_powers["beta"], avg_powers["gamma"], focus, relaxation,
    )

    return {
        "focus": round(focus, 3),
        "relaxation": round(relaxation, 3),
        "band_powers": avg_powers,
    }
