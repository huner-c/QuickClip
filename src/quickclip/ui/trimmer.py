"""Minimalist video trimmer window for previewing and exporting QuickClip captures."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from quickclip.contracts import AppConfig, CaptureResult, TrimRequest, TrimResult
from quickclip.ui._icons import load_app_icon
from quickclip.ui.range_slider import RangeSlider


def format_time_ms(ms: int) -> str:
    """Format a millisecond timestamp as ``MM:SS.dd`` (or ``H:MM:SS.dd`` when >= 1 hour)."""
    clamped = max(0, int(ms))
    total_centis = clamped // 10
    centis = total_centis % 100
    total_seconds = total_centis // 100
    seconds = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}.{centis:02d}"
    return f"{minutes:02d}:{seconds:02d}.{centis:02d}"


_TRIMMER_STYLESHEET = """
QMainWindow, QWidget#trimmerRoot {
    background-color: #12141a;
    color: #e2e8f0;
    font-family: "Segoe UI", sans-serif;
}
QLabel#metaLabel {
    color: #94a3b8;
    font-size: 12px;
}
QLabel#outputLabel {
    color: #64748b;
    font-size: 11px;
}
QFrame#videoFrame {
    background-color: #090b0f;
    border: 1px solid #262c38;
    border-radius: 8px;
}
QLabel.timeValue {
    color: #cbd5e1;
    font-family: "Consolas", "Cascadia Mono", monospace;
    font-size: 12px;
    background-color: #1a1e28;
    border: 1px solid #2a3140;
    border-radius: 4px;
    padding: 3px 8px;
}
QLabel#selectionLabel {
    color: #60a5fa;
    font-weight: 600;
}
QPushButton {
    background-color: #1f2430;
    color: #e2e8f0;
    border: 1px solid #333b4d;
    border-radius: 6px;
    padding: 7px 14px;
    font-size: 13px;
    font-weight: 500;
}
QPushButton:hover:enabled {
    background-color: #293040;
    border-color: #475569;
}
QPushButton:pressed:enabled {
    background-color: #1a1f29;
}
QPushButton:disabled {
    color: #64748b;
    background-color: #171a22;
    border-color: #232834;
}
QPushButton#playButton {
    background-color: #2563eb;
    border-color: #3b82f6;
    color: #ffffff;
    min-width: 92px;
}
QPushButton#playButton:hover:enabled {
    background-color: #1d4ed8;
}
QPushButton#loopButton:checked {
    background-color: #1e3a5f;
    border-color: #3b82f6;
    color: #93c5fd;
}
QPushButton#directSaveButton {
    background-color: #059669;
    border-color: #10b981;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#directSaveButton:hover:enabled {
    background-color: #047857;
}
QPushButton#trimSaveButton {
    background-color: #2563eb;
    border-color: #3b82f6;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#trimSaveButton:hover:enabled {
    background-color: #1d4ed8;
}
QPushButton#discardButton {
    background-color: #26191c;
    border-color: #7f1d1d;
    color: #fca5a5;
}
QPushButton#discardButton:hover:enabled {
    background-color: #7f1d1d;
    color: #ffffff;
}
QLabel#statusBanner {
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 12px;
    font-weight: 500;
}
"""


class TrimmerWindow(QMainWindow):
    """Minimalist video preview and trim window opened after capture completes."""

    exportRequested = Signal(TrimRequest)
    discardRequested = Signal(Path)
    closed = Signal()

    def __init__(
        self,
        capture_result: CaptureResult | None = None,
        output_dir: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._capture_result: CaptureResult | None = None
        self._output_dir: Path = Path(output_dir) if output_dir is not None else AppConfig().output_dir
        self._suppress_range_preview: bool = False
        self._last_range: tuple[int, int] = (0, 0)
        self._busy: bool = False

        self.setWindowTitle("QuickClip — Kırp & Kaydet")
        app_icon = load_app_icon()
        if not app_icon.isNull():
            self.setWindowIcon(app_icon)
        self.setMinimumSize(820, 560)
        self.setStyleSheet(_TRIMMER_STYLESHEET)

        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._video_widget = QVideoWidget(self)
        self._player.setAudioOutput(self._audio_output)
        self._player.setVideoOutput(self._video_widget)

        self._auto_close_timer = QTimer(self)
        self._auto_close_timer.setSingleShot(True)
        self._auto_close_timer.timeout.connect(self.close)

        self._build_ui()
        self._connect_signals()
        self._update_controls_enabled()

        if capture_result is not None:
            self.load_capture(capture_result, output_dir=self._output_dir)

    @property
    def player(self) -> QMediaPlayer:
        """Return the underlying Qt media player instance."""
        return self._player

    @property
    def audio_output(self) -> QAudioOutput:
        """Return the audio output attached to the media player."""
        return self._audio_output

    @property
    def video_widget(self) -> QVideoWidget:
        """Return the video preview surface widget."""
        return self._video_widget

    @property
    def range_slider(self) -> RangeSlider:
        """Return the dual-handle trim timeline slider."""
        return self._range_slider

    @property
    def capture_result(self) -> CaptureResult | None:
        """Return the currently loaded capture metadata, if any."""
        return self._capture_result

    @property
    def output_dir(self) -> Path:
        """Return the destination folder used for exported clips."""
        return self._output_dir

    def _build_ui(self) -> None:
        root = QWidget(self)
        root.setObjectName("trimmerRoot")
        self.setCentralWidget(root)

        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(10)

        # Header metadata row.
        header_row = QHBoxLayout()
        header_row.setSpacing(10)

        self.info_label = QLabel("Klip yüklenmedi", root)
        self.info_label.setObjectName("metaLabel")
        header_row.addWidget(self.info_label, 1)

        self.output_dir_label = QLabel(f"Klasör: {self._output_dir}", root)
        self.output_dir_label.setObjectName("outputLabel")
        self.output_dir_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header_row.addWidget(self.output_dir_label, 0)
        layout.addLayout(header_row)

        # Video preview container.
        video_frame = QFrame(root)
        video_frame.setObjectName("videoFrame")
        video_layout = QVBoxLayout(video_frame)
        video_layout.setContentsMargins(2, 2, 2, 2)
        self._video_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._video_widget.setMinimumHeight(320)
        video_layout.addWidget(self._video_widget)
        layout.addWidget(video_frame, 1)

        # Formatted time indicators.
        time_row = QHBoxLayout()
        time_row.setSpacing(8)

        self.start_time_label = QLabel("Başlangıç: 00:00.00", root)
        self.start_time_label.setProperty("class", "timeValue")
        time_row.addWidget(self.start_time_label)

        self.current_time_label = QLabel("Konum: 00:00.00", root)
        self.current_time_label.setProperty("class", "timeValue")
        time_row.addWidget(self.current_time_label)

        time_row.addStretch(1)

        self.selection_duration_label = QLabel("Seçim: 00:00.00", root)
        self.selection_duration_label.setObjectName("selectionLabel")
        self.selection_duration_label.setProperty("class", "timeValue")
        time_row.addWidget(self.selection_duration_label)

        self.end_time_label = QLabel("Bitiş: 00:00.00", root)
        self.end_time_label.setProperty("class", "timeValue")
        time_row.addWidget(self.end_time_label)
        layout.addLayout(time_row)

        # Timeline range slider.
        self._range_slider = RangeSlider(root)
        layout.addWidget(self._range_slider)

        # Playback + Export actions row.
        action_row = QHBoxLayout()
        action_row.setSpacing(8)

        self.play_pause_button = QPushButton("Oynat", root)
        self.play_pause_button.setObjectName("playButton")
        self.play_pause_button.setToolTip("Oynat / Duraklat (Boşluk)")
        self.play_pause_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        action_row.addWidget(self.play_pause_button)

        self.loop_button = QPushButton("Döngü", root)
        self.loop_button.setObjectName("loopButton")
        self.loop_button.setCheckable(True)
        self.loop_button.setToolTip("Seçili aralığı sürekli tekrar oynat")
        self.loop_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        action_row.addWidget(self.loop_button)

        action_row.addStretch(1)

        self.discard_button = QPushButton("İptal / Sil", root)
        self.discard_button.setObjectName("discardButton")
        self.discard_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        action_row.addWidget(self.discard_button)

        self.trim_save_button = QPushButton("Kırp & Kaydet", root)
        self.trim_save_button.setObjectName("trimSaveButton")
        self.trim_save_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        action_row.addWidget(self.trim_save_button)

        self.direct_save_button = QPushButton("Direkt Kaydet", root)
        self.direct_save_button.setObjectName("directSaveButton")
        self.direct_save_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        action_row.addWidget(self.direct_save_button)

        layout.addLayout(action_row)

        # Non-blocking status banner.
        self.status_label = QLabel("", root)
        self.status_label.setObjectName("statusBanner")
        self.status_label.setWordWrap(True)
        self.status_label.setVisible(False)
        layout.addWidget(self.status_label)

        # Spacebar toggles preview playback anywhere in the window.
        self._space_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        self._space_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)

    def _connect_signals(self) -> None:
        self.play_pause_button.clicked.connect(self.toggle_playback)
        self._space_shortcut.activated.connect(self.toggle_playback)

        self.direct_save_button.clicked.connect(self._on_direct_save_clicked)
        self.trim_save_button.clicked.connect(self._on_trim_save_clicked)
        self.discard_button.clicked.connect(self._on_discard_clicked)

        self._range_slider.rangeChanged.connect(self._on_slider_range_changed)
        self._range_slider.seekRequested.connect(self._on_slider_seek_requested)

        self._player.positionChanged.connect(self._on_player_position_changed)
        self._player.durationChanged.connect(self._on_player_duration_changed)
        self._player.playbackStateChanged.connect(self._on_player_state_changed)
        self._player.mediaStatusChanged.connect(self._on_player_media_status_changed)
        self._player.errorOccurred.connect(self._on_player_error)

    def load_capture(
        self,
        capture_result: CaptureResult,
        output_dir: Path | None = None,
    ) -> None:
        """Load a newly finished ``CaptureResult`` into the preview player and timeline."""
        self._auto_close_timer.stop()
        self.release_media()
        self._capture_result = capture_result
        if output_dir is not None:
            self._output_dir = Path(output_dir)
            self.output_dir_label.setText(f"Klasör: {self._output_dir}")

        duration_ms = max(0, round(capture_result.duration_sec * 1000)) if capture_result.duration_sec > 0 else 0
        self._suppress_range_preview = True
        try:
            self._range_slider.set_duration(duration_ms)
            if duration_ms > 0:
                self._range_slider.set_range(0, duration_ms)
            self._range_slider.set_position(0)
            self._last_range = (self._range_slider.start_ms(), self._range_slider.end_ms())
        finally:
            self._suppress_range_preview = False

        self._refresh_time_labels(self._range_slider.start_ms(), 0, self._range_slider.end_ms())

        audio_tag = "Sesli" if capture_result.has_audio else "Sessiz"
        self.info_label.setText(
            f"{capture_result.width}x{capture_result.height} • {capture_result.fps:.0f} fps • "
            f"{capture_result.encoder_used} • {audio_tag}"
        )
        self._audio_output.setMuted(not capture_result.has_audio)

        if capture_result.error:
            self._set_status_banner(f"Kayıt hatası: {capture_result.error}", kind="error")
        else:
            self._clear_status_banner()
            self._player.setSource(QUrl.fromLocalFile(str(capture_result.raw_file_path)))

        self._busy = False
        self._update_controls_enabled()

    @Slot()
    def release_media(self) -> None:
        """Stop playback and clear source URL so Windows releases file locks on ``temp_raw.mkv``."""
        self._player.stop()
        self._player.setSource(QUrl())
        self.play_pause_button.setText("Oynat")

    @Slot()
    def toggle_playback(self) -> None:
        """Toggle preview playback within the selected trim bounds."""
        if self._busy or self._capture_result is None or self._capture_result.error:
            return
        if self._player.source().isEmpty():
            return

        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
            return

        start_ms = self._range_slider.start_ms()
        end_ms = self._range_slider.end_ms()
        pos_ms = self._player.position()
        if pos_ms < start_ms or (end_ms > 0 and pos_ms >= end_ms - 40):
            self._player.setPosition(start_ms)
            self._range_slider.set_position(start_ms)
            self.current_time_label.setText(f"Konum: {format_time_ms(start_ms)}")

        self._player.play()

    def set_exporting(self, busy: bool, message: str = "") -> None:
        """Enable/disable interactive controls and display non-blocking export progress."""
        self._auto_close_timer.stop()
        self._busy = bool(busy)
        self._update_controls_enabled()
        if self._busy:
            self._set_status_banner(message or "Klip dışa aktarılıyor...", kind="info")
        elif message:
            self._set_status_banner(message, kind="info")

    def show_export_result(self, result: TrimResult, *, auto_close_ms: int = 0) -> None:
        """Display the export outcome in the status banner and re-enable controls."""
        self._auto_close_timer.stop()
        self._busy = False
        self._update_controls_enabled()

        if result.success:
            name = result.output_path.name if result.output_path is not None else "clip.mp4"
            self._set_status_banner(
                f"Kaydedildi: {name} ({result.duration_sec:.2f} sn, {result.elapsed_ms:.0f} ms)",
                kind="success",
            )
            if auto_close_ms > 0:
                self._auto_close_timer.start(auto_close_ms)
        else:
            err = result.error or "Bilinmeyen dışa aktarma hatası."
            self._set_status_banner(f"Dışa aktarma başarısız: {err}", kind="error")

    def closeEvent(self, event: QCloseEvent) -> None:
        """Ensure QMediaPlayer releases file handles before closing the window."""
        self._auto_close_timer.stop()
        self.release_media()
        self.closed.emit()
        super().closeEvent(event)

    def _update_controls_enabled(self) -> None:
        has_capture = self._capture_result is not None
        can_export = has_capture and not self._busy and not bool(self._capture_result.error)
        can_discard = has_capture and not self._busy

        self.play_pause_button.setEnabled(can_export)
        self.loop_button.setEnabled(can_export)
        self._range_slider.setEnabled(can_export)
        self.direct_save_button.setEnabled(can_export)
        self.trim_save_button.setEnabled(can_export)
        self.discard_button.setEnabled(can_discard)

    def _refresh_time_labels(self, start_ms: int, current_ms: int, end_ms: int) -> None:
        span_ms = max(0, end_ms - start_ms)
        self.start_time_label.setText(f"Başlangıç: {format_time_ms(start_ms)}")
        self.current_time_label.setText(f"Konum: {format_time_ms(current_ms)}")
        self.end_time_label.setText(f"Bitiş: {format_time_ms(end_ms)}")
        self.selection_duration_label.setText(f"Seçim: {format_time_ms(span_ms)}")

    def _set_status_banner(self, text: str, *, kind: str) -> None:
        palette = {
            "info": ("#1e293b", "#38bdf8", "#e0f2fe"),
            "success": ("#064e3b", "#10b981", "#d1fae5"),
            "error": ("#450a0a", "#ef4444", "#fee2e2"),
        }
        bg, border, fg = palette.get(kind, palette["info"])
        self.status_label.setStyleSheet(
            f"background-color: {bg}; border: 1px solid {border}; color: {fg};"
        )
        self.status_label.setText(text)
        self.status_label.setVisible(True)

    def _clear_status_banner(self) -> None:
        self.status_label.setText("")
        self.status_label.setVisible(False)

    @Slot()
    def _on_direct_save_clicked(self) -> None:
        if self._capture_result is None or self._busy:
            return
        duration_sec = (
            self._capture_result.duration_sec
            if self._capture_result.duration_sec > 0
            else self._range_slider.duration_ms() / 1000.0
        )
        request = TrimRequest(
            source_path=self._capture_result.raw_file_path,
            start_time_sec=0.0,
            end_time_sec=duration_sec,
            output_dir=self._output_dir,
            direct_save=True,
            cleanup_source=True,
        )
        self.release_media()
        self.exportRequested.emit(request)

    @Slot()
    def _on_trim_save_clicked(self) -> None:
        if self._capture_result is None or self._busy:
            return
        start_sec = self._range_slider.start_ms() / 1000.0
        end_sec = self._range_slider.end_ms() / 1000.0
        request = TrimRequest(
            source_path=self._capture_result.raw_file_path,
            start_time_sec=start_sec,
            end_time_sec=end_sec,
            output_dir=self._output_dir,
            direct_save=False,
            cleanup_source=True,
        )
        self.release_media()
        self.exportRequested.emit(request)

    @Slot()
    def _on_discard_clicked(self) -> None:
        if self._capture_result is None or self._busy:
            return
        source_path = self._capture_result.raw_file_path
        self.release_media()
        self.discardRequested.emit(source_path)

    @Slot(int, int)
    def _on_slider_range_changed(self, start_ms: int, end_ms: int) -> None:
        old_start, old_end = self._last_range
        self._last_range = (start_ms, end_ms)
        self._refresh_time_labels(start_ms, self._range_slider.position_ms(), end_ms)

        if (
            not self._suppress_range_preview
            and self._player.playbackState() != QMediaPlayer.PlaybackState.PlayingState
            and self._player.mediaStatus()
            in (
                QMediaPlayer.MediaStatus.LoadedMedia,
                QMediaPlayer.MediaStatus.BufferedMedia,
            )
        ):
            preview_ms = end_ms if (end_ms != old_end and start_ms == old_start) else start_ms
            self._player.setPosition(preview_ms)
            self._range_slider.set_position(preview_ms)
            self.current_time_label.setText(f"Konum: {format_time_ms(preview_ms)}")

    @Slot(int)
    def _on_slider_seek_requested(self, position_ms: int) -> None:
        self.current_time_label.setText(f"Konum: {format_time_ms(position_ms)}")
        if not self._player.source().isEmpty():
            self._player.setPosition(position_ms)

    @Slot(int)
    def _on_player_position_changed(self, position_ms: int) -> None:
        if self._player.source().isEmpty():
            return
        self._range_slider.set_position(position_ms)
        self.current_time_label.setText(f"Konum: {format_time_ms(position_ms)}")

        end_ms = self._range_slider.end_ms()
        if (
            self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
            and end_ms > 0
            and position_ms >= end_ms
        ):
            if self.loop_button.isChecked():
                start_ms = self._range_slider.start_ms()
                self._player.setPosition(start_ms)
            else:
                self._player.pause()
                self._player.setPosition(end_ms)

    @Slot(int)
    def _on_player_duration_changed(self, duration_ms: int) -> None:
        if duration_ms <= 0 or self._player.source().isEmpty():
            return
        self._suppress_range_preview = True
        try:
            self._range_slider.set_duration(duration_ms)
            self._last_range = (self._range_slider.start_ms(), self._range_slider.end_ms())
        finally:
            self._suppress_range_preview = False
        self._refresh_time_labels(
            self._range_slider.start_ms(),
            self._range_slider.position_ms(),
            self._range_slider.end_ms(),
        )

    @Slot(QMediaPlayer.PlaybackState)
    def _on_player_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        self.play_pause_button.setText(
            "Duraklat" if state == QMediaPlayer.PlaybackState.PlayingState else "Oynat"
        )

    @Slot(QMediaPlayer.MediaStatus)
    def _on_player_media_status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia and not self._player.source().isEmpty():
            if self.loop_button.isChecked():
                self._player.setPosition(self._range_slider.start_ms())
                self._player.play()
            else:
                self._player.pause()

    @Slot(QMediaPlayer.Error, str)
    def _on_player_error(self, error: QMediaPlayer.Error, error_string: str) -> None:
        if self._player.source().isEmpty() or error == QMediaPlayer.Error.NoError:
            return
        if error_string:
            self._set_status_banner(f"Önizleme uyarısı: {error_string}", kind="error")
