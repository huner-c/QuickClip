"""Path resolution helpers for bundled binaries and packaged resources."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys

_BARE_MEDIA_BINARIES = frozenset({"ffmpeg", "ffmpeg.exe", "ffprobe", "ffprobe.exe"})


def _repo_root() -> Path:
    """Return the QuickClip repository root directory in source checkouts."""
    return Path(__file__).resolve().parents[2]


def _normalized_exe_name(bare_name: str) -> str:
    stem = "ffmpeg" if bare_name.lower().startswith("ffmpeg") else "ffprobe"
    return f"{stem}.exe" if sys.platform == "win32" else stem


def resolve_binary_path(executable: str) -> str:
    """Resolve bare ``ffmpeg``/``ffprobe`` executables across frozen and dev layouts.

    Leaves explicit paths (e.g. ``C:\\tools\\ffmpeg.exe``) or custom executable
    names (e.g. ``custom_ffmpeg``) untouched.
    """
    trimmed = executable.strip()
    if trimmed.lower() not in _BARE_MEDIA_BINARIES:
        return executable

    exe_name = _normalized_exe_name(trimmed)
    candidates: list[Path] = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        meipass_dir = Path(meipass)
        candidates.append(meipass_dir / "bin" / exe_name)
        candidates.append(meipass_dir / exe_name)

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / "bin" / exe_name)
        candidates.append(exe_dir / exe_name)
        candidates.append(exe_dir / "_internal" / "bin" / exe_name)

    candidates.append(_repo_root() / "bin" / exe_name)

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    return shutil.which(trimmed) or executable


def resolve_ffmpeg_path(executable: str = "ffmpeg") -> str:
    """Resolve the ``ffmpeg`` binary path for frozen bundles, dev ``bin/``, or ``PATH``."""
    return resolve_binary_path(executable)


def resolve_ffprobe_path(executable: str = "ffprobe") -> str:
    """Resolve the ``ffprobe`` binary path for frozen bundles, dev ``bin/``, or ``PATH``."""
    return resolve_binary_path(executable)


def resolve_resource_path(relative_path: str | Path) -> Path:
    """Locate a bundled asset/resource across ``_MEIPASS``, frozen dir, and repo root."""
    rel = Path(relative_path)
    if rel.is_absolute():
        return rel

    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / rel)

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / rel)
        candidates.append(exe_dir / "_internal" / rel)

    repo_candidate = _repo_root() / rel
    candidates.append(repo_candidate)

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return candidates[0] if candidates else repo_candidate


__all__ = [
    "resolve_binary_path",
    "resolve_ffmpeg_path",
    "resolve_ffprobe_path",
    "resolve_resource_path",
]
