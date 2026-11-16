"""Optional DirectShow audio device enumeration and safe resolution helpers."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import subprocess
import sys

from quickclip.paths import resolve_ffmpeg_path

logger = logging.getLogger(__name__)

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

_INLINE_DEVICE_RE = re.compile(r'"([^"]+)"\s*\((audio|video)\)\s*$', re.IGNORECASE)
_ALT_NAME_RE = re.compile(r'Alternative name\s+"([^"]+)"', re.IGNORECASE)
_QUOTED_NAME_RE = re.compile(r'"([^"]+)"')


@dataclass(frozen=True)
class AudioDevice:
    """Descriptor for an audio capture device discovered via FFmpeg DirectShow."""

    name: str
    alternative_name: str | None = None

    @property
    def dshow_spec(self) -> str:
        """Return the ``audio=...`` input string accepted by FFmpeg ``-f dshow``."""
        target = self.alternative_name or self.name
        return f"audio={target}"


def parse_dshow_audio_devices(output: str) -> list[AudioDevice]:
    """Parse DirectShow audio devices from FFmpeg ``-list_devices true`` output.

    Supports both FFmpeg 5+/9.x inline ``"Name" (audio)`` lines and legacy
    section-based ``DirectShow audio devices`` output.
    """
    if not output:
        return []

    devices: list[AudioDevice] = []
    seen_keys: set[str] = set()
    in_audio_section = False
    pending_name: str | None = None
    pending_alt: str | None = None

    def _flush_pending() -> None:
        nonlocal pending_name, pending_alt
        if pending_name is not None:
            key = pending_alt or pending_name
            if key not in seen_keys:
                seen_keys.add(key)
                devices.append(AudioDevice(name=pending_name, alternative_name=pending_alt))
        pending_name = None
        pending_alt = None

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        lower = line.lower()
        if "directshow audio devices" in lower:
            _flush_pending()
            in_audio_section = True
            continue
        if "directshow video devices" in lower or "error opening input" in lower:
            _flush_pending()
            in_audio_section = False
            continue

        alt_match = _ALT_NAME_RE.search(line)
        if alt_match:
            if pending_name is not None:
                pending_alt = alt_match.group(1)
                _flush_pending()
            continue

        inline_match = _INLINE_DEVICE_RE.search(line)
        if inline_match:
            _flush_pending()
            dev_name, dev_kind = inline_match.group(1), inline_match.group(2).lower()
            if dev_kind == "audio":
                pending_name = dev_name
            continue

        if in_audio_section and "could not enumerate" not in lower:
            quoted = _QUOTED_NAME_RE.search(line)
            if quoted:
                _flush_pending()
                pending_name = quoted.group(1)

    _flush_pending()
    return devices


def list_audio_devices(
    ffmpeg_path: str = "ffmpeg",
    timeout_sec: float = 5.0,
) -> list[AudioDevice]:
    """Enumerate available DirectShow audio devices using FFmpeg.

    Returns an empty list if enumeration fails or no audio devices exist.
    """
    resolved_ffmpeg = resolve_ffmpeg_path(ffmpeg_path)
    cmd = [
        resolved_ffmpeg,
        "-hide_banner",
        "-f",
        "dshow",
        "-list_devices",
        "true",
        "-i",
        "dummy",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("DirectShow audio enumeration failed: %s", exc)
        return []

    combined = "\n".join(part for part in (proc.stderr, proc.stdout) if part)
    return parse_dshow_audio_devices(combined)


def probe_audio_device(
    device_spec: str,
    ffmpeg_path: str = "ffmpeg",
    timeout_sec: float = 3.0,
) -> bool:
    """Verify via a short FFmpeg dry-run that ``device_spec`` can be opened."""
    resolved_ffmpeg = resolve_ffmpeg_path(ffmpeg_path)
    normalized = device_spec if device_spec.startswith("audio=") else f"audio={device_spec}"
    cmd = [
        resolved_ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "dshow",
        "-i",
        normalized,
        "-t",
        "0.05",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            creationflags=_CREATE_NO_WINDOW,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Audio device probe failed for %r: %s", normalized, exc)
        return False


def resolve_audio_input(
    capture_audio: bool,
    audio_device: str | None = None,
    ffmpeg_path: str = "ffmpeg",
    *,
    validate: bool = True,
) -> str | None:
    """Resolve an FFmpeg DirectShow ``audio=...`` input string, or ``None`` if unavailable.

    Gracefully returns ``None`` (degrading to video-only ``has_audio=False``) when
    ``capture_audio`` is False, no devices are available, or the requested device
    cannot be resolved or opened.
    """
    if not capture_audio:
        return None

    resolved_ffmpeg = resolve_ffmpeg_path(ffmpeg_path)
    devices = list_audio_devices(ffmpeg_path=resolved_ffmpeg)
    requested = audio_device.strip() if isinstance(audio_device, str) else ""

    if requested and requested.lower() not in {"auto", "default"}:
        lookup = requested[len("audio=") :] if requested.startswith("audio=") else requested
        lookup_lower = lookup.lower()

        matched_spec: str | None = None
        for dev in devices:
            if (
                dev.name.lower() == lookup_lower
                or (dev.alternative_name and dev.alternative_name.lower() == lookup_lower)
            ):
                matched_spec = dev.dshow_spec
                break

        if matched_spec is None:
            # If enumeration returned devices and none matched, still try validating
            # an explicit spec only when requested directly; otherwise degrade safely.
            candidate_spec = f"audio={lookup}"
            if validate and probe_audio_device(candidate_spec, ffmpeg_path=resolved_ffmpeg):
                return candidate_spec
            logger.warning(
                "Configured audio device %r was not found or is unusable; "
                "falling back to video-only capture.",
                audio_device,
            )
            return None

        if not validate or probe_audio_device(matched_spec, ffmpeg_path=resolved_ffmpeg):
            return matched_spec

        logger.warning(
            "Audio device %r failed FFmpeg dry-run probe; falling back to video-only.",
            matched_spec,
        )
        return None

    if not devices:
        logger.info("No DirectShow audio devices found; falling back to video-only capture.")
        return None

    for dev in devices:
        spec = dev.dshow_spec
        if not validate or probe_audio_device(spec, ffmpeg_path=resolved_ffmpeg):
            return spec

    logger.warning(
        "Enumerated %d audio device(s) but none could be opened; falling back to video-only.",
        len(devices),
    )
    return None
