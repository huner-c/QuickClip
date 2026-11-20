"""Unit and widget tests for QuickClip UI components."""

from __future__ import annotations

import ast
from pathlib import Path
import shutil
import subprocess

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QSize, Qt
from PySide6.QtGui import QIcon, QMouseEvent
from PySide6.QtWidgets import QFileDialog, QSystemTrayIcon
import pytest

from quickclip.contracts import CaptureRegion, CaptureResult, CaptureState, MonitorInfo, TrimRequest, TrimResult
from quickclip.ui import (
    RangeSlider,
    RegionSelectorOverlay,
    StandbyWindow,
    TrimmerWindow,
    logical_rect_to_capture_region,
    qt_key_event_to_hotkey,
)
from quickclip.ui.standby_bar import _build_tray_icon


def _send_mouse(
    widget: RangeSlider,
    event_type: QEvent.Type,
    x: float,
    y: float,
    *,
    button: Qt.MouseButton = Qt.MouseButton.LeftButton,
    buttons: Qt.MouseButton | None = None,
) -> None:
    """Dispatch a synthetic Qt mouse event with exact floating-point coordinates."""
    if buttons is None:
        buttons = Qt.MouseButton.NoButton if event_type == QEvent.Type.MouseButtonRelease else button
    local = QPointF(x, y)
    global_pt = QPointF(widget.mapToGlobal(QPoint(round(x), round(y))))
    ev = QMouseEvent(
        event_type,
        local,
        local,
        global_pt,
        button,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )
    if event_type == QEvent.Type.MouseButtonPress:
        widget.mousePressEvent(ev)
    elif event_type == QEvent.Type.MouseMove:
        widget.mouseMoveEvent(ev)
    elif event_type == QEvent.Type.MouseButtonRelease:
        widget.mouseReleaseEvent(ev)


def test_ui_never_imports_capture_or_exporter() -> None:
    ui_dir = Path(__file__).resolve().parents[1] / "src" / "quickclip" / "ui"
    py_files = list(ui_dir.glob("*.py"))
    assert len(py_files) >= 4

    for py_file in py_files:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("quickclip.capture"), py_file.name
                    assert not alias.name.startswith("quickclip.exporter"), py_file.name
                    assert not alias.name.startswith("quickclip.startup"), py_file.name
                    assert alias.name != "winreg", py_file.name
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert not mod.startswith("quickclip.capture"), py_file.name
                assert not mod.startswith("quickclip.exporter"), py_file.name
                assert not mod.startswith("quickclip.startup"), py_file.name
                assert mod != "winreg", py_file.name


def test_range_slider_bounds_and_clamping(qtbot) -> None:
    slider = RangeSlider()
    qtbot.addWidget(slider)

    assert slider.duration_ms() == 0
    assert slider.start_ms() == 0
    assert slider.end_ms() == 0
    assert slider.position_ms() == 0

    with qtbot.waitSignal(slider.rangeChanged, timeout=1000) as sig:
        slider.set_duration(10000)
    assert sig.args == [0, 10000]
    assert slider.duration_ms() == 10000
    assert slider.start_ms() == 0
    assert slider.end_ms() == 10000

    # Clamp negative and over-duration inputs.
    slider.set_range(-500, 15000)
    assert slider.start_ms() == 0
    assert slider.end_ms() == 10000

    # Enforce minimum span of 200ms when duration allows.
    slider.set_range(4000, 4050)
    assert slider.start_ms() == 4000
    assert slider.end_ms() == 4200
    assert slider.end_ms() - slider.start_ms() >= 200

    # Inverted range still enforces 0 <= start < end <= duration.
    slider.set_range(9950, 9990)
    assert 0 <= slider.start_ms() < slider.end_ms() <= 10000
    assert slider.end_ms() - slider.start_ms() >= 200

    # Very short clip (< 200ms) keeps 0 <= start < end <= duration.
    short_slider = RangeSlider()
    qtbot.addWidget(short_slider)
    short_slider.set_duration(100)
    short_slider.set_range(20, 80)
    assert short_slider.start_ms() == 20
    assert short_slider.end_ms() == 80


