"""Application controller wiring QuickClip capture, preview trimmer, exporter, and UI."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import logging
from pathlib import Path
import sys
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal, Slot
from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from quickclip.capture import CaptureEngine, HotkeyListener
from quickclip.contracts import (
    AppConfig,
    CaptureConfig,
    CaptureResult,
    CaptureState,
    TrimRequest,
    TrimResult,
)
from quickclip.exporter import ExporterService
from quickclip.paths import resolve_resource_path
import quickclip.startup as startup_mod
from quickclip.ui import StandbyWindow, TrimmerWindow

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

        self._sync_startup_checkbox()
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

        self._start_recording_async(auto_stop_override=float(seconds))

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
    )
    app.aboutToQuit.connect(controller.shutdown)
    controller.start(show_standby=True)

    exit_code = app.exec() if owns_app else 0
    controller.shutdown()
    return int(exit_code)
