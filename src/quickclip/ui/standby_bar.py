"""Lightweight Warm Standby control bar window and system tray integration."""

from __future__ import annotations

from pathlib import Path
import time

from PySide6.QtCore import QRectF, QSignalBlocker, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from quickclip.contracts import AppConfig, CaptureState
from quickclip.ui._icons import load_app_icon

_STATE_META: dict[CaptureState, tuple[str, str, str]] = {
    CaptureState.COLD: ("BEKLEMEDE (COLD)", "#334155", "#cbd5e1"),
    CaptureState.WARMING: ("HAZIRLANIYOR (WARMING)", "#92400e", "#fef3c7"),
    CaptureState.STANDBY: ("HAZIR (STANDBY)", "#065f46", "#d1fae5"),
    CaptureState.RECORDING: ("KAYITTA (RECORDING)", "#991b1b", "#fee2e2"),
    CaptureState.STOPPING: ("DURDURULUYOR (STOPPING)", "#9a3412", "#ffedd5"),
    CaptureState.ERROR: ("HATA (ERROR)", "#7f1d1d", "#fecaca"),
}

_STANDBY_STYLESHEET = """
QMainWindow, QWidget#standbyRoot {
    background-color: #12141a;
    color: #e2e8f0;
    font-family: "Segoe UI", sans-serif;
}
QFrame#cardFrame {
    background-color: #191d26;
    border: 1px solid #282f3e;
    border-radius: 8px;
}
QLabel#stateBadge {
    border-radius: 10px;
    padding: 4px 10px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.4px;
}
QLabel#elapsedClock {
    color: #f8fafc;
    font-family: "Consolas", "Cascadia Mono", monospace;
    font-size: 20px;
    font-weight: 700;
}
QLabel.metaKey {
    color: #94a3b8;
    font-size: 12px;
}
QLabel#hotkeyBadge {
    background-color: #252b38;
    border: 1px solid #3a4459;
    border-radius: 4px;
    color: #e2e8f0;
    font-family: "Consolas", monospace;
    font-size: 12px;
    font-weight: 700;
    padding: 2px 8px;
}
QLabel#statusMessage {
    color: #94a3b8;
    font-size: 12px;
}
QPushButton {
    background-color: #1f2430;
    color: #e2e8f0;
    border: 1px solid #333b4d;
    border-radius: 6px;
    padding: 8px 14px;
    font-size: 13px;
    font-weight: 600;
}
QPushButton:hover:enabled {
    background-color: #293040;
}
QPushButton:disabled {
    color: #64748b;
    background-color: #171a22;
    border-color: #232834;
}
QPushButton#recordButton {
    background-color: #2563eb;
    border-color: #3b82f6;
    color: #ffffff;
}
QPushButton#recordButton[recording="true"] {
    background-color: #dc2626;
    border-color: #ef4444;
    color: #ffffff;
}
QPushButton#quickClipButton {
    background-color: #0f766e;
    border-color: #14b8a6;
    color: #ffffff;
}
QCheckBox#startupCheckbox {
    color: #cbd5e1;
    font-size: 12px;
    spacing: 8px;
}
QCheckBox#startupCheckbox::indicator {
    width: 15px;
    height: 15px;
    border-radius: 4px;
    border: 1px solid #475569;
    background-color: #191d26;
}
QCheckBox#startupCheckbox::indicator:checked {
    background-color: #2563eb;
    border-color: #3b82f6;
}
"""


def _build_tray_icon(dot_color: str, *, base_icon: QIcon | None = None) -> QIcon:
    """Overlay a status dot onto the QuickClip icon, falling back to a painted badge."""
    icon = load_app_icon() if base_icon is None else base_icon
    if not icon.isNull():
        base_pixmap = icon.pixmap(QSize(32, 32))
        if not base_pixmap.isNull():
            canvas = QPixmap(QSize(32, 32))
            canvas.fill(Qt.GlobalColor.transparent)
            painter = QPainter(canvas)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawPixmap(0, 0, 32, 32, base_pixmap)

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#0f172a"))
            painter.drawEllipse(QRectF(17.5, 17.5, 13.5, 13.5))
            painter.setBrush(QColor(dot_color))
            painter.drawEllipse(QRectF(19.5, 19.5, 9.5, 9.5))
            painter.end()
            return QIcon(canvas)

    pixmap = QPixmap(QSize(32, 32))
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#1e293b"))
    painter.drawRoundedRect(2, 2, 28, 28, 7, 7)

    painter.setBrush(QColor(dot_color))
    painter.drawEllipse(9, 9, 14, 14)
    painter.end()
    return QIcon(pixmap)