def test_range_slider_position_does_not_emit_seek(qtbot) -> None:
    slider = RangeSlider()
    qtbot.addWidget(slider)
    slider.set_duration(5000)

    seek_events: list[int] = []
    slider.seekRequested.connect(seek_events.append)

    slider.set_position(2500)
    assert slider.position_ms() == 2500
    slider.set_position(99999)
    assert slider.position_ms() == 5000
    slider.set_position(-50)
    assert slider.position_ms() == 0
    assert seek_events == []


def test_range_slider_mouse_drag_handles_and_seek(qtbot) -> None:
    slider = RangeSlider()
    qtbot.addWidget(slider)
    slider.resize(428, 48)
    slider.set_duration(10000)
    slider.show()
    qtbot.waitExposed(slider)

    track = slider.track_rect()
    assert track.width() == pytest.approx(400.0)
    y_center = track.center().y()

    range_events: list[tuple[int, int]] = []
    seek_events: list[int] = []
    slider.rangeChanged.connect(lambda s, e: range_events.append((s, e)))
    slider.seekRequested.connect(seek_events.append)

    # Drag start handle from 0ms (track.left) to ~25% (2500ms).
    _send_mouse(slider, QEvent.Type.MouseButtonPress, track.left(), y_center)
    _send_mouse(slider, QEvent.Type.MouseMove, track.left() + track.width() * 0.25, y_center)
    _send_mouse(slider, QEvent.Type.MouseButtonRelease, track.left() + track.width() * 0.25, y_center)

    assert slider.start_ms() == pytest.approx(2500, abs=100)
    assert slider.end_ms() == 10000
    assert len(range_events) >= 1

    # Drag end handle from 10000ms (track.right) to ~75% (7500ms).
    _send_mouse(slider, QEvent.Type.MouseButtonPress, track.right(), y_center)
    _send_mouse(slider, QEvent.Type.MouseMove, track.left() + track.width() * 0.75, y_center)
    _send_mouse(slider, QEvent.Type.MouseButtonRelease, track.left() + track.width() * 0.75, y_center)

    assert slider.end_ms() == pytest.approx(7500, abs=100)

    # Dragging start handle past end handle stops cleanly at end - 200ms.
    _send_mouse(slider, QEvent.Type.MouseButtonPress, track.left() + track.width() * 0.25, y_center)
    _send_mouse(slider, QEvent.Type.MouseMove, track.right(), y_center)
    _send_mouse(slider, QEvent.Type.MouseButtonRelease, track.right(), y_center)

    assert slider.end_ms() - slider.start_ms() == 200

    # Clicking middle track (50%) emits seekRequested without altering trim range.
    prev_range = (slider.start_ms(), slider.end_ms())
    _send_mouse(slider, QEvent.Type.MouseButtonPress, track.left() + track.width() * 0.50, y_center)
    _send_mouse(slider, QEvent.Type.MouseButtonRelease, track.left() + track.width() * 0.50, y_center)

    assert (slider.start_ms(), slider.end_ms()) == prev_range
    assert len(seek_events) >= 1
    assert slider.position_ms() == pytest.approx(5000, abs=100)

    # Render snapshot cleanly for both populated and zero durations.
    assert not slider.grab().isNull()
    slider.set_duration(0)
    assert slider.start_ms() == 0
    assert slider.end_ms() == 0
    assert not slider.grab().isNull()


def _make_capture_result(raw_path: Path, duration_sec: float = 5.0) -> CaptureResult:
    return CaptureResult(
        raw_file_path=raw_path,
        duration_sec=duration_sec,
        width=1920,
        height=1080,
        fps=60.0,
        video_codec="h264",
        has_audio=False,
        encoder_used="h264_amf",
        start_latency_ms=32.5,
    )


