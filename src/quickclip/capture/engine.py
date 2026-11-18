"""Pre-warmed screen capture engine using DXGI/MSS and FFmpeg rawvideo piping."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from quickclip.capture.audio import resolve_audio_input
from quickclip.capture.monitors import resolve_monitor_rect
from quickclip.contracts import CaptureConfig, CaptureResult, CaptureState
from quickclip.paths import resolve_ffmpeg_path, resolve_ffprobe_path

logger = logging.getLogger(__name__)

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

HARDWARE_ENCODERS: tuple[str, ...] = ("h264_amf", "h264_nvenc", "h264_qsv")
ENCODER_CANDIDATES: tuple[str, ...] = (*HARDWARE_ENCODERS, "libx264")

_ENCODER_CACHE_LOCK = threading.Lock()
_ENCODER_CACHE: dict[tuple[str, bool], str] = {}


def clear_encoder_cache() -> None:
    """Clear the module-level encoder probe cache."""
    with _ENCODER_CACHE_LOCK:
        _ENCODER_CACHE.clear()


def build_encoder_args(encoder: str, gop: int) -> list[str]:
    """Return verified FFmpeg H.264 encoder flags with no B-frames and fixed GOP.

    Note on ``h264_amf`` (FFmpeg 9.0.2):
        AMF ``-usage ultralowlatency`` and ``-usage lowlatency`` ignore ``-g`` and
        emit only a single IDR frame at ``t=0``. Using ``-usage transcoding``
        combined with ``-quality speed -bf 0 -forced_idr true -g <gop>`` honors
        the requested keyframe cadence with zero B-frames so downstream ``-c copy``
        trimming seeks accurately.
    """
    safe_gop = max(1, int(gop))
    if encoder == "h264_amf":
        return [
            "-c:v",
            "h264_amf",
            "-usage",
            "transcoding",
            "-quality",
            "speed",
            "-rc",
            "cqp",
            "-qp_i",
            "20",
            "-qp_p",
            "20",
            "-bf",
            "0",
            "-forced_idr",
            "true",
            "-g",
            str(safe_gop),
        ]
    if encoder == "h264_nvenc":
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p1",
            "-tune",
            "ll",
            "-rc",
            "constqp",
            "-qp",
            "20",
            "-bf",
            "0",
            "-forced-idr",
            "1",
            "-g",
            str(safe_gop),
        ]
    if encoder == "h264_qsv":
        return [
            "-c:v",
            "h264_qsv",
            "-preset",
            "veryfast",
            "-bf",
            "0",
            "-g",
            str(safe_gop),
        ]
    return [
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-crf",
        "20",
        "-bf",
        "0",
        "-g",
        str(safe_gop),
    ]


class ScreenGrabber(Protocol):
    """Internal screen grabber interface returning raw BGRA frame bytes."""

    @property
    def backend_name(self) -> str:
        """Identifier for the active grabber backend."""
        ...

    @property
    def width(self) -> int:
        """Capture width in pixels."""
        ...

    @property
    def height(self) -> int:
        """Capture height in pixels."""
        ...

    def prepare_thread(self) -> None:
        """Initialize thread-local resources (e.g. COM/GDI) on the capture thread."""
        ...

    def grab(self) -> bytes | bytearray | memoryview | None:
        """Grab one BGRA frame, or return None if no new frame is available."""
        ...

    def cleanup_thread(self) -> None:
        """Release thread-local resources initialized in ``prepare_thread``."""
        ...

    def close(self) -> None:
        """Release grabber resources permanently."""
        ...


class _DXCamGrabber:
    """DXGI Desktop Duplication grabber backed by ``dxcam`` (numpy BGRA mode)."""

    def __init__(
        self,
        monitor_index: int,
        target_rect: tuple[int, int, int, int] | None = None,
    ) -> None:
        if monitor_index < 1:
            raise ValueError("DXCam requires monitor_index >= 1.")

        import numpy as np  # noqa: F401
        import dxcam

        self._lock = threading.Lock()
        self._thread_local = threading.local()
        self._error_logged = False
        self._closed = False

        device_idx, output_idx = self._match_dxcam_output(
            dxcam,
            monitor_index=monitor_index,
            target_rect=target_rect,
        )
        cam = dxcam.create(
            device_idx=device_idx,
            output_idx=output_idx,
            output_color="BGRA",
            processor_backend="numpy",
        )
        self._cam = cam
        self._width = int(cam.width)
        self._height = int(cam.height)

        # Verify that DuplicateOutput frame acquisition actually works in this desktop session.
        try:
            self._probe_grab()
        except Exception:
            self.close()
            raise

    @staticmethod
    def _match_dxcam_output(
        dxcam_mod: Any,
        *,
        monitor_index: int,
        target_rect: tuple[int, int, int, int] | None,
    ) -> tuple[int, int | None]:
        """Match MSS monitor geometry against DXCam outputs when available."""
        if target_rect is not None:
            factory = getattr(dxcam_mod, "__factory", None)
            outputs_by_dev = getattr(factory, "outputs", None)
            if isinstance(outputs_by_dev, list):
                for d_idx, dev_outputs in enumerate(outputs_by_dev):
                    for o_idx, out in enumerate(dev_outputs):
                        desc = getattr(out, "desc", None)
                        coords = getattr(desc, "DesktopCoordinates", None)
                        if coords is None:
                            continue
                        rect = (
                            int(coords.left),
                            int(coords.top),
                            int(coords.right - coords.left),
                            int(coords.bottom - coords.top),
                        )
                        if rect == target_rect:
                            return d_idx, o_idx
        return 0, (None if monitor_index == 1 else monitor_index - 1)

    @property
    def backend_name(self) -> str:
        return "dxcam"

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    def prepare_thread(self) -> None:
        try:
            import comtypes

            comtypes.CoInitialize()
            self._thread_local.com_initialized = True
        except Exception:
            self._thread_local.com_initialized = False

    def cleanup_thread(self) -> None:
        if getattr(self._thread_local, "com_initialized", False):
            try:
                import comtypes

                comtypes.CoUninitialize()
            except Exception:
                pass
            self._thread_local.com_initialized = False

    def _probe_grab(self) -> bytes | bytearray | memoryview | None:
        with self._lock:
            frame = self._cam.grab()
        if frame is None:
            return None
        return self._frame_to_bgra(frame)

    def _frame_to_bgra(self, frame: Any) -> bytes | bytearray | memoryview:
        import numpy as np

        arr = np.asarray(frame, dtype=np.uint8)
        if not arr.flags.c_contiguous:
            arr = np.ascontiguousarray(arr, dtype=np.uint8)
        return memoryview(arr.reshape(-1))

    def grab(self) -> bytes | bytearray | memoryview | None:
        with self._lock:
            if self._closed or self._cam is None:
                return None
            try:
                frame = self._cam.grab()
            except Exception as exc:
                if not self._error_logged:
                    logger.warning("DXCam grab error (reusing last frame): %s", exc)
                    self._error_logged = True
                return None
        if frame is None:
            return None
        self._error_logged = False
        return self._frame_to_bgra(frame)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            cam = self._cam
            self._cam = None
        if cam is not None:
            try:
                cam.release()
            except Exception:
                logger.debug("Ignoring error releasing DXCamera.", exc_info=True)


class _MSSGrabber:
    """Fallback GDI screen grabber backed by ``mss.MSS``."""

    def __init__(self, monitor_index: int) -> None:
        import mss

        self._mss_mod = mss
        self._lock = threading.Lock()
        self._sct: Any = None
        self._sct_tid: int | None = None
        self._error_logged = False

        with mss.MSS() as sct:
            monitors = list(sct.monitors)
            if not monitors:
                raise RuntimeError("MSS found no display monitors.")
            if monitor_index == 0:
                chosen = dict(monitors[0])
            elif 1 <= monitor_index < len(monitors):
                chosen = dict(monitors[monitor_index])
            else:
                fallback_idx = 1 if len(monitors) > 1 else 0
                logger.warning(
                    "Monitor index %d out of range (1..%d); falling back to %d.",
                    monitor_index,
                    len(monitors) - 1,
                    fallback_idx,
                )
                chosen = dict(monitors[fallback_idx])

            self._monitor = {
                "left": int(chosen["left"]),
                "top": int(chosen["top"]),
                "width": int(chosen["width"]),
                "height": int(chosen["height"]),
            }
            self._width = self._monitor["width"]
            self._height = self._monitor["height"]
            if self._width <= 0 or self._height <= 0:
                raise RuntimeError(
                    f"Invalid monitor dimensions: {self._width}x{self._height}"
                )

            # Seed an initial frame if BitBlt is permitted on the current desktop.
            self._seed_frame: bytes | None = None
            try:
                shot = sct.grab(self._monitor)
                self._seed_frame = bytes(shot.raw)
            except Exception as exc:
                logger.warning(
                    "Initial MSS BitBlt unavailable (%s); using blank seed frame until desktop is accessible.",
                    exc,
                )

    @property
    def backend_name(self) -> str:
        return "mss"

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def seed_frame(self) -> bytes | None:
        return self._seed_frame

    def _ensure_thread_sct(self) -> Any:
        tid = threading.get_ident()
        if self._sct is not None and self._sct_tid != tid:
            try:
                self._sct.close()
            except Exception:
                pass
            self._sct = None
            self._sct_tid = None
        if self._sct is None:
            self._sct = self._mss_mod.MSS()
            self._sct_tid = tid
        return self._sct

    def prepare_thread(self) -> None:
        with self._lock:
            self._ensure_thread_sct()

    def cleanup_thread(self) -> None:
        with self._lock:
            sct = self._sct
            self._sct = None
            self._sct_tid = None
        if sct is not None:
            try:
                sct.close()
            except Exception:
                pass

    def grab(self) -> bytes | bytearray | memoryview | None:
        with self._lock:
            sct = self._ensure_thread_sct()
            try:
                shot = sct.grab(self._monitor)
                self._error_logged = False
                return shot.raw
            except Exception as exc:
                if not self._error_logged:
                    logger.warning("MSS grab error (reusing last frame): %s", exc)
                    self._error_logged = True
                return None

    def close(self) -> None:
        self.cleanup_thread()


def _create_default_grabber(config: CaptureConfig) -> tuple[ScreenGrabber, bytes | None]:
    """Initialize ``dxcam`` (primary) or fall back to ``mss``."""
    if config.monitor_index < 0:
        raise ValueError(f"monitor_index must be >= 0, got {config.monitor_index}")

    if config.monitor_index >= 1:
        target_rect = resolve_monitor_rect(config.monitor_index)
        try:
            dxcam_grabber = _DXCamGrabber(config.monitor_index, target_rect=target_rect)
            seed: bytes | None = None
            first = dxcam_grabber.grab()
            if first is not None:
                seed = bytes(first)
            return dxcam_grabber, seed
        except Exception as exc:
            logger.info("DXCam unavailable (%s); falling back to MSS.", exc)

    mss_grabber = _MSSGrabber(config.monitor_index)
    return mss_grabber, mss_grabber.seed_frame


def _parse_hhmmss_duration(raw: str) -> float | None:
    """Parse ``HH:MM:SS.ffffff`` duration tags emitted by Matroska."""
    text = raw.strip()
    parts = text.split(":")
    if len(parts) != 3:
        return None
    try:
        hours = float(parts[0])
        minutes = float(parts[1])
        seconds = float(parts[2])
    except ValueError:
        return None
    total = hours * 3600.0 + minutes * 60.0 + seconds
    return total if total > 0 else None


class _WarmupAbortedError(RuntimeError):
    """Internal sentinel raised when ``shutdown()`` aborts an in-flight ``warm_up()``."""


class CaptureEngine:
    """Pre-warmed screen capture engine implementing ``ICaptureEngine``.

    ``warm_up()`` probes the fastest available H.264 encoder once, initializes
    DXGI (``dxcam``) or GDI (``mss``) screen capture, and pre-spawns an ``ffmpeg``
    process already waiting on raw BGRA frames via ``pipe:0`` so ``start_recording()``
    starts writing frames immediately without process-spawn latency.
    """

    def __init__(
        self,
        config: CaptureConfig,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
        on_auto_stop: Callable[[CaptureResult], None] | None = None,
        *,
        grabber_factory: Callable[[CaptureConfig], ScreenGrabber] | None = None,
    ) -> None:
        self._config = config
        self._ffmpeg_path = ffmpeg_path
        self._ffprobe_path = ffprobe_path
        self._on_auto_stop = on_auto_stop
        self._grabber_factory = grabber_factory

        self._state_lock = threading.Lock()
        self._state: CaptureState = CaptureState.COLD

        self._encoder_used: str | None = None
        self._grabber: ScreenGrabber | None = None
        self._grabber_monitor_index: int | None = None
        self._warmed_monitor_index: int | None = None
        self._seed_frame: bytes | None = None
        self._capture_size: tuple[int, int] | None = None
        self._has_audio: bool = False
        self._audio_spec: str | None = None

        self._proc: subprocess.Popen[bytes] | None = None
        self._stderr_lines: deque[str] = deque(maxlen=100)
        self._stderr_thread: threading.Thread | None = None

        self._capture_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._first_frame_event = threading.Event()
        self._stop_done_event = threading.Event()
        self._warm_done_event = threading.Event()
        self._warm_done_event.set()
        self._shutdown_requested = threading.Event()

        self._auto_stop_timer: threading.Timer | None = None
        self._last_result: CaptureResult | None = None

        self._start_latency_ms: float = 0.0
        self._frames_written: int = 0
        self._recording_start_time: float = 0.0
        self._pipe_error: str | None = None

    @property
    def state(self) -> CaptureState:
        """Return the current lifecycle state of the capture engine."""
        with self._state_lock:
            return self._state

    @property
    def encoder_used(self) -> str | None:
        """Return the probed/selected H.264 encoder name, if known."""
        return self._encoder_used

    @property
    def grabber_backend(self) -> str | None:
        """Return the active screen grabber backend name (``dxcam``, ``mss``, etc.)."""
        return self._grabber.backend_name if self._grabber is not None else None

    @property
    def capture_size(self) -> tuple[int, int] | None:
        """Return ``(width, height)`` of the configured capture target, if warmed."""
        return self._capture_size

    @property
    def has_audio(self) -> bool:
        """Return True if a usable audio device was attached to the capture pipeline."""
        return self._has_audio

    @property
    def output_path(self) -> Path:
        """Return the raw temporary file path configured for this engine."""
        return self._config.temp_dir / self._config.temp_filename

    @property
    def resolved_ffmpeg_path(self) -> str:
        """Return the resolved ``ffmpeg`` binary path (bundled or ``PATH``)."""
        return resolve_ffmpeg_path(self._ffmpeg_path)

    @property
    def resolved_ffprobe_path(self) -> str:
        """Return the resolved ``ffprobe`` binary path (bundled or ``PATH``)."""
        return resolve_ffprobe_path(self._ffprobe_path)

    def _check_not_shutting_down(self) -> None:
        if self._shutdown_requested.is_set():
            raise _WarmupAbortedError("CaptureEngine shut down during warm_up().")

    def warm_up(self) -> None:
        """Probe encoder once, initialize grabber, and pre-spawn standby FFmpeg."""
        if self._config.fps <= 0:
            raise ValueError(f"fps must be > 0, got {self._config.fps}")
        if self._config.keyframe_interval_sec <= 0:
            raise ValueError(
                f"keyframe_interval_sec must be > 0, got {self._config.keyframe_interval_sec}"
            )
        if self._config.monitor_index < 0:
            raise ValueError(
                f"monitor_index must be >= 0, got {self._config.monitor_index}"
            )

        with self._state_lock:
            if self._state == CaptureState.STANDBY:
                if (
                    self._proc is not None
                    and self._proc.poll() is None
                    and self._warmed_monitor_index == self._config.monitor_index
                    and not self._shutdown_requested.is_set()
                ):
                    return
            elif self._state in (CaptureState.WARMING, CaptureState.RECORDING, CaptureState.STOPPING):
                raise RuntimeError(
                    f"Cannot call warm_up() while engine is in state {self._state.value}."
                )
            self._state = CaptureState.WARMING
            self._warmed_monitor_index = None
            self._shutdown_requested.clear()
            self._warm_done_event.clear()
            self._last_result = None

        try:
            self._terminate_standby_process(remove_incomplete=True)
            self._check_not_shutting_down()

            encoder = self._probe_and_cache_encoder()
            self._encoder_used = encoder
            self._check_not_shutting_down()

            if self._grabber is not None and self._grabber_monitor_index != self._config.monitor_index:
                self._release_grabber()

            if self._grabber is None:
                if self._grabber_factory is not None:
                    grabber = self._grabber_factory(self._config)
                    seed_raw = grabber.grab()
                    seed = bytes(seed_raw) if seed_raw is not None else None
                else:
                    grabber, seed = _create_default_grabber(self._config)
                self._grabber = grabber
                self._grabber_monitor_index = self._config.monitor_index
            else:
                grabber = self._grabber
                try:
                    seed_raw = grabber.grab()
                    seed = bytes(seed_raw) if seed_raw is not None else self._seed_frame
                except Exception:
                    seed = self._seed_frame

            self._check_not_shutting_down()

            width = int(grabber.width)
            height = int(grabber.height)
            if width <= 0 or height <= 0:
                raise RuntimeError(f"Invalid grabber frame dimensions: {width}x{height}")

            expected_len = width * height * 4
            if seed is not None and len(seed) == expected_len:
                self._seed_frame = seed
            else:
                self._seed_frame = bytes(expected_len)
            self._capture_size = (width, height)

            audio_spec = resolve_audio_input(
                capture_audio=self._config.capture_audio,
                audio_device=self._config.audio_device,
                ffmpeg_path=self.resolved_ffmpeg_path,
                validate=True,
            )
            self._audio_spec = audio_spec
            self._has_audio = audio_spec is not None
            self._check_not_shutting_down()

            self._config.temp_dir.mkdir(parents=True, exist_ok=True)
            cmd = self._build_standby_command(
                width=width,
                height=height,
                encoder=encoder,
                audio_spec=audio_spec,
            )

            self._check_not_shutting_down()
            self._stderr_lines.clear()
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=_CREATE_NO_WINDOW,
            )
            self._proc = proc
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr,
                args=(proc,),
                name="QuickClip-FFmpegStderr",
                daemon=True,
            )
            self._stderr_thread.start()

            # Briefly ensure FFmpeg did not immediately reject options or paths,
            # waking immediately if shutdown() is requested.
            if self._shutdown_requested.wait(timeout=0.05):
                raise _WarmupAbortedError("CaptureEngine shut down during warm_up().")
            rc = proc.poll()
            if rc is not None:
                err_tail = "\n".join(self._stderr_lines)
                raise RuntimeError(
                    f"Standby FFmpeg exited prematurely with code {rc}: {err_tail}"
                )

            with self._state_lock:
                self._check_not_shutting_down()
                self._warmed_monitor_index = self._config.monitor_index
                self._state = CaptureState.STANDBY
        except _WarmupAbortedError:
            self._terminate_standby_process(remove_incomplete=True)
            self._release_grabber()
            with self._state_lock:
                self._state = CaptureState.COLD
            raise
        except Exception:
            self._terminate_standby_process(remove_incomplete=True)
            self._release_grabber()
            with self._state_lock:
                self._state = (
                    CaptureState.COLD
                    if self._shutdown_requested.is_set()
                    else CaptureState.ERROR
                )
            raise
        finally:
            self._warm_done_event.set()

    def start_recording(self) -> None:
        """Immediately begin feeding grabbed BGRA frames into the pre-warmed FFmpeg pipe."""
        entry_time = time.perf_counter()
        auto_stop = self._config.auto_stop_seconds

        with self._state_lock:
            if self._shutdown_requested.is_set():
                raise RuntimeError("Cannot start_recording() while CaptureEngine is shutting down.")
            if self._state != CaptureState.STANDBY:
                raise RuntimeError(
                    f"Cannot start_recording() from state {self._state.value}; call warm_up() first."
                )
            proc = self._proc
            if proc is None or proc.poll() is not None or proc.stdin is None:
                self._state = CaptureState.ERROR
                raise RuntimeError("Standby FFmpeg process is not running; call warm_up() again.")
            if self._grabber is None or self._capture_size is None:
                self._state = CaptureState.ERROR
                raise RuntimeError("Screen grabber is not initialized; call warm_up() again.")

            self._stop_event.clear()
            self._first_frame_event.clear()
            self._stop_done_event.clear()
            self._last_result = None
            self._frames_written = 0
            self._start_latency_ms = 0.0
            self._pipe_error = None
            self._recording_start_time = entry_time

            capture_thread = threading.Thread(
                target=self._capture_loop,
                args=(entry_time,),
                name="QuickClip-CaptureLoop",
                daemon=True,
            )
            self._capture_thread = capture_thread

            timer: threading.Timer | None = None
            if auto_stop is not None and auto_stop > 0:
                timer = threading.Timer(float(auto_stop), self._on_auto_stop_timer)
                timer.daemon = True
            self._auto_stop_timer = timer

            self._state = CaptureState.RECORDING
            capture_thread.start()
            if timer is not None:
                timer.start()

    def set_monitor(self, monitor_index: int, *, rewarm: bool = False) -> None:
        """Switch the target display ``monitor_index`` when not recording or warming."""
        target_index = int(monitor_index)
        if target_index < 0:
            raise ValueError(f"monitor_index must be >= 0, got {monitor_index}")

        needs_reset = False
        with self._state_lock:
            if self._state in (
                CaptureState.WARMING,
                CaptureState.RECORDING,
                CaptureState.STOPPING,
            ):
                raise RuntimeError(
                    f"Cannot change monitor while engine is in state {self._state.value}."
                )
            changed = self._config.monitor_index != target_index
            self._config.monitor_index = target_index
            if changed or self._grabber_monitor_index != target_index:
                needs_reset = True
                self._warmed_monitor_index = None
                if self._state == CaptureState.STANDBY:
                    self._state = CaptureState.COLD

        if needs_reset:
            self._terminate_standby_process(remove_incomplete=True)
            self._release_grabber()
            self._capture_size = None

        if rewarm:
            self.warm_up()

    def stop_recording(self) -> CaptureResult:
        """Stop the active recording cleanly and return capture metadata."""
        return self._stop_internal(from_auto_stop=False)

    def shutdown(self) -> None:
        """Cancel timers/capture, terminate any standby FFmpeg process, and release grabber."""
        with self._state_lock:
            self._shutdown_requested.set()
            self._cancel_auto_stop_timer_locked()
            current_state = self._state

        if current_state == CaptureState.WARMING or not self._warm_done_event.is_set():
            self._warm_done_event.wait(timeout=15.0)
            self._terminate_standby_process(remove_incomplete=True)
        elif current_state in (CaptureState.RECORDING, CaptureState.STOPPING):
            self._stop_event.set()
            if current_state == CaptureState.RECORDING:
                try:
                    self._stop_internal(from_auto_stop=False)
                except Exception:
                    logger.debug("Ignoring error stopping active capture during shutdown.", exc_info=True)
            else:
                self._stop_done_event.wait(timeout=15.0)
        elif current_state == CaptureState.STANDBY:
            self._terminate_standby_process(remove_incomplete=True)
        else:
            self._terminate_standby_process(remove_incomplete=False)

        self._release_grabber()

        with self._state_lock:
            self._warmed_monitor_index = None
            self._state = CaptureState.COLD

    def _on_auto_stop_timer(self) -> None:
        self._stop_internal(from_auto_stop=True)

    def _cancel_auto_stop_timer_locked(self) -> None:
        timer = self._auto_stop_timer
        self._auto_stop_timer = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def _cancel_auto_stop_timer(self) -> None:
        with self._state_lock:
            self._cancel_auto_stop_timer_locked()

    def _stop_internal(self, *, from_auto_stop: bool) -> CaptureResult:
        won_stop = False
        prior_result: CaptureResult | None = None
        state_snapshot = CaptureState.COLD

        with self._state_lock:
            state_snapshot = self._state
            if self._state == CaptureState.RECORDING:
                self._state = CaptureState.STOPPING
                self._cancel_auto_stop_timer_locked()
                won_stop = True
            elif self._state == CaptureState.STOPPING:
                won_stop = False
            else:
                prior_result = self._last_result

        if not won_stop:
            if state_snapshot == CaptureState.STOPPING:
                if from_auto_stop:
                    return self._make_error_result("Auto-stop superseded by active stop.")
                self._stop_done_event.wait(timeout=15.0)
                if self._last_result is not None:
                    return self._last_result
                return self._make_error_result("Stop completed without a capture result.")

            if prior_result is not None and not from_auto_stop:
                return prior_result
            return self._make_error_result(
                f"Cannot stop recording from state {state_snapshot.value}."
            )

        if not self._shutdown_requested.is_set():
            self._first_frame_event.wait(timeout=0.5)

        self._stop_event.set()
        capture_thread = self._capture_thread
        if capture_thread is not None and capture_thread is not threading.current_thread():
            capture_thread.join(timeout=5.0)
        self._capture_thread = None

        proc = self._proc
        self._proc = None

        if proc is not None:
            if proc.stdin is not None:
                try:
                    proc.stdin.flush()
                except Exception:
                    pass
                try:
                    proc.stdin.close()
                except Exception:
                    pass

            try:
                proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                logger.warning("FFmpeg did not exit in time after stdin close; terminating.")
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2.0)

        stderr_thread = self._stderr_thread
        self._stderr_thread = None
        if stderr_thread is not None and stderr_thread.is_alive():
            stderr_thread.join(timeout=1.0)

        wall_elapsed = max(0.0, time.perf_counter() - self._recording_start_time)
        result = self._build_capture_result(proc=proc, wall_elapsed=wall_elapsed)

        with self._state_lock:
            self._last_result = result
            self._state = (
                CaptureState.COLD
                if (self._shutdown_requested.is_set() or not result.error)
                else CaptureState.ERROR
            )
        self._stop_done_event.set()

        if (
            from_auto_stop
            and self._on_auto_stop is not None
            and not self._shutdown_requested.is_set()
        ):
            try:
                self._on_auto_stop(result)
            except Exception:
                logger.exception("Unhandled exception inside on_auto_stop callback.")

        return result

    def _capture_loop(self, entry_time: float) -> None:
        grabber = self._grabber
        proc = self._proc
        if grabber is None or proc is None or proc.stdin is None or self._capture_size is None:
            return

        width, height = self._capture_size
        expected_len = width * height * 4
        fps = float(self._config.fps)
        frame_interval = 1.0 / fps
        last_frame: bytes | bytearray | memoryview = (
            self._seed_frame if self._seed_frame is not None else bytes(expected_len)
        )

        grabber.prepare_thread()
        try:
            if self._stop_event.is_set():
                return

            # Write frame 0 immediately to minimize start_latency_ms.
            first_grab = self._safe_grab(grabber, expected_len)
            if first_grab is not None:
                last_frame = first_grab
            try:
                proc.stdin.write(last_frame)
            except (BrokenPipeError, OSError) as exc:
                self._pipe_error = f"FFmpeg stdin write failed on initial frame: {exc}"
                return

            first_write_done = time.perf_counter()
            self._start_latency_ms = max(0.0, (first_write_done - entry_time) * 1000.0)
            self._frames_written = 1
            self._first_frame_event.set()

            frame_index = 1
            max_catchup = 3

            while not self._stop_event.is_set():
                target_time = first_write_done + frame_index * frame_interval
                remaining = target_time - time.perf_counter()
                if remaining > 0:
                    if self._stop_event.wait(timeout=remaining):
                        break

                grabbed = self._safe_grab(grabber, expected_len)
                if grabbed is not None:
                    last_frame = grabbed

                if self._stop_event.is_set():
                    break

                # Determine how many frames to emit to stay synced to wall clock.
                now = time.perf_counter()
                behind_frames = int((now - target_time) / frame_interval)
                emit_count = 1 + min(max(0, behind_frames), max_catchup)

                for _ in range(emit_count):
                    if self._stop_event.is_set():
                        break
                    try:
                        proc.stdin.write(last_frame)
                    except (BrokenPipeError, OSError) as exc:
                        self._pipe_error = f"FFmpeg stdin pipe closed during capture: {exc}"
                        return
                    self._frames_written += 1
                    frame_index += 1
        finally:
            self._first_frame_event.set()
            grabber.cleanup_thread()

    @staticmethod
    def _safe_grab(
        grabber: ScreenGrabber,
        expected_len: int,
    ) -> bytes | bytearray | memoryview | None:
        try:
            frame = grabber.grab()
        except Exception:
            return None
        if frame is None:
            return None
        if len(frame) != expected_len:
            return None
        return frame

    def _probe_and_cache_encoder(self) -> str:
        resolved_ffmpeg = self.resolved_ffmpeg_path
        key = (resolved_ffmpeg, bool(self._config.prefer_hardware_encoder))
        with _ENCODER_CACHE_LOCK:
            cached = _ENCODER_CACHE.get(key)
        if cached is not None:
            return cached

        candidates = ENCODER_CANDIDATES if self._config.prefer_hardware_encoder else ("libx264",)
        errors: list[str] = []

        for encoder in candidates:
            cmd = [
                resolved_ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=256x256:d=0.05",
                "-frames:v",
                "1",
                *build_encoder_args(encoder, gop=30),
                "-pix_fmt",
                "yuv420p",
                "-f",
                "null",
                "-",
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10.0,
                    creationflags=_CREATE_NO_WINDOW,
                )
                if proc.returncode == 0:
                    with _ENCODER_CACHE_LOCK:
                        _ENCODER_CACHE[key] = encoder
                    logger.info("Selected H.264 encoder: %s", encoder)
                    return encoder
                errors.append(f"{encoder}: rc={proc.returncode} ({proc.stderr.strip()})")
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append(f"{encoder}: {exc}")

        raise RuntimeError(
            "No working H.264 encoder found via FFmpeg dry-run: " + "; ".join(errors)
        )

    def _build_standby_command(
        self,
        *,
        width: int,
        height: int,
        encoder: str,
        audio_spec: str | None,
    ) -> list[str]:
        fps = int(self._config.fps)
        gop = max(1, round(fps * self._config.keyframe_interval_sec))

        cmd: list[str] = [
            self.resolved_ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgra",
            "-s",
            f"{width}x{height}",
            "-r",
            str(fps),
            "-i",
            "pipe:0",
        ]
        if audio_spec is not None:
            cmd.extend(["-thread_queue_size", "512", "-f", "dshow", "-i", audio_spec])

        cmd.extend(build_encoder_args(encoder, gop=gop))
        if width % 2 != 0 or height % 2 != 0:
            cmd.extend(["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"])
        cmd.extend(["-pix_fmt", "yuv420p"])

        if audio_spec is not None:
            cmd.extend(["-c:a", "aac", "-b:a", "128k", "-shortest"])

        cmd.extend(["-f", "matroska", str(self.output_path)])
        return cmd

    def _drain_stderr(self, proc: subprocess.Popen[bytes]) -> None:
        if proc.stderr is None:
            return
        try:
            for raw_line in iter(proc.stderr.readline, b""):
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line:
                    self._stderr_lines.append(line)
        except Exception:
            pass
        finally:
            try:
                proc.stderr.close()
            except Exception:
                pass

    def _terminate_standby_process(self, *, remove_incomplete: bool) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2.0)

        stderr_thread = self._stderr_thread
        self._stderr_thread = None
        if stderr_thread is not None and stderr_thread.is_alive():
            stderr_thread.join(timeout=1.0)

        if remove_incomplete:
            try:
                if self.output_path.is_file():
                    self.output_path.unlink()
            except OSError:
                pass

    def _release_grabber(self) -> None:
        grabber = self._grabber
        self._grabber = None
        self._grabber_monitor_index = None
        self._seed_frame = None
        if grabber is not None:
            try:
                grabber.close()
            except Exception:
                logger.debug("Ignoring error closing grabber.", exc_info=True)

    def _build_capture_result(
        self,
        *,
        proc: subprocess.Popen[bytes] | None,
        wall_elapsed: float,
    ) -> CaptureResult:
        out_path = self.output_path
        width, height = self._capture_size or (0, 0)
        fps = float(self._config.fps)
        encoder_used = self._encoder_used or ""
        fallback_duration = (
            self._frames_written / fps if self._frames_written > 0 else wall_elapsed
        )

        error_parts: list[str] = []
        if self._pipe_error:
            error_parts.append(self._pipe_error)
        if proc is not None and proc.returncode not in (0, None):
            stderr_tail = "; ".join(self._stderr_lines)
            msg = f"FFmpeg exited with code {proc.returncode}"
            if stderr_tail:
                msg += f": {stderr_tail}"
            error_parts.append(msg)

        if not out_path.is_file() or out_path.stat().st_size == 0:
            error_parts.append(f"Recorded file missing or empty: {out_path}")
            return CaptureResult(
                raw_file_path=out_path,
                duration_sec=0.0,
                width=width,
                height=height,
                fps=fps,
                video_codec="h264",
                has_audio=False,
                encoder_used=encoder_used,
                start_latency_ms=self._start_latency_ms,
                error="; ".join(error_parts),
            )

        probed = self._probe_output_metadata(out_path)
        duration_sec = probed.get("duration_sec")
        if not isinstance(duration_sec, float) or duration_sec <= 0:
            duration_sec = fallback_duration

        return CaptureResult(
            raw_file_path=out_path,
            duration_sec=float(duration_sec),
            width=int(probed.get("width") or width),
            height=int(probed.get("height") or height),
            fps=fps,
            video_codec=str(probed.get("video_codec") or "h264"),
            has_audio=bool(probed.get("has_audio", self._has_audio)),
            encoder_used=encoder_used,
            start_latency_ms=self._start_latency_ms,
            error="; ".join(error_parts) if error_parts else None,
        )

    def _probe_output_metadata(self, path: Path) -> dict[str, Any]:
        cmd = [
            self.resolved_ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,duration:stream_tags=DURATION",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5.0,
                creationflags=_CREATE_NO_WINDOW,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                return {}
            data = json.loads(proc.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            return {}

        info: dict[str, Any] = {}
        fmt = data.get("format") or {}
        raw_fmt_dur = fmt.get("duration")
        if raw_fmt_dur not in (None, "N/A", ""):
            try:
                dur = float(raw_fmt_dur)
                if dur > 0:
                    info["duration_sec"] = dur
            except (TypeError, ValueError):
                pass

        streams = data.get("streams") or []
        has_audio = False
        for stream in streams:
            codec_type = stream.get("codec_type")
            if codec_type == "audio":
                has_audio = True
            elif codec_type == "video" and "video_codec" not in info:
                if stream.get("codec_name"):
                    info["video_codec"] = str(stream["codec_name"])
                if stream.get("width"):
                    info["width"] = int(stream["width"])
                if stream.get("height"):
                    info["height"] = int(stream["height"])
                if "duration_sec" not in info:
                    raw_sdur = stream.get("duration")
                    if raw_sdur not in (None, "N/A", ""):
                        try:
                            sdur = float(raw_sdur)
                            if sdur > 0:
                                info["duration_sec"] = sdur
                        except (TypeError, ValueError):
                            pass
                    if "duration_sec" not in info:
                        tags = stream.get("tags") or {}
                        tag_dur = tags.get("DURATION") or tags.get("duration")
                        if isinstance(tag_dur, str):
                            parsed = _parse_hhmmss_duration(tag_dur)
                            if parsed is not None:
                                info["duration_sec"] = parsed

        info["has_audio"] = has_audio
        return info

    def _make_error_result(self, message: str) -> CaptureResult:
        width, height = self._capture_size or (0, 0)
        return CaptureResult(
            raw_file_path=self.output_path,
            duration_sec=0.0,
            width=width,
            height=height,
            fps=float(self._config.fps),
            video_codec="h264",
            has_audio=self._has_audio,
            encoder_used=self._encoder_used or "",
            start_latency_ms=self._start_latency_ms,
            error=message,
        )
