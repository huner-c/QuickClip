"""Lightweight Warm Standby control bar window and system tray integration."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import time

from PySide6.QtCore import QRectF, QSignalBlocker, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QColor, QIcon, QKeyEvent, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
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

from quickclip.contracts import AppConfig, CaptureState, MonitorInfo
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
QComboBox#monitorCombo {
    background-color: #252b38;
    border: 1px solid #3a4459;
    border-radius: 5px;
    color: #e2e8f0;
    font-size: 12px;
    padding: 4px 8px;
}
QComboBox#monitorCombo:disabled {
    color: #64748b;
    background-color: #171a22;
    border-color: #232834;
}
QComboBox#monitorCombo::drop-down {
    border: none;
    width: 20px;
}
QComboBox#monitorCombo QAbstractItemView {
    background-color: #191d26;
    color: #e2e8f0;
    border: 1px solid #3a4459;
    selection-background-color: #2563eb;
    selection-color: #ffffff;
}
QPushButton#refreshMonitorsButton {
    padding: 4px 0px;
    font-size: 13px;
    font-weight: 700;
    min-width: 28px;
    max-width: 28px;
}
QPushButton.compactButton {
    padding: 3px 9px;
    font-size: 11px;
    font-weight: 600;
    border-radius: 5px;
}
QPushButton#changeHotkeyButton:checked {
    background-color: #1e3a5f;
    border-color: #3b82f6;
    color: #93c5fd;
}
QPushButton.modeButton {
    padding: 5px 10px;
    font-size: 12px;
    font-weight: 600;
    border-radius: 5px;
}
QPushButton.modeButton:checked {
    background-color: #1e3a5f;
    border-color: #3b82f6;
    color: #dbeafe;
}
"""

_QT_SPECIAL_KEYS: dict[int, str] = {
    int(Qt.Key.Key_Space): "Space",
    int(Qt.Key.Key_Insert): "Insert",
    int(Qt.Key.Key_Delete): "Delete",
    int(Qt.Key.Key_Home): "Home",
    int(Qt.Key.Key_End): "End",
    int(Qt.Key.Key_PageUp): "PageUp",
    int(Qt.Key.Key_PageDown): "PageDown",
    int(Qt.Key.Key_Pause): "Pause",
    int(Qt.Key.Key_Print): "PrintScreen",
    int(Qt.Key.Key_Up): "Up",
    int(Qt.Key.Key_Down): "Down",
    int(Qt.Key.Key_Left): "Left",
    int(Qt.Key.Key_Right): "Right",
    int(Qt.Key.Key_Tab): "Tab",
    int(Qt.Key.Key_Backspace): "Backspace",
    int(Qt.Key.Key_Return): "Enter",
    int(Qt.Key.Key_Enter): "Enter",
}
for _f_idx in range(1, 25):
    _QT_SPECIAL_KEYS[int(Qt.Key.Key_F1) + (_f_idx - 1)] = f"F{_f_idx}"

_MODIFIER_ONLY_KEYS: frozenset[int] = frozenset(
    {
        int(Qt.Key.Key_Control),
        int(Qt.Key.Key_Shift),
        int(Qt.Key.Key_Alt),
        int(Qt.Key.Key_AltGr),
        int(Qt.Key.Key_Meta),
        int(Qt.Key.Key_Super_L),
        int(Qt.Key.Key_Super_R),
        int(Qt.Key.Key_unknown),
        0,
    }
)
_BARE_ALLOWED_KEYS: frozenset[str] = frozenset(
    {*(f"F{i}" for i in range(1, 25)), "Pause", "Insert", "PrintScreen"}
)