def test_trimmer_window_direct_save_releases_media_before_emit(qtbot, tmp_path: Path) -> None:
    raw = tmp_path / "temp_raw.mkv"
    raw.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 128)
    out_dir = tmp_path / "exports"

    capture = _make_capture_result(raw, duration_sec=5.0)
    window = TrimmerWindow(capture_result=capture, output_dir=out_dir)
    qtbot.addWidget(window)

    assert window.range_slider.duration_ms() == 5000
    assert not window.player.source().isEmpty()
    assert window.direct_save_button.text() == "Direkt Kaydet"

    observed: list[tuple[TrimRequest, bool]] = []
    window.exportRequested.connect(
        lambda req: observed.append((req, window.player.source().isEmpty()))
    )

    qtbot.mouseClick(window.direct_save_button, Qt.MouseButton.LeftButton)

    assert len(observed) == 1
    req, media_cleared_before_signal = observed[0]
    assert media_cleared_before_signal is True
    assert req.source_path == raw
    assert req.output_dir == out_dir
    assert req.start_time_sec == 0.0
    assert req.end_time_sec == 5.0
    assert req.direct_save is True
    assert req.cleanup_source is True


def test_trimmer_window_trim_and_save_uses_slider_timestamps(qtbot, tmp_path: Path) -> None:
    raw = tmp_path / "temp_raw.mkv"
    raw.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 128)
    out_dir = tmp_path / "exports"

    window = TrimmerWindow()
    qtbot.addWidget(window)
    assert not window.direct_save_button.isEnabled()

    window.load_capture(_make_capture_result(raw, duration_sec=6.0), output_dir=out_dir)
    assert window.trim_save_button.isEnabled()
    assert window.trim_save_button.text() == "Kırp & Kaydet"

    window.range_slider.set_range(1250, 4500)
    assert "00:01.25" in window.start_time_label.text()
    assert "00:04.50" in window.end_time_label.text()
    assert "00:03.25" in window.selection_duration_label.text()

    observed: list[tuple[TrimRequest, bool]] = []
    window.exportRequested.connect(
        lambda req: observed.append((req, window.player.source().isEmpty()))
    )

    qtbot.mouseClick(window.trim_save_button, Qt.MouseButton.LeftButton)

    assert len(observed) == 1
    req, media_cleared = observed[0]
    assert media_cleared is True
    assert req.source_path == raw
    assert req.output_dir == out_dir
    assert req.start_time_sec == pytest.approx(1.25)
    assert req.end_time_sec == pytest.approx(4.5)
    assert req.direct_save is False
    assert req.cleanup_source is True


def test_trimmer_window_discard_and_feedback_helpers(qtbot, tmp_path: Path) -> None:
    raw = tmp_path / "temp_raw.mkv"
    raw.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 128)
    out_dir = tmp_path / "exports"

    window = TrimmerWindow(_make_capture_result(raw, duration_sec=3.0), output_dir=out_dir)
    qtbot.addWidget(window)

    discarded: list[tuple[Path, bool]] = []
    window.discardRequested.connect(
        lambda p: discarded.append((p, window.player.source().isEmpty()))
    )

    qtbot.mouseClick(window.discard_button, Qt.MouseButton.LeftButton)
    assert discarded == [(raw, True)]

    # Non-blocking progress and result feedback.
    window.set_exporting(True, "Dışa aktarılıyor...")
    assert not window.direct_save_button.isEnabled()
    assert not window.trim_save_button.isEnabled()
    assert window.status_label.text() == "Dışa aktarılıyor..."

    ok_result = TrimResult(
        success=True,
        output_path=out_dir / "QuickClip_2026-11-16_17-45-30.mp4",
        duration_sec=2.5,
        elapsed_ms=48.0,
        source_cleaned=True,
    )
    window.show_export_result(ok_result)
    assert window.direct_save_button.isEnabled()
    assert "QuickClip_2026-11-16_17-45-30.mp4" in window.status_label.text()

    err_result = TrimResult(
        success=False,
        output_path=None,
        duration_sec=0.0,
        elapsed_ms=10.0,
        source_cleaned=False,
        error="ffmpeg error",
    )
    window.show_export_result(err_result)
    assert window.direct_save_button.isEnabled()
    assert "ffmpeg error" in window.status_label.text()


