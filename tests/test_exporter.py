"""Unit and real FFmpeg integration tests for QuickClip ExporterService."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import patch

import pytest

from quickclip.contracts import IExporterService, TrimRequest
from quickclip.exporter import ExporterService


def _ffprobe_info(path: Path) -> dict:
    """Return parsed ffprobe format + stream metadata for ``path``."""
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def sample_mkv_templates(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Generate synthetic 5-second H.264 MKV clips once for the test module."""
    base_dir = tmp_path_factory.mktemp("exporter_media")

    video_only = base_dir / "template_video.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=30:duration=5",
            "-c:v",
            "libx264",
            "-g",
            "15",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            str(video_only),
        ],
        capture_output=True,
        check=True,
    )

    video_audio = base_dir / "template_video_audio.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=30:duration=5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=5",
            "-c:v",
            "libx264",
            "-g",
            "15",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(video_audio),
        ],
        capture_output=True,
        check=True,
    )

    return {"video_only": video_only, "video_audio": video_audio}


def test_exporter_implements_protocol_and_defaults() -> None:
    service = ExporterService()
    assert isinstance(service, IExporterService)
    assert service.ffmpeg_path == "ffmpeg"
    assert service.ffprobe_path == "ffprobe"

    custom = ExporterService(ffmpeg_path="custom_ffmpeg", ffprobe_path="custom_ffprobe")
    assert custom.ffmpeg_path == "custom_ffmpeg"
    assert custom.ffprobe_path == "custom_ffprobe"


def test_build_output_path_formatting_and_collisions(tmp_path: Path) -> None:
    service = ExporterService()
    ts = datetime(2026, 11, 16, 17, 45, 30)

    first = service.build_output_path(tmp_path, prefix="QuickClip", now=ts)
    assert first == tmp_path / "QuickClip_2026-11-16_17-45-30.mp4"

    first.write_bytes(b"existing-0")
    second = service.build_output_path(tmp_path, prefix="QuickClip", now=ts)
    assert second == tmp_path / "QuickClip_2026-11-16_17-45-30_01.mp4"

    second.write_bytes(b"existing-1")
    third = service.build_output_path(tmp_path, prefix="QuickClip", now=ts)
    assert third == tmp_path / "QuickClip_2026-11-16_17-45-30_02.mp4"
    third.write_bytes(b"existing-2")

    custom_prefix = service.build_output_path(tmp_path, prefix="MyGame", now=ts)
    assert custom_prefix == tmp_path / "MyGame_2026-11-16_17-45-30.mp4"

    blank_prefix = service.build_output_path(tmp_path, prefix="   ", now=ts)
    assert blank_prefix == tmp_path / "QuickClip_2026-11-16_17-45-30_03.mp4"


def test_probe_duration_real_and_errors(
    sample_mkv_templates: dict[str, Path], tmp_path: Path
) -> None:
    service = ExporterService()
    duration = service.probe_duration(sample_mkv_templates["video_only"])
    assert duration == pytest.approx(5.0, abs=0.1)

    with pytest.raises(FileNotFoundError):
        service.probe_duration(tmp_path / "missing.mkv")

    with pytest.raises(FileNotFoundError):
        service.probe_duration(tmp_path)

    corrupt = tmp_path / "corrupt.mkv"
    corrupt.write_bytes(b"not-a-valid-media-file")
    with pytest.raises(RuntimeError):
        service.probe_duration(corrupt)


def test_probe_duration_stream_tags_hms_fallback(tmp_path: Path) -> None:
    dummy = tmp_path / "dummy.mkv"
    dummy.write_bytes(b"placeholder")
    service = ExporterService()

    fake_stdout = json.dumps(
        {
            "format": {"duration": "N/A"},
            "streams": [
                {
                    "codec_type": "video",
                    "tags": {"DURATION": "00:00:04.250000000"},
                }
            ],
        }
    )
    fake_proc = subprocess.CompletedProcess(
        args=["ffprobe"], returncode=0, stdout=fake_stdout, stderr=""
    )
    with patch("quickclip.exporter.service.subprocess.run", return_value=fake_proc):
        assert service.probe_duration(dummy) == pytest.approx(4.25)