def qt_key_event_to_hotkey(
    key: int,
    modifiers: Qt.KeyboardModifier,
) -> str | None:
    """Convert a Qt key + modifiers pair into a canonical QuickClip hotkey string.

    Returns ``None`` for modifier-only presses, Escape, unsupported keys, or
    alphanumeric keys pressed without any modifier.
    """
    key_int = int(key)
    if key_int in _MODIFIER_ONLY_KEYS or key_int == int(Qt.Key.Key_Escape):
        return None

    key_name: str | None = _QT_SPECIAL_KEYS.get(key_int)
    if key_name is None:
        if int(Qt.Key.Key_A) <= key_int <= int(Qt.Key.Key_Z):
            key_name = chr(key_int)
        elif int(Qt.Key.Key_0) <= key_int <= int(Qt.Key.Key_9):
            key_name = chr(key_int)
        else:
            return None

    parts: list[str] = []
    if modifiers & Qt.KeyboardModifier.ControlModifier:
        parts.append("Ctrl")
    if modifiers & Qt.KeyboardModifier.AltModifier:
        parts.append("Alt")
    if modifiers & Qt.KeyboardModifier.ShiftModifier:
        parts.append("Shift")
    if modifiers & Qt.KeyboardModifier.MetaModifier:
        parts.append("Win")

    if not parts and key_name not in _BARE_ALLOWED_KEYS:
        return None

    parts.append(key_name)
    return "+".join(parts)


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
    monitorChanged = Signal(int)
    monitorRefreshRequested = Signal()
    hotkeyChanged = Signal(str)
    outputDirChanged = Signal(Path)
    captureModeChanged = Signal(bool)
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
        self._capturing_hotkey: bool = False
        self._region_mode: bool = False

        self.setWindowTitle("QuickClip — Warm Standby")
        app_icon = load_app_icon()
        if not app_icon.isNull():
            self.setWindowIcon(app_icon)
        self.setFixedWidth(480)
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
        meta_row.setSpacing(6)

        hotkey_caption = QLabel("Kısayol:", root)
        hotkey_caption.setProperty("class", "metaKey")
        meta_row.addWidget(hotkey_caption)

        self.hotkey_label = QLabel(self._hotkey, root)
        self.hotkey_label.setObjectName("hotkeyBadge")
        meta_row.addWidget(self.hotkey_label)

        self.change_hotkey_button = QPushButton("Değiştir", root)
        self.change_hotkey_button.setObjectName("changeHotkeyButton")
        self.change_hotkey_button.setProperty("class", "compactButton")
        self.change_hotkey_button.setCheckable(True)
        self.change_hotkey_button.setToolTip("Yeni kısayol tuşu ata (İptal: Esc)")
        self.change_hotkey_button.clicked.connect(self._on_change_hotkey_clicked)
        meta_row.addWidget(self.change_hotkey_button)

        meta_row.addStretch(1)

        self.encoder_label = QLabel("Kodlayıcı: Bekleniyor", root)
        self.encoder_label.setProperty("class", "metaKey")
        meta_row.addWidget(self.encoder_label)
        card_layout.addLayout(meta_row)

        # Target display selector row.
        monitor_row = QHBoxLayout()
        monitor_row.setSpacing(6)

        monitor_caption = QLabel("Ekran:", root)
        monitor_caption.setProperty("class", "metaKey")
        monitor_row.addWidget(monitor_caption)

        self.monitor_combo = QComboBox(root)
        self.monitor_combo.setObjectName("monitorCombo")
        self.monitor_combo.setToolTip("Kayıt alınacak ekranı seçin")
        self.monitor_combo.addItem("Ekran 1 (Birincil)", 1)
        self.monitor_combo.currentIndexChanged.connect(self._on_monitor_combo_changed)
        monitor_row.addWidget(self.monitor_combo, 1)

        self.refresh_monitors_button = QPushButton("↻", root)
        self.refresh_monitors_button.setObjectName("refreshMonitorsButton")
        self.refresh_monitors_button.setToolTip("Bağlı ekran listesini yenile")
        self.refresh_monitors_button.clicked.connect(self.monitorRefreshRequested.emit)
        monitor_row.addWidget(self.refresh_monitors_button, 0)
        card_layout.addLayout(monitor_row)

        # Capture mode selector row (Full Screen vs Region Select).
        mode_row = QHBoxLayout()
        mode_row.setSpacing(6)

        mode_caption = QLabel("Mod:", root)
        mode_caption.setProperty("class", "metaKey")
        mode_row.addWidget(mode_caption)

        self.fullscreen_mode_button = QPushButton("Tam Ekran", root)
        self.fullscreen_mode_button.setObjectName("fullscreenModeButton")
        self.fullscreen_mode_button.setProperty("class", "modeButton")
        self.fullscreen_mode_button.setCheckable(True)
        self.fullscreen_mode_button.setChecked(True)
        self.fullscreen_mode_button.toggled.connect(self._on_fullscreen_mode_toggled)
        mode_row.addWidget(self.fullscreen_mode_button, 1)

        self.region_mode_button = QPushButton("Bölge Seç", root)
        self.region_mode_button.setObjectName("regionModeButton")
        self.region_mode_button.setProperty("class", "modeButton")
        self.region_mode_button.setCheckable(True)
        self.region_mode_button.setChecked(False)
        self.region_mode_button.setToolTip("Kaydı başlatınca ekranda istenen alanı sürükleyip seçin")
        self.region_mode_button.toggled.connect(self._on_region_mode_toggled)
        mode_row.addWidget(self.region_mode_button, 1)
        card_layout.addLayout(mode_row)

        # Output directory row with folder picker button.
        output_row = QHBoxLayout()
        output_row.setSpacing(6)

        self.output_dir_label = QLabel("", root)
        self.output_dir_label.setProperty("class", "metaKey")
        self.output_dir_label.setWordWrap(True)
        output_row.addWidget(self.output_dir_label, 1)

        self.change_output_dir_button = QPushButton("Klasör Seç", root)
        self.change_output_dir_button.setObjectName("changeOutputDirButton")
        self.change_output_dir_button.setProperty("class", "compactButton")
        self.change_output_dir_button.setToolTip("Videoların kaydedileceği klasörü değiştir")
        self.change_output_dir_button.clicked.connect(self.choose_output_dir)
        output_row.addWidget(self.change_output_dir_button, 0)
        card_layout.addLayout(output_row)

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
            self.record_button.setText(
                "Alan Seç & Kaydet" if self._region_mode else "Kaydı Başlat"
            )
            self.record_button.setEnabled(True)
            self.quick_clip_button.setEnabled(True)

        self.record_button.setProperty("recording", "true" if is_recording else "false")
        self.record_button.style().unpolish(self.record_button)
        self.record_button.style().polish(self.record_button)

        can_change_settings = resolved not in (
            CaptureState.WARMING,
            CaptureState.RECORDING,
            CaptureState.STOPPING,
        )
        if not can_change_settings and self._capturing_hotkey:
            self._cancel_hotkey_capture()
        self.monitor_combo.setEnabled(can_change_settings)
        self.refresh_monitors_button.setEnabled(can_change_settings)
        self.change_hotkey_button.setEnabled(can_change_settings)
        self.change_output_dir_button.setEnabled(can_change_settings)
        self.fullscreen_mode_button.setEnabled(can_change_settings)
        self.region_mode_button.setEnabled(can_change_settings)

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

    def set_monitors(
        self,
        monitors: Sequence[MonitorInfo],
        selected_index: int | None = None,
    ) -> None:
        """Populate available monitors without emitting ``monitorChanged``."""
        target = selected_index if selected_index is not None else self.selected_monitor_index()
        with QSignalBlocker(self.monitor_combo):
            self.monitor_combo.clear()
            if not monitors:
                self.monitor_combo.addItem("Ekran 1 (Birincil)", 1)
            else:
                for mon in monitors:
                    self.monitor_combo.addItem(mon.label, int(mon.index))
                idx_pos = self.monitor_combo.findData(int(target))
                if idx_pos >= 0:
                    self.monitor_combo.setCurrentIndex(idx_pos)
                else:
                    primary_pos = next(
                        (i for i, mon in enumerate(monitors) if mon.is_primary),
                        0,
                    )
                    self.monitor_combo.setCurrentIndex(primary_pos)

    def set_selected_monitor(self, monitor_index: int) -> None:
        """Select ``monitor_index`` in the dropdown without emitting ``monitorChanged``."""
        with QSignalBlocker(self.monitor_combo):
            pos = self.monitor_combo.findData(int(monitor_index))
            if pos >= 0:
                self.monitor_combo.setCurrentIndex(pos)

    def selected_monitor_index(self) -> int:
        """Return the currently selected 1-based monitor index."""
        data = self.monitor_combo.currentData()
        return int(data) if data is not None else 1

    def is_region_mode(self) -> bool:
        """Return True when capture mode is set to interactive region selection."""
        return self._region_mode

    @Slot(bool)
    def set_region_mode(self, enabled: bool) -> None:
        """Sync Full Screen / Region mode buttons without emitting ``captureModeChanged``."""
        flag = bool(enabled)
        self._region_mode = flag
        with QSignalBlocker(self.fullscreen_mode_button):
            self.fullscreen_mode_button.setChecked(not flag)
        with QSignalBlocker(self.region_mode_button):
            self.region_mode_button.setChecked(flag)
        if self._state not in (CaptureState.WARMING, CaptureState.RECORDING, CaptureState.STOPPING):
            self.record_button.setText("Alan Seç & Kaydet" if flag else "Kaydı Başlat")

    def is_capturing_hotkey(self) -> bool:
        """Return True while waiting for the user to press a new hotkey."""
        return self._capturing_hotkey

    def _cancel_hotkey_capture(self) -> None:
        self._capturing_hotkey = False
        with QSignalBlocker(self.change_hotkey_button):
            self.change_hotkey_button.setChecked(False)
        self.change_hotkey_button.setText("Değiştir")

    @Slot(bool)
    def _on_change_hotkey_clicked(self, checked: bool) -> None:
        if checked:
            self._capturing_hotkey = True
            self.change_hotkey_button.setText("Tuşa basın...")
            self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
            self.set_status_message("Yeni kısayol tuşuna basın (F1-F12 veya Ctrl/Alt+Tuş • Esc: İptal)")
        else:
            self._cancel_hotkey_capture()
            self.set_status_message(f"Kısayol değişikliği iptal edildi ({self._hotkey}).")

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Capture a new global hotkey while hotkey-edit mode is active."""
        if self._capturing_hotkey:
            if event.key() == Qt.Key.Key_Escape:
                self._cancel_hotkey_capture()
                self.set_status_message(f"Kısayol değişikliği iptal edildi ({self._hotkey}).")
                event.accept()
                return
            hotkey = qt_key_event_to_hotkey(event.key(), event.modifiers())
            if hotkey is not None:
                self._cancel_hotkey_capture()
                self.hotkeyChanged.emit(hotkey)
            else:
                if int(event.key()) not in _MODIFIER_ONLY_KEYS:
                    self.set_status_message(
                        "Geçersiz tuş — F1-F12 veya Ctrl/Alt/Shift kombinasyonu seçin (Esc: İptal)"
                    )
            event.accept()
            return
        super().keyPressEvent(event)

    @Slot()
    def choose_output_dir(self) -> None:
        """Open a folder dialog and emit ``outputDirChanged`` if a folder is selected."""
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Kayıt Klasörü Seç",
            str(self._output_dir),
        )
        if chosen:
            new_path = Path(chosen)
            self.set_output_dir(new_path)
            self.outputDirChanged.emit(new_path)

    @Slot(bool)
    def _on_fullscreen_mode_toggled(self, checked: bool) -> None:
        if checked:
            self._on_mode_button_clicked(False)
        elif not self._region_mode:
            with QSignalBlocker(self.fullscreen_mode_button):
                self.fullscreen_mode_button.setChecked(True)

    @Slot(bool)
    def _on_region_mode_toggled(self, checked: bool) -> None:
        if checked:
            self._on_mode_button_clicked(True)
        elif self._region_mode:
            with QSignalBlocker(self.region_mode_button):
                self.region_mode_button.setChecked(True)

    def _on_mode_button_clicked(self, region_enabled: bool) -> None:
        prev = self._region_mode
        self.set_region_mode(region_enabled)
        if region_enabled != prev:
            self.captureModeChanged.emit(region_enabled)

    @Slot(int)
    def _on_monitor_combo_changed(self, combo_index: int) -> None:
        if combo_index < 0:
            return
        data = self.monitor_combo.itemData(combo_index)
        if data is not None:
            self.monitorChanged.emit(int(data))

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
