"""Application controller wiring QuickClip capture, preview trimmer, exporter, and UI."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import logging
from pathlib import Path
import sys
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, QRect, Qt, Signal, Slot
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from quickclip.capture import CaptureEngine, HotkeyListener, list_monitors
from quickclip.contracts import (
    AppConfig,
    CaptureConfig,
    CaptureRegion,
    CaptureResult,
    CaptureState,
    MonitorInfo,
    TrimRequest,
    TrimResult,
)
from quickclip.exporter import ExporterService
from quickclip.paths import resolve_resource_path
from quickclip.settings import UserSettings, save_user_settings
import quickclip.startup as startup_mod
from quickclip.ui import RegionSelectorOverlay, StandbyWindow, TrimmerWindow

logger = logging.getLogger(__name__)

DEFAULT_SINGLE_INSTANCE_NAME = "QuickClipSingleInstance"


def try_activate_existing_instance(
    server_name: str = DEFAULT_SINGLE_INSTANCE_NAME,
    timeout_ms: int = 300,
) -> bool:
    """Signal an already-running QuickClip instance to show/focus its window."""
    if not server_name:
        return False
    socket = QLocalSocket()
    socket.connectToServer(server_name)
    if not socket.waitForConnected(timeout_ms):
        socket.abort()
        return False
    try:
        socket.write(b"activate\n")
        socket.flush()
        socket.waitForBytesWritten(timeout_ms)
        socket.disconnectFromServer()
        if socket.state() != QLocalSocket.LocalSocketState.UnconnectedState:
            socket.waitForDisconnected(100)
    finally:
        socket.close()
    return True


def _discover_monitors_with_qt_names() -> list[MonitorInfo]:
    """Enumerate monitors via capture backend and enrich names from Qt screens."""
    monitors = list_monitors()
    if not monitors:
        return []

    screens = QGuiApplication.screens()
    if not screens:
        return monitors

    by_origin: dict[tuple[int, int], str] = {}
    for screen in screens:
        geom = screen.geometry()
        raw_name = (screen.name() or screen.model() or "").strip()
        if raw_name.startswith("\\\\.\\"):
            raw_name = raw_name[4:]
        if raw_name:
            by_origin[(int(geom.x()), int(geom.y()))] = raw_name

    enriched: list[MonitorInfo] = []
    for mon in monitors:
        friendly = by_origin.get((mon.left, mon.top), "")
        name = mon.name or friendly
        enriched.append(
            MonitorInfo(
                index=mon.index,
                name=name,
                width=mon.width,
                height=mon.height,
                left=mon.left,
                top=mon.top,
                is_primary=mon.is_primary,
            )
        )
    return enriched


class QuickClipController(QObject):
    """Thread-safe orchestrator connecting hotkeys, capture engine, trimmer UI, and exporter."""

    stateChanged = Signal(object, str)
    statusMessageChanged = Signal(str)
    captureFinished = Signal(object)
    exportFinished = Signal(object)
    shutdownCompleted = Signal()

    _hotkeyTriggered = Signal()
    _autoStopTriggered = Signal(object)
    _warmUpSucceeded = Signal(str, str)
    _warmUpFailed = Signal(str)
    _recordingStarted = Signal(str)
    _recordingStartFailed = Signal(str)
    _stopCompleted = Signal(object)
    _exportCompleted = Signal(object)

    def __init__(
        self,
        config: AppConfig | None = None,
        *,
        capture_engine: CaptureEngine | None = None,
        exporter: ExporterService | None = None,
        hotkey_listener: HotkeyListener | None = None,
        standby_window: StandbyWindow | None = None,
        trimmer_factory: Callable[[], TrimmerWindow] | None = None,
        grabber_factory: Callable[[CaptureConfig], Any] | None = None,
        enable_tray: bool = True,
        auto_close_ms: int = 1500,
        quit_on_exit: bool = False,
        single_instance_name: str | None = None,
        startup_enabled_fn: Callable[[], bool] | None = None,
        set_startup_fn: Callable[[bool], bool] | None = None,
        monitors_fn: Callable[[], list[MonitorInfo]] | None = None,
        region_mode: bool = False,
        region_selector_factory: Callable[[MonitorInfo, QRect | None], RegionSelectorOverlay] | None = None,
        settings_path: Path | None = None,
        persist_settings: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config if config is not None else AppConfig()
        self._base_auto_stop_seconds: float | None = self._config.capture.auto_stop_seconds
        self._auto_close_ms = max(0, int(auto_close_ms))
        self._quit_on_exit = bool(quit_on_exit)

        self._exporter = exporter if exporter is not None else ExporterService(
            ffmpeg_path=self._config.ffmpeg_path,
            ffprobe_path=self._config.ffprobe_path,
        )
        self._hotkey_listener = hotkey_listener if hotkey_listener is not None else HotkeyListener()
        if capture_engine is not None:
            self._capture_engine = capture_engine
            if getattr(self._capture_engine, "_on_auto_stop", None) is None:
                self._capture_engine._on_auto_stop = self._on_engine_auto_stop
        else:
            self._capture_engine = CaptureEngine(
                self._config.capture,
                ffmpeg_path=self._config.ffmpeg_path,
                ffprobe_path=self._config.ffprobe_path,
                on_auto_stop=self._on_engine_auto_stop,
                grabber_factory=grabber_factory,
            )

        self._standby = standby_window if standby_window is not None else StandbyWindow(
            hotkey=self._config.capture.hotkey,
            output_dir=self._config.output_dir,
            enable_tray=enable_tray,
        )
        self._trimmer_factory = trimmer_factory
        self._trimmer_window: TrimmerWindow | None = None

        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="QuickClip-Worker",
        )

        self._shutting_down = False
        self._shutdown_done = False
        self._warm_in_flight = False
        self._start_in_flight = False
        self._stop_in_flight = False
        self._capture_pending = False
        self._export_in_flight = False
        self._unhandled_capture_path: Path | None = None

        self._single_instance_name = single_instance_name
        self._local_server: QLocalServer | None = None
        self._startup_enabled_fn = (
            startup_enabled_fn
            if startup_enabled_fn is not None
            else lambda: startup_mod.is_run_at_startup_enabled()
        )
        self._set_startup_fn = (
            set_startup_fn
            if set_startup_fn is not None
            else lambda enabled: startup_mod.set_run_at_startup(enabled)
        )
        self._monitors_fn = (
            monitors_fn
            if monitors_fn is not None
            else _discover_monitors_with_qt_names
        )
        self._monitors: list[MonitorInfo] = []
        self._region_mode: bool = bool(region_mode)
        self._region_selector_factory = region_selector_factory
        self._region_overlay: RegionSelectorOverlay | None = None
        self._selecting_region: bool = False
        self._pending_region_auto_stop: float | None = None
        self._restore_standby_after_selection: bool = False
        self._settings_path = settings_path
        self._persist_settings = bool(persist_settings)

        self._sync_startup_checkbox()
        if hasattr(self._standby, "set_region_mode"):
            self._standby.set_region_mode(self._region_mode)
        self.refresh_monitors()
        self._connect_signals()
        self._start_single_instance_server()

    @property
    def config(self) -> AppConfig:
        """Return the active application configuration."""
        return self._config

    @property
    def capture_engine(self) -> CaptureEngine:
        """Return the managed capture engine."""
        return self._capture_engine

    @property
    def exporter(self) -> ExporterService:
        """Return the managed lossless exporter service."""
        return self._exporter

    @property
    def hotkey_listener(self) -> HotkeyListener:
        """Return the managed global hotkey listener."""
        return self._hotkey_listener

    @property
    def standby_window(self) -> StandbyWindow:
        """Return the Warm Standby control window."""
        return self._standby

    @property
    def trimmer_window(self) -> TrimmerWindow | None:
        """Return the trimmer window if instantiated, else ``None``."""
        return self._trimmer_window

    def _connect_signals(self) -> None:
        self._standby.recordToggleRequested.connect(self.toggle_recording)
        self._standby.quickClipRequested.connect(self.start_quick_clip)
        if hasattr(self._standby, "monitorChanged"):
            self._standby.monitorChanged.connect(self.select_monitor)
        if hasattr(self._standby, "monitorRefreshRequested"):
            self._standby.monitorRefreshRequested.connect(self._on_monitor_refresh_requested)
        if hasattr(self._standby, "hotkeyChanged"):
            self._standby.hotkeyChanged.connect(self.change_hotkey)
        if hasattr(self._standby, "outputDirChanged"):
            self._standby.outputDirChanged.connect(self.set_output_dir)
        if hasattr(self._standby, "captureModeChanged"):
            self._standby.captureModeChanged.connect(self.set_region_mode)
        self._standby.startupToggled.connect(self._on_startup_toggled)
        self._standby.quitRequested.connect(self.shutdown)

        queued = Qt.ConnectionType.QueuedConnection
        self._hotkeyTriggered.connect(self.toggle_recording, queued)
        self._autoStopTriggered.connect(self._handle_capture_result, queued)
        self._warmUpSucceeded.connect(self._on_warm_up_succeeded, queued)
        self._warmUpFailed.connect(self._on_warm_up_failed, queued)
        self._recordingStarted.connect(self._on_recording_started, queued)
        self._recordingStartFailed.connect(self._on_recording_start_failed, queued)
        self._stopCompleted.connect(self._handle_capture_result, queued)
        self._exportCompleted.connect(self._on_export_completed, queued)

    def _sync_startup_checkbox(self) -> None:
        try:
            enabled = bool(self._startup_enabled_fn())
        except Exception:
            logger.debug("Failed reading initial startup state.", exc_info=True)
            enabled = False
        if hasattr(self._standby, "set_run_at_startup_checked"):
            self._standby.set_run_at_startup_checked(enabled)

    def refresh_monitors(self, *, notify: bool = False) -> list[MonitorInfo]:
        """Re-enumerate connected monitors and sync the standby display selector."""
        try:
            monitors = list(self._monitors_fn())
        except Exception:
            logger.debug("Failed enumerating display monitors.", exc_info=True)
            monitors = []

        self._monitors = monitors
        if monitors:
            available_indices = {m.index for m in monitors}
            if self._config.capture.monitor_index not in available_indices:
                fallback = next(
                    (m.index for m in monitors if m.is_primary),
                    monitors[0].index,
                )
                self._config.capture.monitor_index = fallback

        if hasattr(self._standby, "set_monitors"):
            self._standby.set_monitors(
                monitors,
                selected_index=self._config.capture.monitor_index,
            )

        if notify:
            count = len(monitors)
            self._set_status(
                f"{count} ekran algılandı."
                if count > 0
                else "Ekran listesi güncellendi (varsayılan ekran kullanılıyor)."
            )
        return monitors

    def _save_settings(self) -> None:
        if not self._persist_settings:
            return
        save_user_settings(
            UserSettings(
                hotkey=self._config.capture.hotkey,
                output_dir=str(self._config.output_dir),
                monitor_index=self._config.capture.monitor_index,
                region_mode=self._region_mode,
            ),
            path=self._settings_path,
        )

    @Slot(str)
    def change_hotkey(self, new_hotkey: str) -> bool:
        """Re-register the global hotkey to ``new_hotkey``, rolling back on failure."""
        cleaned = new_hotkey.strip() if isinstance(new_hotkey, str) else ""
        if not cleaned or self._shutting_down:
            return False
        if (
            self._selecting_region
            or self._start_in_flight
            or self._stop_in_flight
            or self._capture_pending
            or self._capture_engine.state in (CaptureState.RECORDING, CaptureState.STOPPING)
        ):
            self._set_status("Kayıt sürerken kısayol değiştirilemez.")
            return False

        old_hotkey = self._config.capture.hotkey
        if cleaned.lower() == old_hotkey.lower():
            self._standby.set_hotkey_label(cleaned)
            self._config.capture.hotkey = cleaned
            self._set_status(f"Kısayol zaten {cleaned}.")
            return True

        try:
            self._hotkey_listener.register(cleaned, self._on_hotkey_callback)
        except Exception as exc:
            logger.warning("Failed re-registering hotkey %r: %s", cleaned, exc)
            try:
                self._hotkey_listener.register(old_hotkey, self._on_hotkey_callback)
            except Exception:
                logger.debug("Failed restoring previous hotkey %r.", old_hotkey, exc_info=True)
            self._standby.set_hotkey_label(old_hotkey)
            self._set_status(f"Kısayol ({cleaned}) atanamadı: {exc}")
            return False

        self._config.capture.hotkey = cleaned
        self._standby.set_hotkey_label(cleaned)
        self._save_settings()
        self._set_status(f"Kısayol güncellendi: {cleaned}")
        return True

    @Slot(Path)
    @Slot(object)
    def set_output_dir(self, new_dir: Path | str) -> bool:
        """Update the export destination folder and persist the preference."""
        if self._shutting_down:
            return False
        if self._export_in_flight:
            self._set_status("Dışa aktarma sürerken kayıt klasörü değiştirilemez.")
            return False

        target = Path(new_dir)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._set_status(f"Kayıt klasörü kullanılamadı: {exc}")
            return False

        self._config.output_dir = target
        self._standby.set_output_dir(target)
        if self._trimmer_window is not None:
            self._trimmer_window._output_dir = target
            self._trimmer_window.output_dir_label.setText(f"Klasör: {target}")
        self._save_settings()
        self._set_status(f"Kayıt klasörü güncellendi: {target}")
        return True

    @Slot(bool)
    def set_region_mode(self, enabled: bool) -> None:
        """Toggle between Full Screen instant capture and interactive Region Selection."""
        if self._shutting_down:
            return
        flag = bool(enabled)
        if (
            self._selecting_region
            or self._start_in_flight
            or self._stop_in_flight
            or self._capture_pending
            or self._capture_engine.state in (CaptureState.RECORDING, CaptureState.STOPPING)
        ):
            if hasattr(self._standby, "set_region_mode"):
                self._standby.set_region_mode(self._region_mode)
            self._set_status("Kayıt sürerken mod değiştirilemez.")
            return

        self._region_mode = flag
        if hasattr(self._standby, "set_region_mode"):
            self._standby.set_region_mode(flag)
        self._save_settings()
        self._set_status(
            "Bölge Seç modu aktif — kaydı başlatınca ekranda alan seçebilirsiniz."
            if flag
            else f"Tam Ekran modu aktif — {self._config.capture.hotkey} ile anında kayıt hazır."
        )

    @Slot()
    def _on_monitor_refresh_requested(self) -> None:
        self.refresh_monitors(notify=True)

    @Slot(int)
    def select_monitor(self, monitor_index: int) -> None:
        """Switch the capture target monitor and re-arm Warm Standby off the UI thread."""
        target = int(monitor_index)
        if target < 0 or self._shutting_down:
            return
        if (
            self._warm_in_flight
            or self._start_in_flight
            or self._stop_in_flight
            or self._capture_pending
            or self._export_in_flight
            or self._unhandled_capture_path is not None
            or self._capture_engine.state
            in (CaptureState.WARMING, CaptureState.RECORDING, CaptureState.STOPPING)
        ):
            if hasattr(self._standby, "set_selected_monitor"):
                self._standby.set_selected_monitor(self._config.capture.monitor_index)
            self._set_status("Kayıt veya işlem sürerken ekran değiştirilemez.")
            return

        if (
            target == self._config.capture.monitor_index
            and self._capture_engine.state == CaptureState.STANDBY
        ):
            return

        self._config.capture.monitor_index = target
        self._config.capture.capture_region = None
        self._save_settings()
        self._warm_in_flight = True
        self._set_ui_state(CaptureState.WARMING, "Kodlayıcı: Hazırlanıyor...")
        self._set_status(f"Ekran {target} için kayıt motoru hazırlanıyor...")
        self._executor.submit(self._worker_switch_monitor, target)

    def _worker_switch_monitor(self, monitor_index: int) -> None:
        if self._shutting_down:
            self._warmUpFailed.emit("Uygulama kapatılıyor.")
            return
        try:
            if hasattr(self._capture_engine, "set_monitor"):
                self._capture_engine.set_monitor(monitor_index, rewarm=True)
            else:
                self._config.capture.monitor_index = monitor_index
                self._capture_engine.warm_up()
        except Exception as exc:
            self._warmUpFailed.emit(str(exc))
            return
        encoder = getattr(self._capture_engine, "encoder_used", None) or "h264"
        backend = getattr(self._capture_engine, "grabber_backend", None) or ""
        self._warmUpSucceeded.emit(encoder, backend)

    @Slot(bool)
    def _on_startup_toggled(self, enabled: bool) -> None:
        try:
            ok = bool(self._set_startup_fn(bool(enabled)))
        except Exception:
            logger.warning("Failed updating Windows startup entry.", exc_info=True)
            ok = False

        if ok:
            self._set_status(
                "Windows ile otomatik başlatma etkinleştirildi."
                if enabled
                else "Windows ile otomatik başlatma kapatıldı."
            )
        else:
            if hasattr(self._standby, "set_run_at_startup_checked"):
                self._standby.set_run_at_startup_checked(not enabled)
            self._set_status("Windows başlangıç ayarı güncellenemedi.")

    def _start_single_instance_server(self) -> None:
        if not self._single_instance_name:
            return
        server = QLocalServer(self)
        server.newConnection.connect(self._on_single_instance_connection)
        if not server.listen(self._single_instance_name):
            QLocalServer.removeServer(self._single_instance_name)
            if not server.listen(self._single_instance_name):
                logger.warning(
                    "Could not listen on single-instance server %r: %s",
                    self._single_instance_name,
                    server.errorString(),
                )
                server.deleteLater()
                return
        self._local_server = server

    @Slot()
    def _on_single_instance_connection(self) -> None:
        if self._local_server is None:
            return
        while self._local_server.hasPendingConnections():
            sock = self._local_server.nextPendingConnection()
            if sock is None:
                continue
            if sock.bytesAvailable() == 0:
                sock.waitForReadyRead(100)
            _ = bytes(sock.readAll())
            sock.disconnectFromServer()
            sock.close()
            sock.deleteLater()
            if not self._shutting_down:
                self.activate_window()

    @Slot()
    def activate_window(self) -> None:
        """Restore, show, raise, and focus active QuickClip windows."""
        if self._shutting_down:
            return
        self._standby.showNormal()
        self._standby.show()
        self._standby.raise_()
        self._standby.activateWindow()
        if self._trimmer_window is not None and self._trimmer_window.isVisible():
            self._trimmer_window.showNormal()
            self._trimmer_window.raise_()
            self._trimmer_window.activateWindow()

    def start(self, *, show_standby: bool = True) -> None:
        """Register the global hotkey, show the standby bar, and pre-warm capture."""
        if self._shutting_down:
            return

        if show_standby:
            self._standby.show()

        hotkey = self._config.capture.hotkey
        try:
            self._hotkey_listener.register(hotkey, self._on_hotkey_callback)
        except Exception as exc:
            logger.warning("Global hotkey registration failed for %r: %s", hotkey, exc)
            self._set_status(
                f"Kısayol ({hotkey}) kaydedilemedi ({exc}); pencere butonlarını kullanın."
            )

        self._schedule_warm_up()

    def _on_hotkey_callback(self) -> None:
        if not self._shutting_down:
            self._hotkeyTriggered.emit()

    def _on_engine_auto_stop(self, result: CaptureResult) -> None:
        if not self._shutting_down:
            self._autoStopTriggered.emit(result)

    def _set_ui_state(self, state: CaptureState, detail: str = "") -> None:
        self._standby.set_state(state, detail)
        self.stateChanged.emit(state, detail)

    def _set_status(self, message: str) -> None:
        self._standby.set_status_message(message)
        self.statusMessageChanged.emit(message)

    def _ensure_trimmer_window(self) -> TrimmerWindow:
        if self._trimmer_window is None:
            trimmer = (
                self._trimmer_factory()
                if self._trimmer_factory is not None
                else TrimmerWindow(output_dir=self._config.output_dir)
            )
            trimmer.exportRequested.connect(self._on_trimmer_export_requested)
            trimmer.discardRequested.connect(self._on_trimmer_discard_requested)
            trimmer.closed.connect(self._on_trimmer_closed)
            self._trimmer_window = trimmer
        return self._trimmer_window

    def _focus_trimmer(self) -> None:
        if self._trimmer_window is not None:
            self._trimmer_window.show()
            self._trimmer_window.raise_()
            self._trimmer_window.activateWindow()

    def _schedule_warm_up(self, cleanup_path: Path | None = None) -> None:
        if (
            self._shutting_down
            or self._warm_in_flight
            or self._capture_pending
            or self._export_in_flight
            or self._unhandled_capture_path is not None
        ):
            return
        self._config.capture.capture_region = None
        self._warm_in_flight = True
        self._set_ui_state(CaptureState.WARMING, "Kodlayıcı: Hazırlanıyor...")
        if cleanup_path is None:
            self._set_status("Kayıt motoru hazırlanıyor...")
        self._executor.submit(self._worker_warm_up, cleanup_path)

    def _worker_warm_up(self, cleanup_path: Path | None) -> None:
        if cleanup_path is not None:
            try:
                self._exporter.cleanup_temp(cleanup_path)
            except Exception:
                logger.debug("Ignoring temp cleanup failure before warm_up.", exc_info=True)
        if self._shutting_down:
            self._warmUpFailed.emit("Uygulama kapatılıyor.")
            return
        try:
            self._capture_engine.warm_up()
        except Exception as exc:
            self._warmUpFailed.emit(str(exc))
            return
        encoder = getattr(self._capture_engine, "encoder_used", None) or "h264"
        backend = getattr(self._capture_engine, "grabber_backend", None) or ""
        self._warmUpSucceeded.emit(encoder, backend)

    @Slot(str, str)
    def _on_warm_up_succeeded(self, encoder: str, backend: str) -> None:
        self._warm_in_flight = False
        if self._shutting_down:
            return
        detail = f"Kodlayıcı: {encoder} ({backend})" if backend else f"Kodlayıcı: {encoder}"
        size = getattr(self._capture_engine, "capture_size", None)
        if isinstance(size, tuple) and len(size) == 2 and size[0] > 0 and size[1] > 0:
            detail = f"{detail} • {size[0]}x{size[1]}"
        self._set_ui_state(CaptureState.STANDBY, detail)
        self._set_status(f"Hazır — {self._config.capture.hotkey} ile kayda başla")

    @Slot(str)
    def _on_warm_up_failed(self, error: str) -> None:
        self._warm_in_flight = False
        if self._shutting_down:
            return
        self._set_ui_state(CaptureState.ERROR, f"Hazırlama hatası: {error}")

    @Slot()
    def toggle_recording(self) -> None:
        """Start recording when in STANDBY, or stop recording when RECORDING."""
        if self._shutting_down:
            return
        if self._selecting_region:
            self._cancel_region_selection()
            return
        if self._export_in_flight:
            self._set_status("Dışa aktarma sürüyor, lütfen bekleyin...")
            return
        if self._unhandled_capture_path is not None:
            self._focus_trimmer()
            self._set_status("Önce mevcut klibi kaydedin veya silin.")
            return
        if self._start_in_flight or self._stop_in_flight:
            return

        current_state = self._capture_engine.state
        if current_state == CaptureState.RECORDING:
            self.stop_recording()
        elif current_state == CaptureState.STANDBY:
            if self._region_mode:
                self._begin_region_selection(auto_stop_override=None)
            else:
                self._start_recording_async(auto_stop_override=None)
        elif current_state == CaptureState.WARMING or self._warm_in_flight:
            self._set_status("Motor hazırlanıyor, lütfen bekleyin...")
        elif current_state in (CaptureState.COLD, CaptureState.ERROR):
            self._schedule_warm_up()

    @Slot(float)
    def start_quick_clip(self, seconds: float = 5.0) -> None:
        """Start an auto-timed quick clip recording of ``seconds`` duration."""
        if self._shutting_down or seconds <= 0:
            return
        if self._selecting_region:
            self._cancel_region_selection()
            return
        if self._export_in_flight:
            self._set_status("Dışa aktarma sürüyor, lütfen bekleyin...")
            return
        if self._unhandled_capture_path is not None:
            self._focus_trimmer()
            self._set_status("Önce mevcut klibi kaydedin veya silin.")
            return
        if self._start_in_flight or self._stop_in_flight:
            return

        current_state = self._capture_engine.state
        if current_state != CaptureState.STANDBY:
            if current_state in (CaptureState.COLD, CaptureState.ERROR) and not self._warm_in_flight:
                self._schedule_warm_up()
            else:
                self._set_status("Hızlı klip için motorun hazır (STANDBY) olması gerekiyor.")
            return

        if self._region_mode:
            self._begin_region_selection(auto_stop_override=float(seconds))
        else:
            self._start_recording_async(auto_stop_override=float(seconds))

    def _find_selected_monitor_and_screen_rect(self) -> tuple[MonitorInfo, QRect | None]:
        monitors = self._monitors if self._monitors else self.refresh_monitors()
        target_idx = self._config.capture.monitor_index
        chosen = next((m for m in monitors if m.index == target_idx), None)
        if chosen is None and monitors:
            chosen = monitors[0]
        if chosen is None:
            size = getattr(self._capture_engine, "capture_size", None) or (1920, 1080)
            chosen = MonitorInfo(
                index=target_idx,
                name="",
                width=int(size[0]),
                height=int(size[1]),
                left=0,
                top=0,
                is_primary=True,
            )

        screen_rect: QRect | None = None
        screens = QGuiApplication.screens()
        for screen in screens:
            geom = screen.geometry()
            if int(geom.x()) == chosen.left and int(geom.y()) == chosen.top:
                screen_rect = QRect(geom)
                break
        if screen_rect is None and screens:
            primary = QGuiApplication.primaryScreen()
            if primary is not None:
                screen_rect = QRect(primary.geometry())
        return chosen, screen_rect

    def _begin_region_selection(self, *, auto_stop_override: float | None) -> None:
        monitor, screen_rect = self._find_selected_monitor_and_screen_rect()
        self._selecting_region = True
        self._pending_region_auto_stop = auto_stop_override
        self._restore_standby_after_selection = self._standby.isVisible()
        if self._restore_standby_after_selection:
            self._standby.hide()

        overlay = (
            self._region_selector_factory(monitor, screen_rect)
            if self._region_selector_factory is not None
            else RegionSelectorOverlay(monitor=monitor, screen_geometry=screen_rect)
        )
        overlay.regionSelected.connect(self._on_region_selected)
        overlay.selectionCancelled.connect(self._on_region_selection_cancelled)
        self._region_overlay = overlay
        overlay.start_selection()

    def _close_region_overlay(self) -> None:
        overlay = self._region_overlay
        self._region_overlay = None
        if overlay is not None:
            try:
                overlay.hide()
                overlay.deleteLater()
            except Exception:
                logger.debug("Ignoring error closing region overlay.", exc_info=True)

    def _cancel_region_selection(self) -> None:
        overlay = self._region_overlay
        if overlay is not None:
            overlay.cancel_selection()
        else:
            self._on_region_selection_cancelled()

    @Slot(object)
    def _on_region_selected(self, region: CaptureRegion) -> None:
        self._selecting_region = False
        auto_stop_override = self._pending_region_auto_stop
        self._pending_region_auto_stop = None
        self._close_region_overlay()

        if self._restore_standby_after_selection and not self._shutting_down:
            self._standby.show()
        self._restore_standby_after_selection = False

        if self._shutting_down:
            return

        self._start_in_flight = True
        self._capture_pending = True
        self._set_ui_state(
            CaptureState.WARMING,
            f"Bölge hazırlanıyor: {region.width}x{region.height}",
        )
        self._set_status(f"Seçili bölge ({region.width}x{region.height}) başlatılıyor...")
        self._executor.submit(self._worker_start_region_recording, region, auto_stop_override)

    @Slot()
    def _on_region_selection_cancelled(self) -> None:
        self._selecting_region = False
        self._pending_region_auto_stop = None
        self._close_region_overlay()
        if self._restore_standby_after_selection and not self._shutting_down:
            self._standby.show()
            self._standby.raise_()
            self._standby.activateWindow()
        self._restore_standby_after_selection = False
        if not self._shutting_down:
            self._set_status("Bölge seçimi iptal edildi.")

    def _worker_start_region_recording(
        self,
        region: CaptureRegion,
        auto_stop_override: float | None,
    ) -> None:
        if self._shutting_down:
            self._recordingStartFailed.emit("Uygulama kapatılıyor.")
            return
        try:
            self._config.capture.auto_stop_seconds = (
                auto_stop_override
                if auto_stop_override is not None
                else self._base_auto_stop_seconds
            )
            if hasattr(self._capture_engine, "set_capture_region"):
                self._capture_engine.set_capture_region(region, rewarm=True)
            else:
                self._config.capture.capture_region = region
                self._capture_engine.warm_up()
            self._capture_engine.start_recording()
        except Exception as exc:
            self._config.capture.auto_stop_seconds = self._base_auto_stop_seconds
            self._config.capture.capture_region = None
            self._recordingStartFailed.emit(str(exc))
            return

        status = (
            f"Bölge ({region.width}x{region.height}) — {auto_stop_override:g} sn kaydediliyor..."
            if auto_stop_override is not None
            else f"Bölge ({region.width}x{region.height}) kaydediliyor — durdurmak için {self._config.capture.hotkey}"
        )
        self._recordingStarted.emit(status)

    def _start_recording_async(self, *, auto_stop_override: float | None) -> None:
        self._start_in_flight = True
        self._capture_pending = True
        status = (
            f"{auto_stop_override:g} sn hızlı klip başlatılıyor..."
            if auto_stop_override is not None
            else "Kayıt başlatılıyor..."
        )
        self._set_status(status)
        self._executor.submit(self._worker_start_recording, auto_stop_override)

    def _worker_start_recording(self, auto_stop_override: float | None) -> None:
        if self._shutting_down:
            self._recordingStartFailed.emit("Uygulama kapatılıyor.")
            return
        try:
            self._config.capture.auto_stop_seconds = (
                auto_stop_override
                if auto_stop_override is not None
                else self._base_auto_stop_seconds
            )
            self._capture_engine.start_recording()
        except Exception as exc:
            self._config.capture.auto_stop_seconds = self._base_auto_stop_seconds
            self._recordingStartFailed.emit(str(exc))
            return

        status = (
            f"{auto_stop_override:g} sn hızlı klip kaydediliyor..."
            if auto_stop_override is not None
            else f"Kaydediliyor — durdurmak için {self._config.capture.hotkey}"
        )
        self._recordingStarted.emit(status)

    @Slot(str)
    def _on_recording_started(self, status: str) -> None:
        self._start_in_flight = False
        if self._shutting_down or not self._capture_pending or self._stop_in_flight:
            return
        if self._capture_engine.state == CaptureState.RECORDING:
            self._set_ui_state(CaptureState.RECORDING)
            self._set_status(status)

    @Slot(str)
    def _on_recording_start_failed(self, error: str) -> None:
        self._start_in_flight = False
        self._capture_pending = False
        self._config.capture.auto_stop_seconds = self._base_auto_stop_seconds
        if self._shutting_down:
            return
        self._set_ui_state(CaptureState.ERROR, f"Kayıt başlatılamadı: {error}")

    @Slot()
    def stop_recording(self) -> None:
        """Stop an active capture asynchronously off the Qt UI thread."""
        if self._shutting_down or self._stop_in_flight or not self._capture_pending:
            return
        self._stop_in_flight = True
        self._set_ui_state(CaptureState.STOPPING)
        self._set_status("Kayıt sonlandırılıyor...")
        self._executor.submit(self._worker_stop_recording)

    def _worker_stop_recording(self) -> None:
        try:
            result = self._capture_engine.stop_recording()
        except Exception as exc:
            result = CaptureResult(
                raw_file_path=self._config.capture.temp_dir / self._config.capture.temp_filename,
                duration_sec=0.0,
                width=0,
                height=0,
                fps=float(self._config.capture.fps),
                video_codec="h264",
                has_audio=False,
                encoder_used="",
                start_latency_ms=0.0,
                error=str(exc),
            )
        self._stopCompleted.emit(result)

    @Slot(object)
    def _handle_capture_result(self, result: CaptureResult) -> None:
        self._config.capture.auto_stop_seconds = self._base_auto_stop_seconds
        self._start_in_flight = False
        self._stop_in_flight = False
        if self._shutting_down:
            return

        # Deduplicate when manual stop and auto-stop overlap on the same capture.
        if not self._capture_pending:
            return
        self._capture_pending = False

        self.captureFinished.emit(result)

        if result.is_valid:
            self._unhandled_capture_path = result.raw_file_path
            self._set_ui_state(CaptureState.COLD)
            self._set_status(
                f"Kayıt tamamlandı ({result.duration_sec:.2f} sn) — Kırpıcı hazır"
            )
            trimmer = self._ensure_trimmer_window()
            trimmer.load_capture(result, output_dir=self._config.output_dir)
            trimmer.show()
            trimmer.raise_()
            trimmer.activateWindow()
        else:
            err = result.error or "Geçersiz veya boş kayıt dosyası."
            self._set_ui_state(CaptureState.ERROR, f"Kayıt başarısız: {err}")
            self._schedule_warm_up(cleanup_path=result.raw_file_path)

    @Slot(object)
    def _on_trimmer_export_requested(self, request: TrimRequest) -> None:
        if self._shutting_down or self._export_in_flight:
            return
        self._export_in_flight = True
        if self._trimmer_window is not None:
            self._trimmer_window.set_exporting(True, "Klip dışa aktarılıyor...")
        self._set_status("Klip dışa aktarılıyor...")
        self._executor.submit(self._worker_export, request)

    def _worker_export(self, request: TrimRequest) -> None:
        try:
            result = self._exporter.export(request)
        except Exception as exc:
            result = TrimResult(
                success=False,
                output_path=None,
                duration_sec=0.0,
                elapsed_ms=0.0,
                source_cleaned=False,
                command_executed=[],
                error=str(exc),
            )
        self._exportCompleted.emit(result)

    @Slot(object)
    def _on_export_completed(self, result: TrimResult) -> None:
        self._export_in_flight = False
        if self._shutting_down:
            return

        self.exportFinished.emit(result)

        if self._trimmer_window is not None:
            self._trimmer_window.show_export_result(result, auto_close_ms=self._auto_close_ms)

        if result.success:
            self._unhandled_capture_path = None
            filename = result.output_path.name if result.output_path is not None else "clip.mp4"
            self._set_status(f"Kaydedildi: {filename} ({result.duration_sec:.2f} sn)")
            self._schedule_warm_up()
        else:
            err = result.error or "Bilinmeyen dışa aktarma hatası."
            self._set_status(f"Dışa aktarma başarısız: {err}")
            if self._trimmer_window is None or not self._trimmer_window.isVisible():
                leftover = self._unhandled_capture_path
                self._unhandled_capture_path = None
                self._schedule_warm_up(cleanup_path=leftover)

    @Slot(Path)
    def _on_trimmer_discard_requested(self, source_path: Path) -> None:
        if self._shutting_down or self._export_in_flight:
            return
        self._unhandled_capture_path = None
        if self._trimmer_window is not None and self._trimmer_window.isVisible():
            self._trimmer_window.close()
        self._set_status("Klip silindi, motor yeniden hazırlanıyor...")
        self._schedule_warm_up(cleanup_path=Path(source_path))

    @Slot()
    def _on_trimmer_closed(self) -> None:
        if self._shutting_down:
            return
        if self._unhandled_capture_path is not None and not self._export_in_flight:
            leftover = self._unhandled_capture_path
            self._unhandled_capture_path = None
            self._set_status("Kırpıcı kapatıldı, geçici dosya temizleniyor...")
            self._schedule_warm_up(cleanup_path=leftover)

    @Slot()
    def shutdown(self) -> None:
        """Cleanly unregister hotkeys, stop capture, join background workers, and clean up."""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._shutting_down = True
        self._selecting_region = False
        self._close_region_overlay()

        if self._trimmer_window is not None:
            try:
                self._trimmer_window.release_media()
                if self._trimmer_window.isVisible():
                    self._trimmer_window.close()
            except Exception:
                logger.debug("Ignoring error closing TrimmerWindow during shutdown.", exc_info=True)

        try:
            self._hotkey_listener.unregister()
        except Exception:
            logger.debug("Ignoring error unregistering HotkeyListener.", exc_info=True)

        try:
            self._capture_engine.shutdown()
        except Exception:
            logger.debug("Ignoring error shutting down CaptureEngine.", exc_info=True)

        self._executor.shutdown(wait=True, cancel_futures=False)

        leftover = self._unhandled_capture_path
        if leftover is None and self._capture_pending:
            leftover = self._capture_engine.output_path
        self._unhandled_capture_path = None
        self._capture_pending = False
        if leftover is not None:
            try:
                self._exporter.cleanup_temp(leftover)
            except Exception:
                logger.debug("Ignoring leftover temp cleanup failure during shutdown.", exc_info=True)

        if self._local_server is not None:
            try:
                self._local_server.close()
            except Exception:
                logger.debug("Ignoring error closing single-instance server.", exc_info=True)
            self._local_server = None
            if self._single_instance_name:
                try:
                    QLocalServer.removeServer(self._single_instance_name)
                except Exception:
                    pass

        if hasattr(self._standby, "_cleanup_resources"):
            self._standby._cleanup_resources()
        if self._standby.isVisible():
            self._standby.close()

        self.shutdownCompleted.emit()
        if self._quit_on_exit:
            app = QCoreApplication.instance()
            if app is not None:
                app.quit()


def run_app(
    config: AppConfig | None = None,
    *,
    enable_tray: bool = True,
    single_instance_name: str | None = DEFAULT_SINGLE_INSTANCE_NAME,
    region_mode: bool = False,
    settings_path: Path | None = None,
) -> int:
    """Create the Qt application, enforce single-instance, and run the event loop."""
    app = QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QApplication(sys.argv)
    assert isinstance(app, QApplication)

    app.setApplicationName("QuickClip")
    app.setQuitOnLastWindowClosed(False)

    icon_path = resolve_resource_path("assets/quickclip.ico")
    if icon_path.is_file():
        app_icon = QIcon(str(icon_path))
        if not app_icon.isNull():
            app.setWindowIcon(app_icon)

    if single_instance_name and try_activate_existing_instance(single_instance_name):
        return 0

    controller = QuickClipController(
        config=config,
        enable_tray=enable_tray,
        quit_on_exit=True,
        single_instance_name=single_instance_name,
        region_mode=region_mode,
        settings_path=settings_path,
        persist_settings=True,
    )
    app.aboutToQuit.connect(controller.shutdown)
    controller.start(show_standby=True)

    exit_code = app.exec() if owns_app else 0
    controller.shutdown()
    return int(exit_code)
