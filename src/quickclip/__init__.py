"""QuickClip package root exporting core contracts and metadata."""

from quickclip.contracts import (
    AppConfig,
    CaptureConfig,
    CaptureResult,
    CaptureState,
    ICaptureEngine,
    IExporterService,
    IHotkeyListener,
    MonitorInfo,
    TrimRequest,
    TrimResult,
)
from quickclip.paths import (
    resolve_binary_path,
    resolve_ffmpeg_path,
    resolve_ffprobe_path,
    resolve_resource_path,
)
from quickclip.startup import (
    RUN_KEY_PATH,
    default_startup_command,
    is_run_at_startup_enabled,
    set_run_at_startup,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "AppConfig",
    "CaptureConfig",
    "CaptureResult",
    "CaptureState",
    "ICaptureEngine",
    "IExporterService",
    "IHotkeyListener",
    "MonitorInfo",
    "RUN_KEY_PATH",
    "TrimRequest",
    "TrimResult",
    "default_startup_command",
    "is_run_at_startup_enabled",
    "resolve_binary_path",
    "resolve_ffmpeg_path",
    "resolve_ffprobe_path",
    "resolve_resource_path",
    "set_run_at_startup",
]
