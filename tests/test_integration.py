"""End-to-end integration tests for QuickClip wiring, capture, UI, and export."""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import subprocess
import sys
import threading
import uuid

from PySide6.QtCore import QTimer
import pytest

from main import build_app_config, build_parser, configure_logging, main as cli_main
from quickclip.app import QuickClipController, run_app, try_activate_existing_instance
from quickclip.contracts import (
    AppConfig,
    CaptureConfig,
    CaptureResult,
    CaptureState,
    IHotkeyListener,
    MonitorInfo,
    TrimResult,
)


class _SyntheticGrabber:
    """Fast deterministic BGRA screen grabber for end-to-end FFmpeg tests."""

    def __init__(self, width: int = 640, height: int = 360) -> None:
        self._width = width
        self._height = height
        self._counter = 0
        self._frames = [
            bytes([30, 80, 200, 255]) * (width * height),
            bytes([60, 170, 90, 255]) * (width * height),
            bytes([200, 120, 40, 255]) * (width * height),
        ]

    @property
    def backend_name(self) -> str:
        return "synthetic"

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    def prepare_thread(self) -> None:
        return None

    def grab(self) -> bytes:
        frame = self._frames[self._counter % len(self._frames)]
        self._counter += 1
        return frame

    def cleanup_thread(self) -> None:
        return None

    def close(self) -> None:
        return None


class _StubHotkeyListener:
    """Thread-capable stub satisfying :class:`IHotkeyListener`."""

    def __init__(self) -> None:
        self.hotkey: str | None = None
        self.callback: Callable[[], None] | None = None
        self.unregistered = False

    def register(self, hotkey: str, callback: Callable[[], None]) -> None:
        self.hotkey = hotkey
        self.callback = callback
        self.unregistered = False

    def unregister(self) -> None:
        self.callback = None
        self.unregistered = True

    def trigger_from_thread(self) -> None:
        assert self.callback is not None
        cb = self.callback
        t = threading.Thread(target=cb, daemon=True)
        t.start()
        t.join(timeout=2.0)


def _probe_mp4(path: Path) -> dict:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,codec_type,width,height",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(proc.stdout)


def test_sprint1_full_cycle_trim_and_save_responsive(qtbot, tmp_path: Path) -> None:
    temp_dir = tmp_path / "temp"
    out_dir = tmp_path / "exports"
    config = AppConfig(
        capture=CaptureConfig(
            temp_dir=temp_dir,
            temp_filename="temp_raw.mkv",
            fps=30,
            keyframe_interval_sec=0.5,
            prefer_hardware_encoder=True,
            auto_stop_seconds=None,
        ),
        output_dir=out_dir,
    )
    hotkey_stub = _StubHotkeyListener()
    assert isinstance(hotkey_stub, IHotkeyListener)

    controller = QuickClipController(
        config=config,
        hotkey_listener=hotkey_stub,
        grabber_factory=lambda _cfg: _SyntheticGrabber(),
        enable_tray=False,
        auto_close_ms=0,
        quit_on_exit=False,
    )

    captures: list[CaptureResult] = []
    exports: list[TrimResult] = []
    controller.captureFinished.connect(captures.append)
    controller.exportFinished.connect(exports.append)

    heartbeat_ticks = 0
    heartbeat = QTimer()
    heartbeat.setInterval(15)

    def _on_tick() -> None:
        nonlocal heartbeat_ticks
        heartbeat_ticks += 1

    heartbeat.timeout.connect(_on_tick)
    heartbeat.start()

    try:
        controller.start(show_standby=True)
        qtbot.waitUntil(
            lambda: controller.capture_engine.state == CaptureState.STANDBY
            and controller.standby_window.state == CaptureState.STANDBY,
            timeout=15000,
        )

        # Trigger quick-clip flow (auto-stop after 2.0s while keeping base auto_stop=None).
        controller.standby_window.quickClipRequested.emit(2.0)
        qtbot.waitUntil(
            lambda: controller.standby_window.state == CaptureState.RECORDING,
            timeout=5000,
        )
        qtbot.waitUntil(lambda: len(captures) == 1, timeout=15000)

        cap_res = captures[0]
        assert cap_res.is_valid is True
        assert cap_res.duration_sec >= 1.5
        assert cap_res.raw_file_path.is_file()
        assert config.capture.auto_stop_seconds is None

        trimmer = controller.trimmer_window
        assert trimmer is not None
        assert trimmer.isVisible()
        assert trimmer.capture_result is cap_res
        assert trimmer.range_slider.duration_ms() >= 1500

        # Trim a sub-range and click "Kırp & Kaydet".
        trimmer.range_slider.set_range(500, 1500)
        trimmer.trim_save_button.click()
        assert trimmer.player.source().isEmpty()

        qtbot.waitUntil(lambda: len(exports) == 1, timeout=15000)
        trim_res = exports[0]
        assert trim_res.success is True
        assert trim_res.output_path is not None
        assert trim_res.output_path.is_file()
        assert trim_res.output_path.parent == out_dir
        assert trim_res.source_cleaned is True
        assert not cap_res.raw_file_path.exists()

        probed = _probe_mp4(trim_res.output_path)
        video_streams = [s for s in probed["streams"] if s["codec_type"] == "video"]
        assert video_streams and video_streams[0]["codec_name"] == "h264"
        assert float(probed["format"]["duration"]) > 0.3

        # Engine automatically re-arms back to STANDBY after successful export.
        qtbot.waitUntil(
            lambda: controller.capture_engine.state == CaptureState.STANDBY
            and controller.standby_window.state == CaptureState.STANDBY,
            timeout=15000,
        )
        assert heartbeat_ticks >= 5
    finally:
        heartbeat.stop()
        controller.shutdown()