def test_export_lossless_trim_real_ffmpeg(
    sample_mkv_templates: dict[str, Path], tmp_path: Path
) -> None:
    service = ExporterService()
    source = tmp_path / "temp_raw.mkv"
    shutil.copy2(sample_mkv_templates["video_only"], source)
    out_dir = tmp_path / "exports" / "nested"

    request = TrimRequest(
        source_path=source,
        start_time_sec=1.0,
        end_time_sec=3.5,
        output_dir=out_dir,
        direct_save=False,
        cleanup_source=True,
    )
    result = service.export(request)

    assert result.success is True
    assert result.error is None
    assert result.output_path is not None
    assert result.output_path.is_file()
    assert result.output_path.parent == out_dir
    assert result.output_path.suffix == ".mp4"
    assert result.output_path.stat().st_size > 0
    assert result.elapsed_ms > 0.0

    # Critical Rule 2: stream copy only, never re-encode, -ss/-to placed before -i.
    cmd = result.command_executed
    assert "-c" in cmd
    c_idx = cmd.index("-c")
    assert cmd[c_idx + 1] == "copy"
    assert "-movflags" in cmd
    assert "+faststart" in cmd
    assert cmd.index("-ss") < cmd.index("-i")
    assert cmd.index("-to") < cmd.index("-i")
    joined = " ".join(cmd)
    for forbidden in ("libx264", "h264_amf", "libx265", "hevc_amf", "h264_nvenc"):
        assert forbidden not in joined

    # Verify duration matches (end - start) ~= 2.5s (and NOT end = 3.5s) and codec is h264.
    assert result.duration_sec == pytest.approx(2.5, abs=0.25)
    info = _ffprobe_info(result.output_path)
    video_streams = [s for s in info["streams"] if s["codec_type"] == "video"]
    assert len(video_streams) == 1
    assert video_streams[0]["codec_name"] == "h264"
    probed_dur = float(info["format"]["duration"])
    assert probed_dur == pytest.approx(2.5, abs=0.25)
    assert probed_dur < 3.0

    # Source temp_raw.mkv cleaned up on success.
    assert result.source_cleaned is True
    assert not source.exists()


def test_export_lossless_trim_non_keyframe_and_audio(
    sample_mkv_templates: dict[str, Path], tmp_path: Path
) -> None:
    service = ExporterService()
    source = tmp_path / "temp_raw.mkv"
    shutil.copy2(sample_mkv_templates["video_audio"], source)
    out_dir = tmp_path / "exports"

    request = TrimRequest(
        source_path=source,
        start_time_sec=1.2,
        end_time_sec=3.5,
        output_dir=out_dir,
        direct_save=False,
        cleanup_source=False,
    )
    result = service.export(request)

    assert result.success is True
    assert result.output_path is not None and result.output_path.is_file()
    assert result.duration_sec == pytest.approx(2.3, abs=0.5)

    info = _ffprobe_info(result.output_path)
    codec_types = {s["codec_type"]: s["codec_name"] for s in info["streams"]}
    assert codec_types["video"] == "h264"
    assert codec_types["audio"] == "aac"

    # cleanup_source=False preserves temp_raw.mkv.
    assert result.source_cleaned is False
    assert source.exists()


def test_export_direct_save_real_ffmpeg(
    sample_mkv_templates: dict[str, Path], tmp_path: Path
) -> None:
    service = ExporterService()
    source = tmp_path / "temp_raw.mkv"
    shutil.copy2(sample_mkv_templates["video_only"], source)
    out_dir = tmp_path / "exports"

    request = TrimRequest(
        source_path=source,
        start_time_sec=0.0,
        end_time_sec=None,
        output_dir=out_dir,
        direct_save=True,
        cleanup_source=True,
    )
    result = service.export(request)

    assert result.success is True
    assert result.error is None
    assert result.output_path is not None and result.output_path.is_file()
    assert result.duration_sec == pytest.approx(5.0, abs=0.2)
    assert result.source_cleaned is True
    assert not source.exists()

    cmd = result.command_executed
    assert "-ss" not in cmd
    assert "-to" not in cmd
    c_idx = cmd.index("-c")
    assert cmd[c_idx + 1] == "copy"
    assert "+faststart" in cmd

    info = _ffprobe_info(result.output_path)
    assert info["streams"][0]["codec_name"] == "h264"


