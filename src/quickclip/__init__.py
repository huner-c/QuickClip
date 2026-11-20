"""QuickClip package root exporting core contracts and metadata."""

from quickclip.contracts import (
    AppConfig,
    CaptureConfig,
    CaptureRegion,
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
from quickclip.settings import (
    UserSettings,
    default_settings_path,
    load_user_settings,
    save_user_settings,
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
    "CaptureRegion",
    "CaptureResult",
    "CaptureState",
    "ICaptureEngine",
    "IExporterService",
    "IHotkeyListener",
    "MonitorInfo",
    "RUN_KEY_PATH",
    "TrimRequest",
    "TrimResult",
    "UserSettings",
    "default_settings_path",
    "default_startup_command",
    "is_run_at_startup_enabled",
    "load_user_settings",
    "resolve_binary_path",
    "resolve_ffmpeg_path",
    "resolve_ffprobe_path",
    "resolve_resource_path",
    "save_user_settings",
    "set_run_at_startup",
]