def test_trimmer_load_capture_cancels_pending_auto_close(qtbot, tmp_path: Path) -> None:
    raw1 = tmp_path / "temp_raw_1.mkv"
    raw2 = tmp_path / "temp_raw_2.mkv"
    raw1.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 128)
    raw2.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 128)
    out_dir = tmp_path / "exports"

    window = TrimmerWindow(_make_capture_result(raw1, duration_sec=2.0), output_dir=out_dir)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)

    closed_count = 0

    def _on_closed() -> None:
        nonlocal closed_count
        closed_count += 1

    window.closed.connect(_on_closed)

    ok_result = TrimResult(
        success=True,
        output_path=out_dir / "QuickClip_1.mp4",
        duration_sec=2.0,
        elapsed_ms=25.0,
        source_cleaned=True,
    )
    window.show_export_result(ok_result, auto_close_ms=120)
    # Loading a second capture immediately must cancel the stale auto-close timer.
    window.load_capture(_make_capture_result(raw2, duration_sec=3.0), output_dir=out_dir)

    qtbot.wait(220)
    assert window.isVisible() is True
    assert closed_count == 0

    # Also verify set_exporting cancels a pending auto-close timer.
    window.show_export_result(ok_result, auto_close_ms=120)
    window.set_exporting(True, "Yeniden aktarılıyor...")
    qtbot.wait(220)
    assert window.isVisible() is True
    assert closed_count == 0

    # When not cancelled, auto_close_ms closes the window cleanly.
    window.show_export_result(ok_result, auto_close_ms=80)
    qtbot.waitUntil(lambda: closed_count == 1 and not window.isVisible(), timeout=1500)


def test_trimmer_release_media_unlocks_real_mkv_on_windows(qtbot, tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not installed on PATH")

    raw = tmp_path / "temp_raw.mkv"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=navy:s=320x240:d=1",
            "-c:v",
            "libx264",
            str(raw),
        ],
        check=True,
        capture_output=True,
    )

    window = TrimmerWindow(_make_capture_result(raw, duration_sec=1.0), output_dir=tmp_path)
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(100)

    assert not window.player.source().isEmpty()
    window.release_media()
    assert window.player.source().isEmpty()

    # Windows must permit immediate deletion after release_media().
    raw.unlink()
    assert not raw.exists()


def test_trimmer_playback_toggle_and_close_releases_media(qtbot, tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not installed on PATH")

    raw = tmp_path / "playback.mkv"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=320x240:d=1.5",
            "-c:v",
            "libx264",
            str(raw),
        ],
        check=True,
        capture_output=True,
    )

    window = TrimmerWindow(_make_capture_result(raw, duration_sec=1.5), output_dir=tmp_path)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)

    window.range_slider.set_range(200, 600)
    window.toggle_playback()
    qtbot.waitUntil(
        lambda: window.player.playbackState().name == "PlayingState",
        timeout=2000,
    )
    assert window.play_pause_button.text() == "Duraklat"

    # Wait until playback reaches end_ms (600ms) and pauses cleanly.
    qtbot.waitUntil(
        lambda: window.player.playbackState().name == "PausedState",
        timeout=3000,
    )
    assert window.play_pause_button.text() == "Oynat"
    assert window.player.position() >= 550

    # Spacebar shortcut restarts playback cleanly.
    qtbot.keyClick(window, Qt.Key.Key_Space)
    qtbot.waitUntil(
        lambda: window.player.playbackState().name == "PlayingState",
        timeout=2000,
    )
    qtbot.keyClick(window, Qt.Key.Key_Space)
    qtbot.waitUntil(
        lambda: window.player.playbackState().name == "PausedState",
        timeout=2000,
    )

    with qtbot.waitSignal(window.closed, timeout=1000):
        window.close()
    assert window.player.source().isEmpty()
    raw.unlink()
    assert not raw.exists()


