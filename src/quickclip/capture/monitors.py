"""Display monitor enumeration and geometry helpers for QuickClip capture."""

from __future__ import annotations

import logging
from typing import Any

from quickclip.contracts import MonitorInfo

logger = logging.getLogger(__name__)


def _normalize_device_name(raw_name: Any, index: int) -> str:
    text = str(raw_name).strip() if raw_name is not None else ""
    if text.startswith("\\\\.\\"):
        text = text[4:]
    if not text or text.lower() in {"generic pnp monitor", "generic non-pnp monitor"}:
        return ""
    return text


def list_monitors() -> list[MonitorInfo]:
    """Enumerate individual physical monitors using MSS (1-based indices).

    Returns at least one fallback entry only if displays are present; returns an
    empty list if MSS enumeration fails.
    """
    try:
        import mss
    except Exception:
        logger.debug("MSS unavailable during monitor enumeration.", exc_info=True)
        return []

    try:
        with mss.MSS() as sct:
            raw_monitors = list(sct.monitors)
    except Exception:
        logger.warning("Failed to enumerate monitors via MSS.", exc_info=True)
        return []

    if len(raw_monitors) <= 1:
        return []

    monitors: list[MonitorInfo] = []
    for idx, mon in enumerate(raw_monitors[1:], start=1):
        width = int(mon.get("width", 0) or 0)
        height = int(mon.get("height", 0) or 0)
        if width <= 0 or height <= 0:
            continue
        left = int(mon.get("left", 0) or 0)
        top = int(mon.get("top", 0) or 0)
        is_primary = bool(mon.get("is_primary", idx == 1 and left == 0 and top == 0))
        name = _normalize_device_name(mon.get("name"), idx)
        monitors.append(
            MonitorInfo(
                index=idx,
                name=name,
                width=width,
                height=height,
                left=left,
                top=top,
                is_primary=is_primary,
            )
        )
    return monitors


def resolve_monitor_rect(monitor_index: int) -> tuple[int, int, int, int] | None:
    """Return ``(left, top, width, height)`` for 1-based ``monitor_index`` via MSS."""
    if monitor_index < 0:
        return None
    try:
        import mss

        with mss.MSS() as sct:
            monitors = list(sct.monitors)
            if not monitors:
                return None
            if monitor_index == 0:
                chosen = monitors[0]
            elif 1 <= monitor_index < len(monitors):
                chosen = monitors[monitor_index]
            else:
                return None
            return (
                int(chosen["left"]),
                int(chosen["top"]),
                int(chosen["width"]),
                int(chosen["height"]),
            )
    except Exception:
        logger.debug("Failed resolving monitor rect for index %d.", monitor_index, exc_info=True)
        return None


__all__ = ["list_monitors", "resolve_monitor_rect"]