def test_hotkey_direct_save_and_auto_close(qtbot, tmp_path: Path) -> None:
    config = AppConfig(
        capture=CaptureConfig(
            temp_dir=tmp_path / "temp",
            fps=30,
            keyframe_interval_sec=0.5,
            prefer_hardware_encoder=True,
        ),
        output_dir=tmp_path / "exports",
    )
    hotkey_stub = _StubHotkeyListener()
    controller = QuickClipController(
        config=config,
        hotkey_listener=hotkey_stub,
        grabber_factory=lambda _cfg: _SyntheticGrabber(),
        enable_tray=False,
        auto_close_ms=100,
        quit_on_exit=False,
    )

    captures: list[CaptureResult] = []
    exports: list[TrimResult] = []
    controller.captureFinished.connect(captures.append)
    controller.exportFinished.connect(exports.append)

    try:
        controller.start(show_standby=False)
        qtbot.waitUntil(
            lambda: controller.capture_engine.state == CaptureState.STANDBY,
            timeout=15000,
        )

        # Simulate background Win32 hotkey thread starting capture.
        hotkey_stub.trigger_from_thread()
        qtbot.waitUntil(
            lambda: controller.standby_window.state == CaptureState.RECORDING,
            timeout=5000,
        )
        qtbot.wait(1100)

        # Simulate background hotkey thread stopping capture.
        hotkey_stub.trigger_from_thread()
        qtbot.waitUntil(lambda: len(captures) == 1, timeout=15000)

        cap_res = captures[0]
        assert cap_res.is_valid is True

        trimmer = controller.trimmer_window
        assert trimmer is not None
        assert trimmer.isVisible()

        trimmer.direct_save_button.click()
        qtbot.waitUntil(lambda: len(exports) == 1, timeout=15000)

        trim_res = exports[0]
        assert trim_res.success is True
        assert trim_res.output_path is not None
        assert trim_res.output_path.is_file()
        assert not cap_res.raw_file_path.exists()

        probed = _probe_mp4(trim_res.output_path)
        assert float(probed["format"]["duration"]) >= 0.7

        qtbot.waitUntil(lambda: not trimmer.isVisible(), timeout=5000)
        qtbot.waitUntil(
            lambda: controller.capture_engine.state == CaptureState.STANDBY,
            timeout=15000,
        )
    finally:
        controller.shutdown()
        assert hotkey_stub.unregistered is True


