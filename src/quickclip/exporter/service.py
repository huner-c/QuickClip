"""Lossless FFmpeg clip trimming and export service for QuickClip."""

from __future__ import annotations

from datetime import datetime
import json
import math
from pathlib import Path
import re
import subprocess
import time
from typing import Any

from quickclip.contracts import TrimRequest, TrimResult
from quickclip.paths import resolve_ffmpeg_path, resolve_ffprobe_path

_SUBPROCESS_CREATION_FLAGS: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_HMS_DURATION_RE = re.compile(
    r"^\s*(?P<hours>\d+):(?P<minutes>\d{1,2}):(?P<seconds>\d{1,2}(?:\.\d+)?)\s*$"
)


def _format_timestamp(seconds: float) -> str:
    """Format a non-negative timestamp in seconds without scientific notation."""
    text = f"{seconds:.6f}".rstrip("0").rstrip(".")
    return text if text else "0"


def _parse_float_duration(raw: Any) -> float | None:
    """Parse a finite non-negative float duration value, or return None."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0.0:
        return None
    return value


def _parse_hms_duration(raw: Any) -> float | None:
    """Parse an ``HH:MM:SS[.ffffff]`` tag duration string, or return None."""
    if not isinstance(raw, str):
        return None
    match = _HMS_DURATION_RE.match(raw)
    if match is None:
        return None
    hours = int(match.group("hours"))
    minutes = int(match.group("minutes"))
    seconds = float(match.group("seconds"))
    total = hours * 3600.0 + minutes * 60.0 + seconds
    if not math.isfinite(total) or total < 0.0:
        return None
    return total


class ExporterService:
    """Stream-copy FFmpeg exporter satisfying :class:`~quickclip.contracts.IExporterService`.

    Verified on FFmpeg 9.0.2:
    - Trimming places **both** ``-ss`` and ``-to`` **before** ``-i`` with ``-c copy``
      and ``-movflags +faststart``:
      ``ffmpeg -y -ss <start> -to <end> -i <source> -c copy -movflags +faststart <output>``.
      Because both ``-ss`` and ``-to`` are input options before ``-i``, FFmpeg seeks
      to ``<start>`` and stops reading at input timestamp ``<end>``, producing a clip
      of duration ``<end> - <start>`` (within keyframe tolerance) rather than ``<end>``.
    - Direct save remuxes without trimming or re-encoding:
      ``ffmpeg -y -i <source> -c copy -movflags +faststart <output>``.
    """

    _FFMPEG_TIMEOUT_SEC: float = 120.0
    _FFPROBE_TIMEOUT_SEC: float = 30.0
    _CLEANUP_RETRIES: int = 10
    _CLEANUP_RETRY_DELAY_SEC: float = 0.05

    def __init__(
        self,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
    ) -> None:
        """Initialize the exporter with executable paths for FFmpeg and FFprobe."""
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path

    @property
    def resolved_ffmpeg_path(self) -> str:
        """Return the resolved FFmpeg executable path, prioritizing bundled binaries."""
        return resolve_ffmpeg_path(self.ffmpeg_path)

    @property
    def resolved_ffprobe_path(self) -> str:
        """Return the resolved FFprobe executable path, prioritizing bundled binaries."""
        return resolve_ffprobe_path(self.ffprobe_path)

    def probe_duration(self, path: Path) -> float:
        """Read the media duration in seconds using ``ffprobe``.

        Args:
            path: Path to an existing media file (e.g. ``.mkv`` or ``.mp4``).

        Returns:
            Clip duration in seconds.

        Raises:
            FileNotFoundError: If ``path`` does not exist or ``ffprobe`` cannot be launched.
            RuntimeError: If ``ffprobe`` fails or returns no valid duration metadata.
        """
        target = Path(path)
        if not target.is_file():
            raise FileNotFoundError(f"Media file does not exist: {target}")

        cmd = [
            self.resolved_ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=duration:stream_tags=DURATION",
            "-of",
            "json",
            str(target),
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            creationflags=_SUBPROCESS_CREATION_FLAGS,
            timeout=self._FFPROBE_TIMEOUT_SEC,
            check=False,
        )
        if proc.returncode != 0:
            stderr_msg = proc.stderr.strip() or f"ffprobe exited with code {proc.returncode}"
            raise RuntimeError(f"ffprobe failed for '{target}': {stderr_msg}")

        try:
            payload = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid ffprobe JSON output for '{target}': {exc}") from exc

        format_section = payload.get("format") or {}
        duration = _parse_float_duration(format_section.get("duration"))
        if duration is not None:
            return duration

        streams = payload.get("streams") or []
        stream_durations: list[float] = []
        for stream in streams:
            parsed = _parse_float_duration(stream.get("duration"))
            if parsed is not None:
                stream_durations.append(parsed)
                continue
            tags = stream.get("tags") or {}
            tag_duration = _parse_hms_duration(tags.get("DURATION") or tags.get("duration"))
            if tag_duration is not None:
                stream_durations.append(tag_duration)

        if stream_durations:
            return max(stream_durations)

        raise RuntimeError(f"Could not determine media duration for '{target}'")

    def build_output_path(
        self,
        output_dir: Path,
        prefix: str = "QuickClip",
        now: datetime | None = None,
    ) -> Path:
        """Build a non-colliding timestamped MP4 output path in ``output_dir``.

        Format is ``{prefix}_{YYYY-MM-DD_HH-MM-SS}.mp4``. When that file already exists,
        ``_{counter:02d}`` (``_01``, ``_02``, ...) is appended before ``.mp4``.
        """
        target_dir = Path(output_dir)
        clean_prefix = prefix.strip() or "QuickClip"
        current_time = now if now is not None else datetime.now()
        timestamp = current_time.strftime("%Y-%m-%d_%H-%M-%S")

        candidate = target_dir / f"{clean_prefix}_{timestamp}.mp4"
        if not candidate.exists():
            return candidate

        counter = 1
        while True:
            numbered = target_dir / f"{clean_prefix}_{timestamp}_{counter:02d}.mp4"
            if not numbered.exists():
                return numbered
            counter += 1

    def export(self, request: TrimRequest) -> TrimResult:
        """Trim or directly remux a recorded clip using lossless stream copy.

        Never re-encodes and never deletes ``request.source_path`` on validation or
        FFmpeg failure.
        """
        start_perf = time.perf_counter()
        cmd: list[str] = []
        output_path: Path | None = None

        try:
            request.validate()
            request.output_dir.mkdir(parents=True, exist_ok=True)
            output_path = self.build_output_path(
                request.output_dir,
                prefix=request.filename_prefix,
            )
            cmd = self._build_export_command(request, output_path)

            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                creationflags=_SUBPROCESS_CREATION_FLAGS,
                timeout=self._FFMPEG_TIMEOUT_SEC,
                check=False,
            )
            if proc.returncode != 0:
                self._remove_partial_output(output_path, request.source_path)
                stderr_msg = (
                    proc.stderr.strip() or f"ffmpeg exited with code {proc.returncode}"
                )
                return TrimResult(
                    success=False,
                    output_path=None,
                    duration_sec=0.0,
                    elapsed_ms=(time.perf_counter() - start_perf) * 1000.0,
                    source_cleaned=False,
                    command_executed=cmd,
                    error=stderr_msg,
                )

            if not output_path.is_file() or output_path.stat().st_size <= 0:
                self._remove_partial_output(output_path, request.source_path)
                return TrimResult(
                    success=False,
                    output_path=None,
                    duration_sec=0.0,
                    elapsed_ms=(time.perf_counter() - start_perf) * 1000.0,
                    source_cleaned=False,
                    command_executed=cmd,
                    error=f"FFmpeg produced an empty or missing output file: {output_path}",
                )

            duration_sec = self.probe_duration(output_path)
        except Exception as exc:
            if output_path is not None:
                self._remove_partial_output(output_path, request.source_path)
            return TrimResult(
                success=False,
                output_path=None,
                duration_sec=0.0,
                elapsed_ms=(time.perf_counter() - start_perf) * 1000.0,
                source_cleaned=False,
                command_executed=cmd,
                error=str(exc),
            )

        source_cleaned = (
            self.cleanup_temp(request.source_path) if request.cleanup_source else False
        )
        elapsed_ms = (time.perf_counter() - start_perf) * 1000.0
        return TrimResult(
            success=True,
            output_path=output_path,
            duration_sec=duration_sec,
            elapsed_ms=elapsed_ms,
            source_cleaned=source_cleaned,
            command_executed=cmd,
        )

    def cleanup_temp(self, path: Path) -> bool:
        """Safely delete a temporary file with brief retries for Windows handle release.

        Returns:
            True if the file was deleted (or vanished during retry), False if it did
            not exist, was not a regular file, or remained locked after retries.
        """
        target = Path(path)
        if not target.is_file():
            return False

        for attempt in range(self._CLEANUP_RETRIES):
            try:
                target.unlink()
                return True
            except FileNotFoundError:
                return True
            except OSError:
                if attempt + 1 < self._CLEANUP_RETRIES:
                    time.sleep(self._CLEANUP_RETRY_DELAY_SEC)

        return False

    def _build_export_command(self, request: TrimRequest, output_path: Path) -> list[str]:
        """Construct the lossless ``-c copy`` FFmpeg argument list for ``request``."""
        ffmpeg_exe = self.resolved_ffmpeg_path
        if request.direct_save:
            return [
                ffmpeg_exe,
                "-y",
                "-i",
                str(request.source_path),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(output_path),
            ]

        assert request.end_time_sec is not None
        # Both -ss and -to must precede -i so -to is interpreted in the input timeline
        # (yielding duration ~= end - start rather than duration ~= end).
        return [
            ffmpeg_exe,
            "-y",
            "-ss",
            _format_timestamp(request.start_time_sec),
            "-to",
            _format_timestamp(request.end_time_sec),
            "-i",
            str(request.source_path),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
        ]

    @staticmethod
    def _remove_partial_output(output_path: Path, source_path: Path) -> None:
        """Remove an incomplete output file without ever touching ``source_path``."""
        try:
            if output_path.resolve() == source_path.resolve():
                return
        except OSError:
            if output_path == source_path:
                return
        try:
            if output_path.is_file():
                output_path.unlink()
        except OSError:
            pass


__all__ = ["ExporterService"]