def test_standby_window_states_clock_and_signals(qtbot, monkeypatch, tmp_path: Path) -> None:
    # Ensure headless-safe tray guard works cleanly even when tray is unavailable.
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))

    window = StandbyWindow(hotkey="F9", output_dir=tmp_path)
    qtbot.addWidget(window)

    assert window.tray_icon is None
    assert window.state == CaptureState.COLD
    assert window.hotkey_label.text() == "F9"
    assert str(tmp_path) in window.output_dir_label.text()

    # Transition through STANDBY -> RECORDING -> STOPPING -> STANDBY.
    window.set_state(CaptureState.WARMING, "Başlatılıyor...")
    assert not window.record_button.isEnabled()
    assert not window.quick_clip_button.isEnabled()

    window.set_state(CaptureState.STANDBY, "h264_amf • 60 fps")
    assert window.state == CaptureState.STANDBY
    assert "STANDBY" in window.state_badge_label.text()
    assert window.record_button.text() == "Kaydı Başlat"
    assert window.record_button.isEnabled()
    assert window.quick_clip_button.isEnabled()
    assert window.encoder_label.text() == "h264_amf • 60 fps"

    # Quick clip button emits 5.0 seconds.
    with qtbot.waitSignal(window.quickClipRequested, timeout=1000) as quick_sig:
        qtbot.mouseClick(window.quick_clip_button, Qt.MouseButton.LeftButton)
    assert quick_sig.args == [5.0]

    # Record button emits recordToggleRequested.
    with qtbot.waitSignal(window.recordToggleRequested, timeout=1000):
        qtbot.mouseClick(window.record_button, Qt.MouseButton.LeftButton)

    window.set_state(CaptureState.RECORDING)
    assert window.state == CaptureState.RECORDING
    assert "RECORDING" in window.state_badge_label.text()
    assert window.record_button.text() == "Kaydı Durdur"
    assert window.record_button.isEnabled()
    assert not window.quick_clip_button.isEnabled()

    # Live timer advances while RECORDING.
    qtbot.wait(250)
    assert window.elapsed_seconds() > 0.1
    assert window.elapsed_label.text() != "00:00.0"

    window.set_state(CaptureState.STOPPING)
    assert window.state == CaptureState.STOPPING
    assert not window.record_button.isEnabled()
    assert not window.quick_clip_button.isEnabled()

    window.set_state(CaptureState.STANDBY)
    assert window.elapsed_seconds() == 0.0
    assert window.elapsed_label.text() == "00:00.0"

    # Orchestrator helper slots.
    new_dir = tmp_path / "clips"
    window.set_hotkey_label("Ctrl+Shift+R")
    window.set_output_dir(new_dir)
    window.set_status_message("Kayıt tamamlandı")
    assert window.hotkey_label.text() == "Ctrl+Shift+R"
    assert str(new_dir) in window.output_dir_label.text()
    assert window.status_message_label.text() == "Kayıt tamamlandı"

    # ERROR state shows error detail in status message and keeps recovery enabled.
    window.set_state(CaptureState.ERROR, "Kamera bulunamadı")
    assert window.state == CaptureState.ERROR
    assert "ERROR" in window.state_badge_label.text()
    assert window.status_message_label.text() == "Kamera bulunamadı"
    assert window.record_button.isEnabled()

    # Quit signal emitted cleanly on quit button.
    with qtbot.waitSignal(window.quitRequested, timeout=1000):
        qtbot.mouseClick(window.quit_button, Qt.MouseButton.LeftButton)


def test_standby_window_close_emits_quit(qtbot, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))
    window = StandbyWindow(hotkey="F9", output_dir=tmp_path)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)

    with qtbot.waitSignal(window.quitRequested, timeout=1000):
        window.close()


def test_window_icons_and_tray_overlay_badge(qtbot, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))

    standby = StandbyWindow(hotkey="F9", output_dir=tmp_path)
    trimmer = TrimmerWindow(output_dir=tmp_path)
    qtbot.addWidget(standby)
    qtbot.addWidget(trimmer)

    assert not standby.windowIcon().isNull()
    assert not trimmer.windowIcon().isNull()

    # Overlay dot on bundled icon renders non-null 32x32 pixmap.
    overlay_icon = _build_tray_icon("#ef4444")
    assert not overlay_icon.isNull()
    assert not overlay_icon.pixmap(QSize(32, 32)).isNull()

    # Fallback painted badge works cleanly even if icon asset is unavailable.
    fallback_icon = _build_tray_icon("#10b981", base_icon=QIcon())
    assert not fallback_icon.isNull()
    assert not fallback_icon.pixmap(QSize(32, 32)).isNull()


