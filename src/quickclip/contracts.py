"""Inter-module data contracts and service protocols for QuickClip."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import math
from pathlib import Path
import tempfile
from typing import Protocol, runtime_checkable


def _default_temp_dir() -> Path:
    """Return the default temporary directory for raw capture files."""
    return Path(tempfile.gettempdir()) / "quickclip"


def _default_output_dir() -> Path:
    """Return the default destination directory for exported clips."""
    return Path.home() / "Videos" / "QuickClip"


class CaptureState(str, Enum):
    """Lifecycle states for a pre-warmed screen capture engine."""

    COLD = "cold"
    WARMING = "warming"
    STANDBY = "standby"
    RECORDING = "recording"
    STOPPING = "stopping"
    ERROR = "error"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class MonitorInfo:
    """Metadata describing a captureable display monitor."""

    index: int
    name: str
    width: int
    height: int
    left: int = 0
    top: int = 0
    is_primary: bool = False

    @property
    def label(self) -> str:
        """Return a concise UI-friendly label describing this monitor."""
        clean_name = self.name.strip()
        prefix = f"Ekran {self.index} • {clean_name}" if clean_name else f"Ekran {self.index}"
        dims = f"{self.width}x{self.height}"
        suffix = f"{dims} • Birincil" if self.is_primary else dims
        return f"{prefix} ({suffix})"


@dataclass(frozen=True)
class CaptureRegion:
    """Monitor-relative capture rectangle in physical pixels."""

    left: int
    top: int
    width: int
    height: int
    monitor_index: int = 1

    def __post_init__(self) -> None:
        if self.left < 0 or self.top < 0:
            raise ValueError(
                f"CaptureRegion origin must be non-negative, got ({self.left}, {self.top})"
            )
        if self.width <= 0 or self.height <= 0:
            raise ValueError(
                f"CaptureRegion dimensions must be positive, got {self.width}x{self.height}"
            )
        if self.monitor_index < 0:
            raise ValueError(
                f"CaptureRegion monitor_index must be >= 0, got {self.monitor_index}"
            )

    @property
    def right(self) -> int:
        """Return monitor-relative right edge in physical pixels."""
        return self.left + self.width

    @property
    def bottom(self) -> int:
        """Return monitor-relative bottom edge in physical pixels."""
        return self.top + self.height

    def normalized(self, monitor_width: int, monitor_height: int) -> CaptureRegion:
        """Clamp region within monitor bounds with even width/height for H.264."""
        if monitor_width < 2 or monitor_height < 2:
            raise ValueError(
                f"Invalid monitor dimensions for normalization: {monitor_width}x{monitor_height}"
            )
        left = max(0, min(int(self.left), monitor_width - 2))
        top = max(0, min(int(self.top), monitor_height - 2))
        max_w = monitor_width - left
        max_h = monitor_height - top
        width = max(2, min(int(self.width), max_w))
        height = max(2, min(int(self.height), max_h))
        # Ensure even dimensions for yuv420p H.264 compatibility.
        width = max(2, (width // 2) * 2)
        height = max(2, (height // 2) * 2)
        return CaptureRegion(
            left=left,
            top=top,
            width=width,
            height=height,
            monitor_index=self.monitor_index,
        )


@dataclass
class CaptureConfig:
    """Configuration options controlling capture and pre-warming behavior."""

    temp_dir: Path = field(default_factory=_default_temp_dir)
    temp_filename: str = "temp_raw.mkv"
    hotkey: str = "F9"
    fps: int = 60
    capture_audio: bool = False
    audio_device: str | None = None
    prefer_hardware_encoder: bool = True
    keyframe_interval_sec: float = 0.5
    monitor_index: int = 1
    capture_region: CaptureRegion | None = None
    auto_stop_seconds: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.temp_dir, Path):
            self.temp_dir = Path(self.temp_dir)


@dataclass
class CaptureResult:
    """Metadata and file outcome produced after stopping a recording."""

    raw_file_path: Path
    duration_sec: float
    width: int
    height: int
    fps: float
    video_codec: str
    has_audio: bool
    encoder_used: str
    start_latency_ms: float
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.raw_file_path, Path):
            self.raw_file_path = Path(self.raw_file_path)

    @property
    def is_valid(self) -> bool:
        """Return True if capture completed with no error and a non-empty, positive-duration file."""
        if self.error:
            return False
        if not (self.duration_sec > 0) or math.isinf(self.duration_sec):
            return False
        try:
            return self.raw_file_path.is_file() and self.raw_file_path.stat().st_size > 0
        except OSError:
            return False


@dataclass
class TrimRequest:
    """Parameters describing a trim or direct-save export operation."""

    source_path: Path
    start_time_sec: float
    end_time_sec: float | None
    output_dir: Path
    direct_save: bool = False
    cleanup_source: bool = True
    filename_prefix: str = "QuickClip"

    def __post_init__(self) -> None:
        if not isinstance(self.source_path, Path):
            self.source_path = Path(self.source_path)
        if not isinstance(self.output_dir, Path):
            self.output_dir = Path(self.output_dir)

    def validate(self) -> None:
        """Validate source file existence and trim time boundaries.

        Raises:
            ValueError: If the source file is missing, ``start_time_sec`` is negative
                or non-finite, or (when ``direct_save`` is False) ``end_time_sec`` is
                missing, non-finite, or not greater than ``start_time_sec``.
        """
        if not self.source_path.is_file():
            raise ValueError(f"Source file does not exist: {self.source_path}")
        if (
            math.isnan(self.start_time_sec)
            or math.isinf(self.start_time_sec)
            or self.start_time_sec < 0
        ):
            raise ValueError(
                f"start_time_sec must be a finite non-negative number, got {self.start_time_sec}"
            )
        if not self.direct_save:
            if self.end_time_sec is None:
                raise ValueError("end_time_sec must be provided when direct_save is False")
            if (
                math.isnan(self.end_time_sec)
                or math.isinf(self.end_time_sec)
                or self.end_time_sec <= self.start_time_sec
            ):
                raise ValueError(
                    f"end_time_sec ({self.end_time_sec}) must be greater than "
                    f"start_time_sec ({self.start_time_sec})"
                )


@dataclass
class TrimResult:
    """Outcome produced after exporting or directly saving a recorded clip."""

    success: bool
    output_path: Path | None
    duration_sec: float
    elapsed_ms: float
    source_cleaned: bool
    command_executed: list[str] = field(default_factory=list)
    error: str | None = None

    def __post_init__(self) -> None:
        if self.output_path is not None and not isinstance(self.output_path, Path):
            self.output_path = Path(self.output_path)


@dataclass
class AppConfig:
    """Top-level application configuration."""

    capture: CaptureConfig = field(default_factory=CaptureConfig)
    output_dir: Path = field(default_factory=_default_output_dir)
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"

    def __post_init__(self) -> None:
        if not isinstance(self.output_dir, Path):
            self.output_dir = Path(self.output_dir)


@runtime_checkable
class ICaptureEngine(Protocol):
    """Protocol for screen capture engines supporting pre-warmed recording."""

    @property
    def state(self) -> CaptureState:
        """Return the current lifecycle state of the capture engine."""
        ...

    def warm_up(self) -> None:
        """Pre-warm capture resources so recording can begin with minimal latency."""
        ...

    def start_recording(self) -> None:
        """Begin capturing video (and optional audio) to the raw temporary file."""
        ...

    def stop_recording(self) -> CaptureResult:
        """Stop an active recording and return the resulting capture metadata."""
        ...

    def shutdown(self) -> None:
        """Cleanly tear down background capture processes and release resources."""
        ...


@runtime_checkable
class IExporterService(Protocol):
    """Protocol for clip trimming and export services."""

    def export(self, request: TrimRequest) -> TrimResult:
        """Trim or directly save a recorded source clip according to ``request``."""
        ...


@runtime_checkable
class IHotkeyListener(Protocol):
    """Protocol for global hotkey listeners."""

    def register(self, hotkey: str, callback: Callable[[], None]) -> None:
        """Register ``hotkey`` to invoke ``callback`` when pressed."""
        ...

    def unregister(self) -> None:
        """Unregister the active hotkey binding and release listener resources."""
        ...


__all__ = [
    "AppConfig",
    "CaptureConfig",
    "CaptureRegion",
    "CaptureResult",
    "CaptureState",
    "ICaptureEngine",
    "IExporterService",
    "IHotkeyListener",
    "MonitorInfo",
    "TrimRequest",
    "TrimResult",
]
