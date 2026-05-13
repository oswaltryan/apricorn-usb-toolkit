# src/usb_tool/utils.py

from __future__ import annotations

import math

OOB_MODE_SIZE_BYTE_VALUES = (512, 500_000, 500 * 1024)
OOB_MODE_SIZE_GB_VALUES = tuple(size / (1024**3) for size in OOB_MODE_SIZE_BYTE_VALUES)


def bytes_to_gb(bytes_value: float) -> float:
    if not isinstance(bytes_value, (int, float)) or bytes_value <= 0:
        return 0.0
    return bytes_value / (1024**3)


def is_oob_mode_size_bytes(bytes_value: object) -> bool:
    if not isinstance(bytes_value, (int, float)) or isinstance(bytes_value, bool):
        return False
    return any(float(bytes_value) == float(size) for size in OOB_MODE_SIZE_BYTE_VALUES)


def is_oob_mode_size_gb(size_gb: object) -> bool:
    if not isinstance(size_gb, (int, float)) or isinstance(size_gb, bool):
        return False
    return any(
        math.isclose(float(size_gb), sentinel, rel_tol=0.0, abs_tol=1e-12)
        for sentinel in OOB_MODE_SIZE_GB_VALUES
    )


def find_closest(target: float, options: list[int]) -> int | None:
    if not isinstance(target, (int, float)) or target <= 0 or not options:
        return None
    try:
        numeric_options = [opt for opt in options if isinstance(opt, (int, float))]
        if not numeric_options:
            return None
        return min(numeric_options, key=lambda x: abs(x - target))
    except (TypeError, ValueError):
        return None


def parse_usb_version(bcd: int) -> str:
    major = (bcd & 0xFF00) >> 8
    minor = (bcd & 0x00F0) >> 4
    subminor = bcd & 0x000F
    return f"{major}.{minor}{subminor}" if subminor else f"{major}.{minor}"