def test_standby_startup_checkbox_sync_vs_user_toggle(qtbot, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))

    window = StandbyWindow(hotkey="F9", output_dir=tmp_path)
    qtbot.addWidget(window)

    assert window.startup_checkbox.text() == "Windows ile Başlat"
    assert window.is_run_at_startup_checked() is False

    emitted: list[bool] = []
    window.startupToggled.connect(emitted.append)

    # Programmatic sync uses QSignalBlocker and must NOT emit startupToggled.
    window.set_run_at_startup_checked(True)
    assert window.is_run_at_startup_checked() is True
    window.set_run_at_startup_checked(False)
    assert window.is_run_at_startup_checked() is False
    assert emitted == []

    # User toggle emits startupToggled with the new boolean state.
    qtbot.mouseClick(window.startup_checkbox, Qt.MouseButton.LeftButton)
    assert window.is_run_at_startup_checked() is True
    assert emitted == [True]

    qtbot.mouseClick(window.startup_checkbox, Qt.MouseButton.LeftButton)
    assert window.is_run_at_startup_checked() is False
    assert emitted == [True, False]


def test_standby_monitor_selector_signals_and_state_gating(qtbot, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))

    window = StandbyWindow(hotkey="F9", output_dir=tmp_path)
    qtbot.addWidget(window)

    monitors = [
        MonitorInfo(index=1, name="PrimaryDisp", width=3840, height=2160, left=0, top=0, is_primary=True),
        MonitorInfo(index=2, name="SideDisp", width=1920, height=1080, left=-1920, top=0, is_primary=False),
    ]
    changed: list[int] = []
    refreshed: list[bool] = []
    window.monitorChanged.connect(changed.append)
    window.monitorRefreshRequested.connect(lambda: refreshed.append(True))

    # Programmatic population/selection must NOT emit monitorChanged.
    window.set_monitors(monitors, selected_index=1)
    assert window.monitor_combo.count() == 2
    assert window.selected_monitor_index() == 1
    window.set_selected_monitor(2)
    assert window.selected_monitor_index() == 2
    window.set_selected_monitor(1)
    assert changed == []

    # Interactive selection emits monitorChanged(2).
    window.monitor_combo.setCurrentIndex(1)
    assert window.selected_monitor_index() == 2
    assert changed == [2]

    # Refresh button emits monitorRefreshRequested.
    qtbot.mouseClick(window.refresh_monitors_button, Qt.MouseButton.LeftButton)
    assert refreshed == [True]

    # Switching monitor is disabled while warming, recording, or stopping.
    for blocked_state in (CaptureState.WARMING, CaptureState.RECORDING, CaptureState.STOPPING):
        window.set_state(blocked_state)
        assert window.monitor_combo.isEnabled() is False
        assert window.refresh_monitors_button.isEnabled() is False

    window.set_state(CaptureState.STANDBY)
    assert window.monitor_combo.isEnabled() is True
    assert window.refresh_monitors_button.isEnabled() is True


