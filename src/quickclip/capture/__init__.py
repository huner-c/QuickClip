"""QuickClip capture subsystem exports."""

from quickclip.capture.audio import (
    AudioDevice,
    list_audio_devices,
    parse_dshow_audio_devices,
    probe_audio_device,
    resolve_audio_input,
)
from quickclip.capture.engine import (
    CaptureEngine,
    ENCODER_CANDIDATES,
    HARDWARE_ENCODERS,
    build_encoder_args,
    clear_encoder_cache,
)
from quickclip.capture.hotkey import (
    HotkeyListener,
    parse_hotkey,
)
from quickclip.capture.monitors import (
    list_monitors,
    resolve_monitor_rect,
)

__all__ = [
    "AudioDevice",
    "CaptureEngine",
    "ENCODER_CANDIDATES",
    "HARDWARE_ENCODERS",
    "HotkeyListener",
    "build_encoder_args",
    "clear_encoder_cache",
    "list_audio_devices",
    "list_monitors",
    "parse_dshow_audio_devices",
    "parse_hotkey",
    "probe_audio_device",
    "resolve_audio_input",
    "resolve_monitor_rect",
]