def test_export_failures_preserve_source_file(
    sample_mkv_templates: dict[str, Path], tmp_path: Path
) -> None:
    service = ExporterService()
    out_dir = tmp_path / "exports"

    # 1. Validation failure (end <= start) must preserve temp_raw.mkv.
    valid_source = tmp_path / "temp_raw.mkv"
    shutil.copy2(sample_mkv_templates["video_only"], valid_source)
    invalid_range = TrimRequest(
        source_path=valid_source,
        start_time_sec=3.0,
        end_time_sec=1.5,
        output_dir=out_dir,
        direct_save=False,
        cleanup_source=True,
    )
    res_invalid = service.export(invalid_range)
    assert res_invalid.success is False
    assert res_invalid.output_path is None
    assert res_invalid.duration_sec == 0.0
    assert res_invalid.source_cleaned is False
    assert res_invalid.command_executed == []
    assert res_invalid.error is not None
    assert valid_source.exists()

    # 2. Missing source file failure.
    missing_req = TrimRequest(
        source_path=tmp_path / "nonexistent.mkv",
        start_time_sec=0.0,
        end_time_sec=None,
        output_dir=out_dir,
        direct_save=True,
        cleanup_source=True,
    )
    res_missing = service.export(missing_req)
    assert res_missing.success is False
    assert res_missing.output_path is None
    assert res_missing.source_cleaned is False
    assert res_missing.error is not None

    # 3. FFmpeg execution failure on corrupt MKV must preserve temp_raw.mkv.
    corrupt_source = tmp_path / "corrupt_raw.mkv"
    corrupt_source.write_bytes(b"corrupt-data-not-mkv")
    res_corrupt = service.export(
        TrimRequest(
            source_path=corrupt_source,
            start_time_sec=0.0,
            end_time_sec=None,
            output_dir=out_dir,
            direct_save=True,
            cleanup_source=True,
        )
    )
    assert res_corrupt.success is False
    assert res_corrupt.output_path is None
    assert res_corrupt.duration_sec == 0.0
    assert res_corrupt.source_cleaned is False
    assert "-c" in res_corrupt.command_executed
    assert res_corrupt.error
    assert corrupt_source.exists()
    assert list(out_dir.glob("*.mp4")) == []

    # 4. Missing ffmpeg binary must fail safely and preserve temp_raw.mkv.
    bad_bin_service = ExporterService(ffmpeg_path="nonexistent_ffmpeg_binary_xyz")
    res_bad_bin = bad_bin_service.export(
        TrimRequest(
            source_path=valid_source,
            start_time_sec=0.0,
            end_time_sec=None,
            output_dir=out_dir,
            direct_save=True,
            cleanup_source=True,
        )
    )
    assert res_bad_bin.success is False
    assert res_bad_bin.output_path is None
    assert res_bad_bin.source_cleaned is False
    assert res_bad_bin.command_executed
    assert res_bad_bin.error
    assert valid_source.exists()


def test_export_probe_failure_preserves_source_and_cleans_output(
    sample_mkv_templates: dict[str, Path], tmp_path: Path
) -> None:
    service = ExporterService(ffprobe_path="nonexistent_ffprobe_binary_xyz")
    source = tmp_path / "temp_raw.mkv"
    shutil.copy2(sample_mkv_templates["video_only"], source)
    out_dir = tmp_path / "exports"

    res = service.export(
        TrimRequest(
            source_path=source,
            start_time_sec=0.0,
            end_time_sec=None,
            output_dir=out_dir,
            direct_save=True,
            cleanup_source=True,
        )
    )
    assert res.success is False
    assert res.output_path is None
    assert res.source_cleaned is False
    assert source.exists()
    assert list(out_dir.glob("*.mp4")) == []


def test_cleanup_temp_safe_and_retries(tmp_path: Path) -> None:
    service = ExporterService()

    # Missing file & directory return False without raising.
    assert service.cleanup_temp(tmp_path / "does_not_exist.mkv") is False
    assert service.cleanup_temp(tmp_path) is False

    # Existing file deleted -> True.
    temp_file = tmp_path / "temp_raw.mkv"
    temp_file.write_bytes(b"1234")
    assert service.cleanup_temp(temp_file) is True
    assert not temp_file.exists()

    # Transient PermissionError recovers on retry.
    locked_then_free = tmp_path / "transient_lock.mkv"
    locked_then_free.write_bytes(b"1234")
    real_unlink = Path.unlink
    attempts = {"count": 0}

    def flaky_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == locked_then_free and attempts["count"] < 2:
            attempts["count"] += 1
            raise PermissionError("Windows handle still closing")
        real_unlink(self, missing_ok=missing_ok)

    with (
        patch.object(Path, "unlink", flaky_unlink),
        patch("quickclip.exporter.service.time.sleep") as mock_sleep,
    ):
        assert service.cleanup_temp(locked_then_free) is True
    assert attempts["count"] == 2
    assert mock_sleep.call_count == 2
    assert not locked_then_free.exists()

    # Persistent PermissionError fails safely without raising.
    always_locked = tmp_path / "always_locked.mkv"
    always_locked.write_bytes(b"1234")
    with (
        patch.object(Path, "unlink", side_effect=PermissionError("Locked")),
        patch("quickclip.exporter.service.time.sleep"),
    ):
        assert service.cleanup_temp(always_locked) is False
    assert always_locked.exists()