def _format_elapsed_clock(seconds: float) -> str:
    """Format elapsed seconds as ``MM:SS.d``."""
    clamped = max(0.0, seconds)
    tenths_total = int(clamped * 10)
    tenths = tenths_total % 10
    total_sec = tenths_total // 10
    secs = total_sec % 60
    mins = total_sec // 60
    return f"{mins:02d}:{secs:02d}.{tenths}"


class StandbyWindow(QMainWindow):
    """Compact Warm Standby control window with optional system tray integration."""

    recordToggleRequested = Signal()
    quickClipRequested = Signal(float)
    startupToggled = Signal(bool)
    quitRequested = Signal()

    def __init__(
        self,
        hotkey: str = "F9",
        output_dir: Path | None = None,
        enable_tray: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._state: CaptureState = CaptureState.COLD
        self._hotkey: str = hotkey
        self._output_dir: Path = Path(output_dir) if output_dir is not None else AppConfig().output_dir
        self._record_started_monotonic: float | None = None
        self._quitting: bool = False

        self.setWindowTitle("QuickClip — Warm Standby")
        app_icon = load_app_icon()
        if not app_icon.isNull():
            self.setWindowIcon(app_icon)
        self.setFixedWidth(440)
        self.setStyleSheet(_STANDBY_STYLESHEET)

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(100)
        self._elapsed_timer.timeout.connect(self._on_elapsed_tick)

        self._tray_icon: QSystemTrayIcon | None = None
        self._tray_record_action: QAction | None = None

        self._build_ui()
        self._setup_tray(enable_tray=enable_tray)

        self.set_hotkey_label(self._hotkey)
        self.set_output_dir(self._output_dir)
        self.set_state(CaptureState.COLD)

    @property
    def state(self) -> CaptureState:
        """Return the current capture lifecycle state displayed by the window."""
        return self._state

    @property
    def tray_icon(self) -> QSystemTrayIcon | None:
        """Return the system tray icon instance, or ``None`` if unavailable/disabled."""
        return self._tray_icon

    def elapsed_seconds(self) -> float:
        """Return active recording elapsed seconds, or ``0.0`` when idle."""
        if self._record_started_monotonic is None:
            return 0.0
        return max(0.0, time.monotonic() - self._record_started_monotonic)

    def _build_ui(self) -> None:
        root = QWidget(self)
        root.setObjectName("standbyRoot")
        self.setCentralWidget(root)

        layout = QVBoxLayout(root)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        # Status card: State badge + live elapsed clock.
        status_card = QFrame(root)
        status_card.setObjectName("cardFrame")
        card_layout = QVBoxLayout(status_card)
        card_layout.setContentsMargins(12, 10, 12, 10)
        card_layout.setSpacing(8)

        top_row = QHBoxLayout()
        self.state_badge_label = QLabel(root)
        self.state_badge_label.setObjectName("stateBadge")
        top_row.addWidget(self.state_badge_label, 0, Qt.AlignmentFlag.AlignLeft)

        top_row.addStretch(1)

        self.elapsed_label = QLabel("00:00.0", root)
        self.elapsed_label.setObjectName("elapsedClock")
        top_row.addWidget(self.elapsed_label, 0, Qt.AlignmentFlag.AlignRight)
        card_layout.addLayout(top_row)

        # Hotkey + Encoder info row.
        meta_row = QHBoxLayout()
        meta_row.setSpacing(8)

        hotkey_caption = QLabel("Kısayol:", root)
        hotkey_caption.setProperty("class", "metaKey")
        meta_row.addWidget(hotkey_caption)

        self.hotkey_label = QLabel(self._hotkey, root)
        self.hotkey_label.setObjectName("hotkeyBadge")
        meta_row.addWidget(self.hotkey_label)

        meta_row.addStretch(1)

        self.encoder_label = QLabel("Kodlayıcı: Bekleniyor", root)
        self.encoder_label.setProperty("class", "metaKey")
        meta_row.addWidget(self.encoder_label)
        card_layout.addLayout(meta_row)

        # Output directory label.
        self.output_dir_label = QLabel("", root)
        self.output_dir_label.setProperty("class", "metaKey")
        self.output_dir_label.setWordWrap(True)
        card_layout.addWidget(self.output_dir_label)

        layout.addWidget(status_card)

        # Primary action buttons.
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.record_button = QPushButton("Kaydı Başlat", root)
        self.record_button.setObjectName("recordButton")
        self.record_button.clicked.connect(self.recordToggleRequested.emit)
        btn_row.addWidget(self.record_button, 1)

        self.quick_clip_button = QPushButton("5 sn Hızlı Klip", root)
        self.quick_clip_button.setObjectName("quickClipButton")
        self.quick_clip_button.clicked.connect(self._on_quick_clip_clicked)
        btn_row.addWidget(self.quick_clip_button, 0)

        layout.addLayout(btn_row)

        # Startup preference row.
        self.startup_checkbox = QCheckBox("Windows ile Başlat", root)
        self.startup_checkbox.setObjectName("startupCheckbox")
        self.startup_checkbox.toggled.connect(self.startupToggled.emit)
        layout.addWidget(self.startup_checkbox)

        # Footer: status message + quit button.
        footer_row = QHBoxLayout()
        footer_row.setSpacing(8)

        self.status_message_label = QLabel("Hazır bekliyor", root)
        self.status_message_label.setObjectName("statusMessage")
        self.status_message_label.setWordWrap(True)
        footer_row.addWidget(self.status_message_label, 1)

        self.quit_button = QPushButton("Çıkış", root)
        self.quit_button.clicked.connect(self._request_quit)
        footer_row.addWidget(self.quit_button, 0)

        layout.addLayout(footer_row)

    def _setup_tray(self, *, enable_tray: bool) -> None:
        if not enable_tray or not QSystemTrayIcon.isSystemTrayAvailable():
            return

        tray = QSystemTrayIcon(_build_tray_icon("#10b981"), self)
        tray.setToolTip("QuickClip")

        menu = QMenu(self)
        toggle_visibility_action = menu.addAction("Pencereyi Göster / Gizle")
        toggle_visibility_action.triggered.connect(self._toggle_window_visibility)

        record_action = menu.addAction("Kaydı Başlat")
        record_action.triggered.connect(self.recordToggleRequested.emit)

        quick_action = menu.addAction("5 sn Hızlı Klip")
        quick_action.triggered.connect(self._on_quick_clip_clicked)

        menu.addSeparator()
        quit_action = menu.addAction("Çıkış")
        quit_action.triggered.connect(self._request_quit)

        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()

        self._tray_icon = tray
        self._tray_record_action = record_action

    @Slot(CaptureState)
    @Slot(CaptureState, str)
    @Slot(object)
    @Slot(object, str)
    def set_state(self, state: CaptureState, detail: str = "") -> None:
        """Transition UI indicators, buttons, and elapsed clock to ``state``."""
        resolved = state if isinstance(state, CaptureState) else CaptureState(state)
        prev_state = self._state
        self._state = resolved

        badge_text, bg_hex, fg_hex = _STATE_META[resolved]
        self.state_badge_label.setText(badge_text)
        self.state_badge_label.setStyleSheet(f"background-color: {bg_hex}; color: {fg_hex};")

        if detail:
            if resolved == CaptureState.ERROR:
                self.set_status_message(detail)
            else:
                self.encoder_label.setText(detail)

        # Elapsed timer management.
        if resolved == CaptureState.RECORDING:
            if prev_state != CaptureState.RECORDING:
                self._record_started_monotonic = time.monotonic()
                self.elapsed_label.setText("00:00.0")
            if not self._elapsed_timer.isActive():
                self._elapsed_timer.start()
        elif resolved == CaptureState.STOPPING:
            self._elapsed_timer.stop()
        else:
            self._elapsed_timer.stop()
            self._record_started_monotonic = None
            self.elapsed_label.setText("00:00.0")

        # Main button and quick-clip button state.
        is_recording = resolved == CaptureState.RECORDING
        if resolved == CaptureState.WARMING:
            self.record_button.setText("Hazırlanıyor...")
            self.record_button.setEnabled(False)
            self.quick_clip_button.setEnabled(False)
        elif resolved == CaptureState.STOPPING:
            self.record_button.setText("Durduruluyor...")
            self.record_button.setEnabled(False)
            self.quick_clip_button.setEnabled(False)
        elif is_recording:
            self.record_button.setText("Kaydı Durdur")
            self.record_button.setEnabled(True)
            self.quick_clip_button.setEnabled(False)
        else:
            self.record_button.setText("Kaydı Başlat")
            self.record_button.setEnabled(True)
            self.quick_clip_button.setEnabled(True)

        self.record_button.setProperty("recording", "true" if is_recording else "false")
        self.record_button.style().unpolish(self.record_button)
        self.record_button.style().polish(self.record_button)

        # Tray visual feedback.
        if self._tray_icon is not None:
            dot_color = (
                "#ef4444"
                if is_recording
                else "#f59e0b"
                if resolved in (CaptureState.WARMING, CaptureState.STOPPING)
                else "#10b981"
                if resolved == CaptureState.STANDBY
                else "#64748b"
            )
            self._tray_icon.setIcon(_build_tray_icon(dot_color))
            self._tray_icon.setToolTip(f"QuickClip — {badge_text}")
        if self._tray_record_action is not None:
            self._tray_record_action.setText("Kaydı Durdur" if is_recording else "Kaydı Başlat")

    @Slot(str)
    def set_status_message(self, msg: str) -> None:
        """Update the footer status text shown to the user."""
        self.status_message_label.setText(msg)

    @Slot(str)
    def set_hotkey_label(self, hotkey: str) -> None:
        """Update the displayed global hotkey badge."""
        self._hotkey = hotkey
        self.hotkey_label.setText(hotkey)
        self.hotkey_label.setToolTip(f"Global kayıt kısayolu: {hotkey}")

    @Slot(Path)
    @Slot(object)
    def set_output_dir(self, path: Path) -> None:
        """Update the displayed clip export directory."""
        self._output_dir = Path(path)
        self.output_dir_label.setText(f"Kayıt klasörü: {self._output_dir}")
        self.output_dir_label.setToolTip(str(self._output_dir))

    @Slot(bool)
    def set_run_at_startup_checked(self, enabled: bool) -> None:
        """Sync the ``Windows ile Başlat`` checkbox without emitting ``startupToggled``."""
        with QSignalBlocker(self.startup_checkbox):
            self.startup_checkbox.setChecked(bool(enabled))

    def is_run_at_startup_checked(self) -> bool:
        """Return whether the ``Windows ile Başlat`` checkbox is currently checked."""
        return self.startup_checkbox.isChecked()

    @Slot()
    def _on_quick_clip_clicked(self) -> None:
        self.quickClipRequested.emit(5.0)

    @Slot()
    def _on_elapsed_tick(self) -> None:
        self.elapsed_label.setText(_format_elapsed_clock(self.elapsed_seconds()))

    @Slot()
    def _toggle_window_visibility(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.showNormal()
            self.activateWindow()

    @Slot(QSystemTrayIcon.ActivationReason)
    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._toggle_window_visibility()

    @Slot()
    def _request_quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self._cleanup_resources()
        self.quitRequested.emit()
        self.close()

    def _cleanup_resources(self) -> None:
        self._elapsed_timer.stop()
        if self._tray_icon is not None:
            self._tray_icon.hide()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Stop timers, hide tray icon, and emit ``quitRequested`` on window close."""
        self._cleanup_resources()
        if not self._quitting:
            self._quitting = True
            self.quitRequested.emit()
        super().closeEvent(event)
