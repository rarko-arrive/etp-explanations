"""Backward-compatible re-exports for CLHP path upgrades."""

from __future__ import annotations

from dqt.etp_slider.movement.feature_path import (
    CLHP_PATH_EPS,
    PATH_MARK_HRS,
    apply_feature_path_upgrades,
    detect_feature_path_flags,
)

# Legacy name used in tests and drift report imports.
detect_clhp_path_change_ind = detect_feature_path_flags
apply_clhp_path_upgrades = apply_feature_path_upgrades


__all__ = [
    "CLHP_PATH_EPS",
    "PATH_MARK_HRS",
    "apply_clhp_path_upgrades",
    "detect_clhp_path_change_ind",
]