def test_discard_and_close_cleanup_and_rewarm(qtbot, tmp_path: Path) -> None:
    out_dir = tmp_path / "exports"
    config = AppConfig(
        capture=CaptureConfig(
            temp_dir=tmp_path / "temp",
            fps=30,
            keyframe_interval_sec=0.5,
            prefer_hardware_encoder=True,
        ),
        output_dir=out_dir,
    )
    controller = QuickClipController(
        config=config,
        hotkey_listener=_StubHotkeyListener(),
        grabber_factory=lambda _cfg: _SyntheticGrabber(),
        enable_tray=False,
        auto_close_ms=0,
        quit_on_exit=False,
    )

    captures: list[CaptureResult] = []
    controller.captureFinished.connect(captures.append)

    try:
        controller.start(show_standby=False)
        qtbot.waitUntil(
            lambda: controller.capture_engine.state == CaptureState.STANDBY,
            timeout=15000,
        )

        # Cycle 1: Discard via "İptal / Sil" button.
        controller.standby_window.recordToggleRequested.emit()
        qtbot.waitUntil(
            lambda: controller.standby_window.state == CaptureState.RECORDING,
            timeout=5000,
        )
        qtbot.wait(800)
        controller.standby_window.recordToggleRequested.emit()
        qtbot.waitUntil(lambda: len(captures) == 1, timeout=15000)

        first_raw = captures[0].raw_file_path
        assert first_raw.is_file()
        trimmer = controller.trimmer_window
        assert trimmer is not None
        trimmer.discard_button.click()

        qtbot.waitUntil(lambda: not trimmer.isVisible(), timeout=5000)
        qtbot.waitUntil(
            lambda: controller.capture_engine.state == CaptureState.STANDBY,
            timeout=15000,
        )
        assert not first_raw.exists()
        assert not out_dir.exists() or not list(out_dir.glob("*.mp4"))

        # Cycle 2: Close TrimmerWindow directly without exporting.
        controller.standby_window.recordToggleRequested.emit()
        qtbot.waitUntil(
            lambda: controller.standby_window.state == CaptureState.RECORDING,
            timeout=5000,
        )
        qtbot.wait(800)
        controller.standby_window.recordToggleRequested.emit()
        qtbot.waitUntil(lambda: len(captures) == 2, timeout=15000)

        second_raw = captures[1].raw_file_path
        assert second_raw.is_file()
        trimmer.close()

        qtbot.waitUntil(
            lambda: controller.capture_engine.state == CaptureState.STANDBY,
            timeout=15000,
        )
        assert not second_raw.exists()
    finally:
        controller.shutdown()


def test_live_default_capture_engine_controller_smoke(qtbot, tmp_path: Path) -> None:
    config = AppConfig(
        capture=CaptureConfig(
            temp_dir=tmp_path / "temp",
            fps=15,
            keyframe_interval_sec=0.5,
            prefer_hardware_encoder=True,
        ),
        output_dir=tmp_path / "exports",
    )
    controller = QuickClipController(
        config=config,
        hotkey_listener=_StubHotkeyListener(),
        enable_tray=False,
        auto_close_ms=0,
        quit_on_exit=False,
    )
    captures: list[CaptureResult] = []
    controller.captureFinished.connect(captures.append)

    try:
        controller.start(show_standby=False)
        qtbot.waitUntil(
            lambda: controller.capture_engine.state == CaptureState.STANDBY,
            timeout=15000,
        )
        assert controller.capture_engine.grabber_backend in ("dxcam", "mss")

        controller.start_quick_clip(1.0)
        qtbot.waitUntil(lambda: len(captures) == 1, timeout=15000)

        cap_res = captures[0]
        assert cap_res.is_valid is True
        assert cap_res.raw_file_path.is_file()

        trimmer = controller.trimmer_window
        assert trimmer is not None
        trimmer.discard_button.click()
        qtbot.waitUntil(
            lambda: controller.capture_engine.state == CaptureState.STANDBY,
            timeout=15000,
        )
        assert not cap_res.raw_file_path.exists()
    finally:
        controller.shutdown()


def test_manual_stop_and_auto_stop_race_deduplicated(qtbot, tmp_path: Path) -> None:
    config = AppConfig(
        capture=CaptureConfig(
            temp_dir=tmp_path / "temp",
            fps=30,
            keyframe_interval_sec=0.5,
            prefer_hardware_encoder=False,
        ),
        output_dir=tmp_path / "exports",
    )
    controller = QuickClipController(
        config=config,
        hotkey_listener=_StubHotkeyListener(),
        grabber_factory=lambda _cfg: _SyntheticGrabber(width=320, height=180),
        enable_tray=False,
        auto_close_ms=0,
        quit_on_exit=False,
    )

    captures: list[CaptureResult] = []
    controller.captureFinished.connect(captures.append)

    try:
        controller.start(show_standby=False)
        qtbot.waitUntil(
            lambda: controller.capture_engine.state == CaptureState.STANDBY,
            timeout=15000,
        )

        controller.start_quick_clip(0.8)
        qtbot.waitUntil(
            lambda: controller.capture_engine.state == CaptureState.RECORDING,
            timeout=5000,
        )
        # Wait near the auto-stop deadline and issue manual stop as well.
        qtbot.wait(780)
        controller.stop_recording()
        qtbot.waitUntil(lambda: len(captures) >= 1, timeout=15000)
        qtbot.wait(300)

        assert len(captures) == 1
        assert captures[0].is_valid is True
    finally:
        controller.shutdown()