def test_hotkey_conversion_and_standby_settings_controls(qtbot, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))

    assert qt_key_event_to_hotkey(Qt.Key.Key_F8, Qt.KeyboardModifier.NoModifier) == "F8"
    assert (
        qt_key_event_to_hotkey(
            Qt.Key.Key_R,
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
        )
        == "Ctrl+Shift+R"
    )
    assert qt_key_event_to_hotkey(Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier) is None
    assert qt_key_event_to_hotkey(Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier) is None
    assert qt_key_event_to_hotkey(Qt.Key.Key_Control, Qt.KeyboardModifier.ControlModifier) is None

    window = StandbyWindow(hotkey="F9", output_dir=tmp_path)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)

    hotkeys: list[str] = []
    out_dirs: list[Path] = []
    modes: list[bool] = []
    window.hotkeyChanged.connect(hotkeys.append)
    window.outputDirChanged.connect(out_dirs.append)
    window.captureModeChanged.connect(modes.append)

    # Hotkey capture: Escape cancels without emitting.
    qtbot.mouseClick(window.change_hotkey_button, Qt.MouseButton.LeftButton)
    assert window.is_capturing_hotkey() is True
    qtbot.keyClick(window, Qt.Key.Key_Escape)
    assert window.is_capturing_hotkey() is False
    assert hotkeys == []

    # Hotkey capture: pressing F8 emits hotkeyChanged("F8").
    qtbot.mouseClick(window.change_hotkey_button, Qt.MouseButton.LeftButton)
    assert window.is_capturing_hotkey() is True
    qtbot.keyClick(window, Qt.Key.Key_F8)
    assert window.is_capturing_hotkey() is False
    assert hotkeys == ["F8"]

    # Output directory picker emits outputDirChanged(Path).
    chosen_dir = tmp_path / "custom_clips"
    monkeypatch.setattr(
        QFileDialog,
        "getExistingDirectory",
        staticmethod(lambda *_args, **_kwargs: str(chosen_dir)),
    )
    qtbot.mouseClick(window.change_output_dir_button, Qt.MouseButton.LeftButton)
    assert out_dirs == [chosen_dir]
    assert str(chosen_dir) in window.output_dir_label.text()

    # Mode toggle switches between Full Screen and Region Selection.
    assert window.is_region_mode() is False
    assert window.record_button.text() == "Kaydı Başlat"
    qtbot.mouseClick(window.region_mode_button, Qt.MouseButton.LeftButton)
    assert window.is_region_mode() is True
    assert window.record_button.text() == "Alan Seç & Kaydet"
    assert modes == [True]

    # Controls are locked during active recording.
    window.set_state(CaptureState.RECORDING)
    assert window.change_hotkey_button.isEnabled() is False
    assert window.change_output_dir_button.isEnabled() is False
    assert window.region_mode_button.isEnabled() is False
    assert window.fullscreen_mode_button.isEnabled() is False


def test_region_selector_overlay_dpi_conversion_drag_and_cancel(qtbot) -> None:
    monitor = MonitorInfo(
        index=1,
        name="4KDisp",
        width=3840,
        height=2160,
        left=0,
        top=0,
        is_primary=True,
    )
    # 200% DPI scaling: logical 1920x1080 overlay -> physical 3840x2160 monitor.
    mapped = logical_rect_to_capture_region(
        QRect(100, 50, 300, 200),
        QSize(1920, 1080),
        monitor,
    )
    assert mapped == CaptureRegion(left=200, top=100, width=600, height=400, monitor_index=1)

    overlay = RegionSelectorOverlay(monitor=monitor, screen_geometry=QRect(0, 0, 640, 360))
    qtbot.addWidget(overlay)
    overlay.resize(640, 360)

    selected: list[CaptureRegion] = []
    cancelled: list[bool] = []
    overlay.regionSelected.connect(selected.append)
    overlay.selectionCancelled.connect(lambda: cancelled.append(True))

    # Tiny click is ignored without emitting.
    overlay.start_selection()
    _send_mouse(overlay, QEvent.Type.MouseButtonPress, 40, 40)
    _send_mouse(overlay, QEvent.Type.MouseButtonRelease, 45, 45)
    assert selected == []
    assert cancelled == []

    # Valid drag emits scaled physical CaptureRegion and hides overlay.
    _send_mouse(overlay, QEvent.Type.MouseButtonPress, 100, 60)
    _send_mouse(overlay, QEvent.Type.MouseMove, 260, 150)
    _send_mouse(overlay, QEvent.Type.MouseButtonRelease, 260, 150)
    assert len(selected) == 1
    assert selected[0].monitor_index == 1
    assert selected[0].width == 960
    assert selected[0].height == 540
    assert overlay.isVisible() is False

    # Escape cancels cleanly.
    overlay.start_selection()
    qtbot.keyClick(overlay, Qt.Key.Key_Escape)
    assert cancelled == [True]
    assert overlay.isVisible() is False
