"""Windows Run-at-Startup registry helpers for QuickClip."""

from __future__ import annotations

import logging
from pathlib import Path
import sys

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows fallback
    winreg = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


def default_startup_command() -> str:
    """Return the quoted command line registered for launching QuickClip at sign-in."""
    exe_path = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False):
        return f'"{exe_path}"'

    pythonw = exe_path.with_name("pythonw.exe")
    interpreter = pythonw if pythonw.is_file() else exe_path
    main_script = Path(__file__).resolve().parents[2] / "main.py"
    return f'"{interpreter}" "{main_script}"'


def is_run_at_startup_enabled(app_name: str = "QuickClip") -> bool:
    """Return True if ``app_name`` has a non-empty entry in HKCU Run."""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, app_name)
            return isinstance(value, str) and bool(value.strip())
    except FileNotFoundError:
        return False
    except OSError:
        logger.debug("Failed reading startup registry key for %s.", app_name, exc_info=True)
        return False


def set_run_at_startup(
    enabled: bool,
    *,
    command: str | None = None,
    app_name: str = "QuickClip",
) -> bool:
    """Enable or disable QuickClip auto-start under HKCU Run.

    Returns:
        True when the requested registry change succeeded (or entry was already absent
        when ``enabled=False``); False if ``winreg`` is unavailable or an ``OSError``
        occurred.
    """
    if winreg is None:
        return False

    if enabled:
        cmd = (command if command is not None else default_startup_command()).strip()
        if not cmd:
            raise ValueError("Startup command must be a non-empty string when enabled=True.")
        try:
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER,
                RUN_KEY_PATH,
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, cmd)
            return True
        except OSError:
            logger.warning("Failed enabling startup registry key for %s.", app_name, exc_info=True)
            return False

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            RUN_KEY_PATH,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            try:
                winreg.DeleteValue(key, app_name)
            except FileNotFoundError:
                pass
        return True
    except FileNotFoundError:
        return True
    except OSError:
        logger.warning("Failed disabling startup registry key for %s.", app_name, exc_info=True)
        return False


__all__ = [
    "RUN_KEY_PATH",
    "default_startup_command",
    "is_run_at_startup_enabled",
    "set_run_at_startup",
]
