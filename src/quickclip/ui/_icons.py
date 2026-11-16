"""Shared icon loading helpers for QuickClip UI windows and tray."""

from __future__ import annotations

from PySide6.QtGui import QIcon

from quickclip.paths import resolve_resource_path


def load_app_icon() -> QIcon:
    """Load the bundled QuickClip ``.ico`` asset, or return an empty ``QIcon`` if missing."""
    icon_path = resolve_resource_path("assets/quickclip.ico")
    if icon_path.is_file():
        icon = QIcon(str(icon_path))
        if not icon.isNull():
            return icon
    return QIcon()