def test_exporter_resolves_bundled_binaries_priorities_and_custom_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    exe_suffix = ".exe" if sys.platform == "win32" else ""
    meipass = tmp_path / "meipass"
    exe_dir = tmp_path / "dist" / "QuickClip"
    exe_path = exe_dir / "QuickClip.exe"
    exe_path.parent.mkdir(parents=True, exist_ok=True)
    exe_path.write_bytes(b"exe")

    meipass_ffmpeg = meipass / "bin" / f"ffmpeg{exe_suffix}"
    meipass_ffprobe = meipass / "bin" / f"ffprobe{exe_suffix}"
    frozen_ffmpeg = exe_dir / "bin" / f"ffmpeg{exe_suffix}"
    frozen_ffprobe = exe_dir / "bin" / f"ffprobe{exe_suffix}"
    for bin_path in (meipass_ffmpeg, meipass_ffprobe, frozen_ffmpeg, frozen_ffprobe):
        bin_path.parent.mkdir(parents=True, exist_ok=True)
        bin_path.write_bytes(b"bin")

    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_path))
    monkeypatch.setenv("PATH", "")

    service = ExporterService()
    assert service.ffmpeg_path == "ffmpeg"
    assert service.ffprobe_path == "ffprobe"
    assert service.resolved_ffmpeg_path == str(meipass_ffmpeg)
    assert service.resolved_ffprobe_path == str(meipass_ffprobe)

    # Verify export() and probe_duration() use the resolved _MEIPASS/bin binaries.
    source = tmp_path / "temp_raw.mkv"
    source.write_bytes(b"raw")
    out_dir = tmp_path / "out"
    seen_cmds: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        seen_cmds.append(list(cmd))
        if cmd[0] == str(meipass_ffmpeg):
            Path(cmd[-1]).write_bytes(b"mp4-bytes")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({"format": {"duration": "2.5"}, "streams": []}),
            stderr="",
        )

    with patch("quickclip.exporter.service.subprocess.run", side_effect=fake_run):
        res = service.export(
            TrimRequest(
                source_path=source,
                start_time_sec=1.0,
                end_time_sec=3.5,
                output_dir=out_dir,
                cleanup_source=False,
            )
        )
    assert res.success is True
    assert seen_cmds[0][0] == str(meipass_ffmpeg)
    assert seen_cmds[1][0] == str(meipass_ffprobe)
    assert res.command_executed[0] == str(meipass_ffmpeg)

    # Remove _MEIPASS/bin binaries -> falls back to frozen exe_dir/bin with empty PATH.
    meipass_ffmpeg.unlink()
    meipass_ffprobe.unlink()
    assert service.resolved_ffmpeg_path == str(frozen_ffmpeg)
    assert service.resolved_ffprobe_path == str(frozen_ffprobe)

    # Custom names and explicit paths stay untouched even when _MEIPASS/bin exists.
    meipass_ffmpeg.write_bytes(b"bin")
    meipass_ffprobe.write_bytes(b"bin")
    custom_name = ExporterService(ffmpeg_path="custom_ffmpeg", ffprobe_path="custom_ffprobe")
    assert custom_name.resolved_ffmpeg_path == "custom_ffmpeg"
    assert custom_name.resolved_ffprobe_path == "custom_ffprobe"

    explicit_ffmpeg = r"C:\Tools\ffmpeg.exe"
    explicit_ffprobe = r"C:\Tools\ffprobe.exe"
    explicit = ExporterService(ffmpeg_path=explicit_ffmpeg, ffprobe_path=explicit_ffprobe)
    assert explicit.resolved_ffmpeg_path == explicit_ffmpeg
    assert explicit.resolved_ffprobe_path == explicit_ffprobe
