"""Lightweight persistent user settings storage for QuickClip."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class UserSettings:
    """User-configurable preferences persisted across sessions."""

    hotkey: str | None = None
    output_dir: str | None = None
    monitor_index: int | None = None
    region_mode: bool | None = None


def default_settings_path() -> Path:
    """Return the default per-user JSON settings path."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "QuickClip" / "settings.json"
    return Path.home() / ".config" / "quickclip" / "settings.json"


def load_user_settings(path: Path | None = None) -> UserSettings:
    """Load persisted settings from ``path`` (or return defaults if absent/invalid)."""
    target = Path(path) if path is not None else default_settings_path()
    if not target.is_file():
        return UserSettings()

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.debug("Failed loading QuickClip settings from %s.", target, exc_info=True)
        return UserSettings()

    if not isinstance(raw, dict):
        return UserSettings()

    hotkey = raw.get("hotkey")
    output_dir = raw.get("output_dir")
    monitor_index = raw.get("monitor_index")
    region_mode = raw.get("region_mode")

    parsed_hotkey = hotkey.strip() if isinstance(hotkey, str) and hotkey.strip() else None
    parsed_output = output_dir.strip() if isinstance(output_dir, str) and output_dir.strip() else None
    parsed_monitor = (
        int(monitor_index)
        if isinstance(monitor_index, int) and not isinstance(monitor_index, bool) and monitor_index >= 0
        else None
    )
    parsed_region_mode = region_mode if isinstance(region_mode, bool) else None

    return UserSettings(
        hotkey=parsed_hotkey,
        output_dir=parsed_output,
        monitor_index=parsed_monitor,
        region_mode=parsed_region_mode,
    )


def save_user_settings(settings: UserSettings, path: Path | None = None) -> bool:
    """Persist ``settings`` to ``path`` as JSON, returning True on success."""
    target = Path(path) if path is not None else default_settings_path()
    payload: dict[str, object] = {}
    if settings.hotkey:
        payload["hotkey"] = settings.hotkey
    if settings.output_dir:
        payload["output_dir"] = settings.output_dir
    if settings.monitor_index is not None:
        payload["monitor_index"] = int(settings.monitor_index)
    if settings.region_mode is not None:
        payload["region_mode"] = bool(settings.region_mode)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(target)
        return True
    except OSError:
        logger.warning("Failed saving QuickClip settings to %s.", target, exc_info=True)
        return False


__all__ = [
    "UserSettings",
    "default_settings_path",
    "load_user_settings",
    "save_user_settings",
]