def test_main_cli_parsing_and_headless_check(tmp_path: Path) -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "--hotkey",
            "ctrl+shift+r",
            "--fps",
            "30",
            "--output-dir",
            str(tmp_path / "out"),
            "--temp-dir",
            str(tmp_path / "tmp"),
            "--auto-stop",
            "5.0",
            "--cpu-only",
            "--audio",
        ]
    )
    cfg = build_app_config(args)
    assert cfg.capture.hotkey == "ctrl+shift+r"
    assert cfg.capture.fps == 30
    assert cfg.output_dir == tmp_path / "out"
    assert cfg.capture.temp_dir == tmp_path / "tmp"
    assert cfg.capture.auto_stop_seconds == 5.0
    assert cfg.capture.prefer_hardware_encoder is False
    assert cfg.capture.capture_audio is True

    rc = cli_main(
        [
            "--headless-check",
            "--output-dir",
            str(tmp_path / "out"),
            "--temp-dir",
            str(tmp_path / "tmp"),
        ]
    )
    assert rc == 0

    proc = subprocess.run(
        [sys.executable, "main.py", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0
    assert "--hotkey" in proc.stdout
    assert "--headless-check" in proc.stdout


def test_single_instance_activation_and_second_run_app(qtbot, tmp_path: Path) -> None:
    server_name = f"QuickClipTest-{uuid.uuid4().hex}"
    config = AppConfig(
        capture=CaptureConfig(temp_dir=tmp_path / "temp", fps=30),
        output_dir=tmp_path / "exports",
    )
    controller = QuickClipController(
        config=config,
        hotkey_listener=_StubHotkeyListener(),
        grabber_factory=lambda _cfg: _SyntheticGrabber(width=320, height=180),
        enable_tray=False,
        auto_close_ms=0,
        quit_on_exit=False,
        single_instance_name=server_name,
    )

    try:
        controller.start(show_standby=True)
        qtbot.waitUntil(
            lambda: controller.capture_engine.state == CaptureState.STANDBY,
            timeout=15000,
        )
        controller.standby_window.hide()
        assert not controller.standby_window.isVisible()

        assert try_activate_existing_instance(server_name) is True
        qtbot.waitUntil(lambda: controller.standby_window.isVisible(), timeout=5000)

        # A second run_app invocation activates the running instance and exits 0 immediately.
        controller.standby_window.hide()
        rc = run_app(config=config, enable_tray=False, single_instance_name=server_name)
        assert rc == 0
        qtbot.waitUntil(lambda: controller.standby_window.isVisible(), timeout=5000)
    finally:
        controller.shutdown()

    assert try_activate_existing_instance(server_name, timeout_ms=100) is False


def test_startup_checkbox_controller_wiring_mocked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = AppConfig(
        capture=CaptureConfig(temp_dir=tmp_path / "temp", fps=30),
        output_dir=tmp_path / "exports",
    )
    reg_state = {"enabled": True, "should_fail": False}
    calls: list[bool] = []

    def fake_is_enabled() -> bool:
        return reg_state["enabled"]

    def fake_set_enabled(enabled: bool) -> bool:
        calls.append(enabled)
        if reg_state["should_fail"]:
            return False
        reg_state["enabled"] = enabled
        return True

    monkeypatch.setattr("quickclip.startup.is_run_at_startup_enabled", fake_is_enabled)
    monkeypatch.setattr("quickclip.startup.set_run_at_startup", fake_set_enabled)

    controller = QuickClipController(
        config=config,
        hotkey_listener=_StubHotkeyListener(),
        grabber_factory=lambda _cfg: _SyntheticGrabber(width=320, height=180),
        enable_tray=False,
        quit_on_exit=False,
    )
    try:
        assert controller.standby_window.is_run_at_startup_checked() is True
        assert calls == []

        controller.standby_window.startup_checkbox.setChecked(False)
        assert calls == [False]
        assert reg_state["enabled"] is False
        assert controller.standby_window.is_run_at_startup_checked() is False

        # When setting fails, checkbox reverts cleanly without infinite recursion.
        reg_state["should_fail"] = True
        controller.standby_window.startup_checkbox.setChecked(True)
        assert calls == [False, True]
        assert controller.standby_window.is_run_at_startup_checked() is False
        assert "güncellenemedi" in controller.standby_window.status_message_label.text()
    finally:
        controller.shutdown()


def test_windowed_logging_when_stderr_is_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "stdout", None)

    try:
        rc = cli_main(
            [
                "--headless-check",
                "--verbose",
                "--temp-dir",
                str(log_dir),
                "--output-dir",
                str(tmp_path / "out"),
            ]
        )
        assert rc == 0
        log_file = log_dir / "quickclip.log"
        assert log_file.is_file()
        assert "QuickClip headless check OK" in log_file.read_text(encoding="utf-8")
    finally:
        import logging

        root_logger = logging.getLogger()
        for h in list(root_logger.handlers):
            h.close()
            root_logger.removeHandler(h)
        monkeypatch.undo()
        configure_logging(verbose=False)


def test_monitor_selection_rewarms_and_records_selected_screen(qtbot, tmp_path: Path) -> None:
    fake_monitors = [
        MonitorInfo(index=1, name="MainDisp", width=640, height=360, left=0, top=0, is_primary=True),
        MonitorInfo(index=2, name="SecondDisp", width=320, height=240, left=640, top=0, is_primary=False),
    ]

    def _grabber_for_monitor(cfg: CaptureConfig) -> _SyntheticGrabber:
        if cfg.monitor_index == 2:
            return _SyntheticGrabber(width=320, height=240)
        return _SyntheticGrabber(width=640, height=360)

    config = AppConfig(
        capture=CaptureConfig(
            temp_dir=tmp_path / "temp",
            fps=30,
            keyframe_interval_sec=0.5,
            monitor_index=1,
            prefer_hardware_encoder=False,
        ),
        output_dir=tmp_path / "exports",
    )
    controller = QuickClipController(
        config=config,
        hotkey_listener=_StubHotkeyListener(),
        grabber_factory=_grabber_for_monitor,
        monitors_fn=lambda: fake_monitors,
        enable_tray=False,
        auto_close_ms=0,
        quit_on_exit=False,
    )
    captures: list[CaptureResult] = []
    exports: list[TrimResult] = []
    controller.captureFinished.connect(captures.append)
    controller.exportFinished.connect(exports.append)

    try:
        controller.start(show_standby=True)
        qtbot.waitUntil(
            lambda: controller.capture_engine.state == CaptureState.STANDBY
            and controller.standby_window.state == CaptureState.STANDBY,
            timeout=15000,
        )
        assert controller.standby_window.monitor_combo.count() == 2
        assert controller.capture_engine.capture_size == (640, 360)

        # User selects Ekran 2 in the StandbyWindow dropdown.
        controller.standby_window.monitor_combo.setCurrentIndex(1)
        qtbot.waitUntil(
            lambda: controller.capture_engine.state == CaptureState.STANDBY
            and controller.capture_engine.capture_size == (320, 240),
            timeout=15000,
        )
        assert config.capture.monitor_index == 2
        assert "320x240" in controller.standby_window.encoder_label.text()

        # Record 1.0s on the newly selected monitor and direct-save.
        controller.start_quick_clip(1.0)
        qtbot.waitUntil(lambda: len(captures) == 1, timeout=15000)

        cap_res = captures[0]
        assert cap_res.is_valid is True
        assert cap_res.width == 320
        assert cap_res.height == 240

        trimmer = controller.trimmer_window
        assert trimmer is not None
        trimmer.direct_save_button.click()
        qtbot.waitUntil(lambda: len(exports) == 1, timeout=15000)

        trim_res = exports[0]
        assert trim_res.success is True and trim_res.output_path is not None
        probed = _probe_mp4(trim_res.output_path)
        v_stream = next(s for s in probed["streams"] if s["codec_type"] == "video")
        assert v_stream["width"] == 320
        assert v_stream["height"] == 240
    finally:
        controller.shutdown()
