"""Unit tests for QuickClip inter-module contracts and package structure."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
import tempfile

import pytest

import quickclip
import quickclip.capture
import quickclip.exporter
import quickclip.ui
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
from quickclip.settings import UserSettings, load_user_settings, save_user_settings


def test_package_exports_and_subpackages() -> None:
    assert quickclip.__version__ == "0.1.0"
    for name in (
        "AppConfig",
        "CaptureConfig",
        "CaptureResult",
        "CaptureState",
        "ICaptureEngine",
        "IExporterService",
        "IHotkeyListener",
        "TrimRequest",
        "TrimResult",
    ):
        assert hasattr(quickclip, name)


def test_capture_state_enum() -> None:
    expected = ["COLD", "WARMING", "STANDBY", "RECORDING", "STOPPING", "ERROR"]
    assert [state.name for state in CaptureState] == expected
    for state in CaptureState:
        assert isinstance(state, str)
        assert isinstance(state, Enum)
        assert str(state) == state.value


def test_capture_config_defaults_and_coercion(tmp_path: Path) -> None:
    cfg = CaptureConfig()
    assert cfg.temp_dir == Path(tempfile.gettempdir()) / "quickclip"
    assert cfg.temp_filename == "temp_raw.mkv"
    assert cfg.hotkey == "F9"
    assert cfg.fps == 60
    assert cfg.capture_audio is False
    assert cfg.audio_device is None
    assert cfg.prefer_hardware_encoder is True
    assert cfg.keyframe_interval_sec == 0.5
    assert cfg.monitor_index == 1
    assert cfg.auto_stop_seconds is None

    custom = CaptureConfig(temp_dir=str(tmp_path))  # type: ignore[arg-type]
    assert isinstance(custom.temp_dir, Path)
    assert custom.temp_dir == tmp_path


def test_capture_result_is_valid(tmp_path: Path) -> None:
    raw_file = tmp_path / "temp_raw.mkv"
    raw_file.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 64)

    valid_result = CaptureResult(
        raw_file_path=raw_file,
        duration_sec=3.5,
        width=1920,
        height=1080,
        fps=60.0,
        video_codec="h264",
        has_audio=False,
        encoder_used="h264_amf",
        start_latency_ms=42.0,
    )
    assert valid_result.is_valid is True
    assert isinstance(valid_result.created_at, datetime)
    assert valid_result.created_at.tzinfo == timezone.utc

    with_error = CaptureResult(
        raw_file_path=raw_file,
        duration_sec=3.5,
        width=1920,
        height=1080,
        fps=60.0,
        video_codec="h264",
        has_audio=False,
        encoder_used="h264_amf",
        start_latency_ms=42.0,
        error="ffmpeg crashed",
    )
    assert with_error.is_valid is False

    missing_file = CaptureResult(
        raw_file_path=tmp_path / "missing.mkv",
        duration_sec=3.5,
        width=1920,
        height=1080,
        fps=60.0,
        video_codec="h264",
        has_audio=False,
        encoder_used="h264_amf",
        start_latency_ms=42.0,
    )
    assert missing_file.is_valid is False

    empty_file = tmp_path / "empty.mkv"
    empty_file.write_bytes(b"")
    empty_result = CaptureResult(
        raw_file_path=empty_file,
        duration_sec=3.5,
        width=1920,
        height=1080,
        fps=60.0,
        video_codec="h264",
        has_audio=False,
        encoder_used="h264_amf",
        start_latency_ms=42.0,
    )
    assert empty_result.is_valid is False

    dir_result = CaptureResult(
        raw_file_path=tmp_path,
        duration_sec=3.5,
        width=1920,
        height=1080,
        fps=60.0,
        video_codec="h264",
        has_audio=False,
        encoder_used="h264_amf",
        start_latency_ms=42.0,
    )
    assert dir_result.is_valid is False

    for bad_duration in (0.0, -0.5, float("nan"), float("inf")):
        bad_dur_result = CaptureResult(
            raw_file_path=raw_file,
            duration_sec=bad_duration,
            width=1920,
            height=1080,
            fps=60.0,
            video_codec="h264",
            has_audio=False,
            encoder_used="h264_amf",
            start_latency_ms=42.0,
        )
        assert bad_dur_result.is_valid is False


def test_trim_request_validate_valid_cases(tmp_path: Path) -> None:
    source = tmp_path / "raw.mkv"
    source.write_bytes(b"fake-mkv-data")
    out_dir = tmp_path / "exports"

    req = TrimRequest(
        source_path=source,
        start_time_sec=0.5,
        end_time_sec=4.25,
        output_dir=out_dir,
    )
    assert req.direct_save is False
    assert req.cleanup_source is True
    assert req.filename_prefix == "QuickClip"
    req.validate()

    direct_req = TrimRequest(
        source_path=source,
        start_time_sec=0.0,
        end_time_sec=None,
        output_dir=out_dir,
        direct_save=True,
    )
    direct_req.validate()


def test_trim_request_validate_errors(tmp_path: Path) -> None:
    source = tmp_path / "raw.mkv"
    source.write_bytes(b"fake-mkv-data")
    out_dir = tmp_path / "exports"

    missing_req = TrimRequest(
        source_path=tmp_path / "missing.mkv",
        start_time_sec=0.0,
        end_time_sec=2.0,
        output_dir=out_dir,
    )
    with pytest.raises(ValueError, match="does not exist"):
        missing_req.validate()

    neg_start = TrimRequest(
        source_path=source,
        start_time_sec=-0.1,
        end_time_sec=2.0,
        output_dir=out_dir,
    )
    with pytest.raises(ValueError, match="start_time_sec"):
        neg_start.validate()

    neg_start_direct = TrimRequest(
        source_path=source,
        start_time_sec=-1.0,
        end_time_sec=None,
        output_dir=out_dir,
        direct_save=True,
    )
    with pytest.raises(ValueError, match="start_time_sec"):
        neg_start_direct.validate()

    none_end = TrimRequest(
        source_path=source,
        start_time_sec=0.0,
        end_time_sec=None,
        output_dir=out_dir,
        direct_save=False,
    )
    with pytest.raises(ValueError, match="end_time_sec"):
        none_end.validate()

    for bad_end in (1.0, 0.5, float("nan"), float("inf")):
        bad_range = TrimRequest(
            source_path=source,
            start_time_sec=1.0,
            end_time_sec=bad_end,
            output_dir=out_dir,
            direct_save=False,
        )
        with pytest.raises(ValueError, match="end_time_sec"):
            bad_range.validate()


def test_trim_result_and_app_config(tmp_path: Path) -> None:
    res_a = TrimResult(
        success=True,
        output_path=tmp_path / "clip.mp4",
        duration_sec=2.5,
        elapsed_ms=110.0,
        source_cleaned=True,
    )
    res_b = TrimResult(
        success=False,
        output_path=None,
        duration_sec=0.0,
        elapsed_ms=5.0,
        source_cleaned=False,
        error="failed",
    )
    assert res_a.command_executed == []
    assert res_a.command_executed is not res_b.command_executed
    assert res_b.error == "failed"

    cfg_a = AppConfig()
    cfg_b = AppConfig()
    assert isinstance(cfg_a.capture, CaptureConfig)
    assert cfg_a.capture is not cfg_b.capture
    assert cfg_a.output_dir == Path.home() / "Videos" / "QuickClip"
    assert cfg_a.ffmpeg_path == "ffmpeg"
    assert cfg_a.ffprobe_path == "ffprobe"


def test_protocols_runtime_checkable(tmp_path: Path) -> None:
    class DummyCaptureEngine:
        @property
        def state(self) -> CaptureState:
            return CaptureState.STANDBY

        def warm_up(self) -> None:
            return None

        def start_recording(self) -> None:
            return None

        def stop_recording(self) -> CaptureResult:
            return CaptureResult(
                raw_file_path=tmp_path / "x.mkv",
                duration_sec=1.0,
                width=1920,
                height=1080,
                fps=60.0,
                video_codec="h264",
                has_audio=False,
                encoder_used="h264_amf",
                start_latency_ms=10.0,
            )

        def shutdown(self) -> None:
            return None

    class DummyExporter:
        def export(self, request: TrimRequest) -> TrimResult:
            return TrimResult(
                success=True,
                output_path=request.output_dir / "out.mp4",
                duration_sec=1.0,
                elapsed_ms=15.0,
                source_cleaned=False,
                command_executed=["ffmpeg"],
            )

    class DummyHotkeyListener:
        def register(self, hotkey: str, callback: Callable[[], None]) -> None:
            return None

        def unregister(self) -> None:
            return None

    assert isinstance(DummyCaptureEngine(), ICaptureEngine)
    assert isinstance(DummyExporter(), IExporterService)
    assert isinstance(DummyHotkeyListener(), IHotkeyListener)
    assert not isinstance(object(), ICaptureEngine)
    assert not isinstance(object(), IExporterService)
    assert not isinstance(object(), IHotkeyListener)


def test_monitor_info_contract_and_label() -> None:
    primary = MonitorInfo(
        index=1,
        name="DisplayA",
        width=3840,
        height=2160,
        left=0,
        top=0,
        is_primary=True,
    )
    secondary = MonitorInfo(
        index=2,
        name="",
        width=1920,
        height=1080,
        left=3840,
        top=0,
        is_primary=False,
    )
    assert quickclip.MonitorInfo is MonitorInfo
    assert primary.label == "Ekran 1 • DisplayA (3840x2160 • Birincil)"
    assert secondary.label == "Ekran 2 (1920x1080)"


def test_capture_region_validation_and_normalization() -> None:
    region = CaptureRegion(left=100, top=50, width=641, height=361, monitor_index=2)
    assert region.right == 741
    assert region.bottom == 411

    norm = region.normalized(1920, 1080)
    assert norm == CaptureRegion(left=100, top=50, width=640, height=360, monitor_index=2)

    clamped = CaptureRegion(left=1900, top=1070, width=200, height=200, monitor_index=1).normalized(
        1920, 1080
    )
    assert clamped == CaptureRegion(left=1900, top=1070, width=20, height=10, monitor_index=1)

    with pytest.raises(ValueError):
        CaptureRegion(left=-1, top=0, width=100, height=100)
    with pytest.raises(ValueError):
        CaptureRegion(left=0, top=0, width=0, height=100)
    with pytest.raises(ValueError):
        CaptureRegion(left=0, top=0, width=100, height=-5)


def test_user_settings_save_load_and_corrupt_fallback(tmp_path: Path) -> None:
    cfg_path = tmp_path / "settings.json"
    assert load_user_settings(cfg_path) == UserSettings()

    saved = UserSettings(
        hotkey="Ctrl+Shift+R",
        output_dir=str(tmp_path / "clips"),
        monitor_index=2,
        region_mode=True,
    )
    assert save_user_settings(saved, cfg_path) is True
    assert load_user_settings(cfg_path) == saved

    cfg_path.write_text("{not-valid-json", encoding="utf-8")
    assert load_user_settings(cfg_path) == UserSettings()
