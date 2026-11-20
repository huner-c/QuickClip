"""Unit and real-FFmpeg integration tests for QuickClip capture subsystem."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from quickclip.capture import (
    AudioDevice,
    CaptureEngine,
    HotkeyListener,
    build_encoder_args,
    clear_encoder_cache,
    list_audio_devices,
    list_monitors,
    parse_dshow_audio_devices,
    parse_hotkey,
    probe_audio_device,
    resolve_audio_input,
    resolve_monitor_rect,
)
from quickclip.capture.engine import _DXCamGrabber
from quickclip.capture.hotkey import MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, MOD_SHIFT, MOD_WIN, WM_HOTKEY, WM_QUIT
from quickclip.contracts import (
    CaptureConfig,
    CaptureRegion,
    CaptureResult,
    CaptureState,
    ICaptureEngine,
    IHotkeyListener,
    MonitorInfo,
)


class _SyntheticGrabber:
    """Deterministic animated BGRA frame grabber for real-FFmpeg smoke tests."""

    def __init__(self, width: int = 320, height: int = 240, *, static_after_first: bool = False) -> None:
        self._width = width
        self._height = height
        self._static_after_first = static_after_first
        self._counter = 0
        self.closed = False

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

    def cleanup_thread(self) -> None:
        return None

    def grab(self) -> bytes | None:
        self._counter += 1
        if self._static_after_first and self._counter > 1:
            return None
        b = (self._counter * 17) % 256
        g = (self._counter * 41) % 256
        r = (self._counter * 73) % 256
        pixel = bytes((b, g, r, 255))
        return pixel * (self._width * self._height)

    def close(self) -> None:
        self.closed = True


def _ffprobe_info(path: Path) -> dict:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,has_b_frames,pix_fmt",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        text=True,
    )
    return json.loads(out)


def _ffprobe_keyframes(path: Path) -> list[float]:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-skip_frame",
            "nokey",
            "-show_entries",
            "frame=pts_time",
            "-of",
            "csv=p=0",
            str(path),
        ],
        text=True,
    )
    times: list[float] = []
    for line in out.strip().splitlines():
        val = line.strip().rstrip(",")
        if val:
            times.append(float(val))
    return times


# ---------------------------------------------------------------------------
# Protocol & HotkeyListener tests
# ---------------------------------------------------------------------------


def test_protocols_satisfied() -> None:
    engine = CaptureEngine(CaptureConfig())
    listener = HotkeyListener()
    assert isinstance(engine, ICaptureEngine)
    assert isinstance(listener, IHotkeyListener)


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("F9", (0, 0x78)),
        ("f1", (0, 0x70)),
        ("f12", (0, 0x7B)),
        ("ctrl+shift+r", (MOD_CONTROL | MOD_SHIFT, ord("R"))),
        ("ALT + F9", (MOD_ALT, 0x78)),
        ("win+shift+s", (MOD_WIN | MOD_SHIFT, ord("S"))),
        ("ctrl+alt+space", (MOD_CONTROL | MOD_ALT, 0x20)),
    ],
)
def test_parse_hotkey_valid(spec: str, expected: tuple[int, int]) -> None:
    assert parse_hotkey(spec) == expected


@pytest.mark.parametrize("spec", ["", "   ", "ctrl", "ctrl+shift", "a+b", "ctrl++r", "ctrl+unknownkey"])
def test_parse_hotkey_invalid(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_hotkey(spec)


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 RegisterHotKey test")
def test_hotkey_listener_real_win32_register_and_unregister() -> None:
    listener = HotkeyListener(use_keyboard_fallback=False, hotkey_id=97)
    triggered = threading.Event()
    try:
        listener.register("ctrl+alt+shift+f12", triggered.set)
        assert listener.is_registered is True
        assert listener.backend == "win32"
        assert listener.hotkey == "ctrl+alt+shift+f12"

        # Re-registering replaces previous binding cleanly.
        listener.register("ctrl+alt+shift+f11", triggered.set)
        assert listener.is_registered is True
        assert listener.hotkey == "ctrl+alt+shift+f11"
    finally:
        listener.unregister()
        # Verify idempotent unregister.
        listener.unregister()

    assert listener.is_registered is False
    assert listener.backend is None
    assert listener.hotkey is None


def test_hotkey_listener_win32_message_dispatch_and_wm_quit(monkeypatch: pytest.MonkeyPatch) -> None:
    posted_messages: list[tuple[int, int]] = []
    registered_calls: list[tuple[int, int, int]] = []
    unregistered_calls: list[int] = []
    quit_event = threading.Event()
    msg_queue: list[tuple[int, int]] = [(WM_HOTKEY, 7)]

    class FakeUser32:
        def PeekMessageW(self, _msg_ptr, _hwnd, _min, _max, _remove) -> int:
            return 0

        def RegisterHotKey(self, _hwnd, hotkey_id: int, fs_modifiers: int, vk: int) -> int:
            registered_calls.append((hotkey_id, fs_modifiers, vk))
            return 1

        def GetMessageW(self, msg_ptr, _hwnd, _min, _max) -> int:
            msg_obj = ctypes.cast(msg_ptr, ctypes.POINTER(wintypes.MSG)).contents
            if msg_queue:
                m_id, wp = msg_queue.pop(0)
                msg_obj.message = m_id
                msg_obj.wParam = wp
                return 1
            quit_event.wait(timeout=2.0)
            msg_obj.message = WM_QUIT
            msg_obj.wParam = 0
            return 0

        def PostThreadMessageW(self, tid: int, msg_id: int, _wp, _lp) -> int:
            posted_messages.append((int(tid), int(msg_id)))
            if int(msg_id) == WM_QUIT:
                quit_event.set()
            return 1

        def UnregisterHotKey(self, _hwnd, hotkey_id: int) -> int:
            unregistered_calls.append(int(hotkey_id))
            return 1

    class FakeKernel32:
        def GetCurrentThreadId(self) -> int:
            return 4242

        def GetLastError(self) -> int:
            return 0

    fake_windll = MagicMock()
    fake_windll.user32 = FakeUser32()
    fake_windll.kernel32 = FakeKernel32()
    monkeypatch.setattr(ctypes, "windll", fake_windll)

    fired = threading.Event()
    listener = HotkeyListener(use_keyboard_fallback=False, hotkey_id=7)
    listener.register("ctrl+shift+r", fired.set)
    assert fired.wait(timeout=2.0)
    listener.unregister()

    assert registered_calls == [(7, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, ord("R"))]
    assert posted_messages == [(4242, WM_QUIT)]
    assert unregistered_calls == [7]


def test_hotkey_listener_fallback_and_no_fallback_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingUser32:
        def PeekMessageW(self, *_args) -> int:
            return 0

        def RegisterHotKey(self, *_args) -> int:
            return 0

        def UnregisterHotKey(self, *_args) -> int:
            return 1

    class FakeKernel32:
        def GetCurrentThreadId(self) -> int:
            return 1111

        def GetLastError(self) -> int:
            return 1409

    fake_windll = MagicMock()
    fake_windll.user32 = FailingUser32()
    fake_windll.kernel32 = FakeKernel32()
    monkeypatch.setattr(ctypes, "windll", fake_windll)

    no_fb = HotkeyListener(use_keyboard_fallback=False)
    with pytest.raises(RuntimeError, match="1409"):
        no_fb.register("F9", lambda: None)

    removed: list[object] = []
    fake_keyboard = MagicMock()
    fake_keyboard.add_hotkey.return_value = "kb-handle-1"
    fake_keyboard.remove_hotkey.side_effect = removed.append

    with patch.dict(sys.modules, {"keyboard": fake_keyboard}):
        with_fb = HotkeyListener(use_keyboard_fallback=True)
        with_fb.register("Ctrl+Shift+R", lambda: None)
        assert with_fb.backend == "keyboard"
        assert with_fb.is_registered is True
        fake_keyboard.add_hotkey.assert_called_once()
        with_fb.unregister()
        assert removed == ["kb-handle-1"]
        assert with_fb.is_registered is False


# ---------------------------------------------------------------------------
# Audio helpers tests
# ---------------------------------------------------------------------------


def test_parse_dshow_audio_devices_ffmpeg9_and_legacy() -> None:
    ffmpeg9_stderr = """
[in#0 @ 0000029848410d00] Could not enumerate video devices (or none found).
[in#0 @ 0000029848410d00] "Integrated Webcam" (video)
[in#0 @ 0000029848410d00] "Microphone (Steam Streaming Microphone)" (audio)
[in#0 @ 0000029848410d00]   Alternative name "@device_cm_{33D9}/wave_{A65D}"
[in#0 @ 0000029848410d00] "Desktop Mic" (audio)
"""
    devices = parse_dshow_audio_devices(ffmpeg9_stderr)
    assert devices == [
        AudioDevice(
            name="Microphone (Steam Streaming Microphone)",
            alternative_name="@device_cm_{33D9}/wave_{A65D}",
        ),
        AudioDevice(name="Desktop Mic", alternative_name=None),
    ]
    assert devices[0].dshow_spec == "audio=@device_cm_{33D9}/wave_{A65D}"
    assert devices[1].dshow_spec == "audio=Desktop Mic"

    legacy_stderr = """
DirectShow video devices (some may be both video and audio devices)
"OBS Virtual Camera"
DirectShow audio devices
"Microphone (USB Audio)"
  Alternative name "@device_cm_{1111}/wave_{2222}"
Error opening input file dummy.
"""
    legacy_devices = parse_dshow_audio_devices(legacy_stderr)
    assert legacy_devices == [
        AudioDevice(
            name="Microphone (USB Audio)",
            alternative_name="@device_cm_{1111}/wave_{2222}",
        )
    ]


def test_resolve_audio_input_graceful_degradation() -> None:
    assert resolve_audio_input(capture_audio=False, audio_device="Anything") is None

    with patch("quickclip.capture.audio.list_audio_devices", return_value=[]):
        assert resolve_audio_input(capture_audio=True, audio_device=None) is None
        assert (
            resolve_audio_input(
                capture_audio=True,
                audio_device="Missing Mic",
                validate=False,
            )
            is None
        )

    fake_devs = [AudioDevice(name="USB Mic", alternative_name="@alt_usb")]
    with (
        patch("quickclip.capture.audio.list_audio_devices", return_value=fake_devs),
        patch("quickclip.capture.audio.probe_audio_device", return_value=True),
    ):
        assert resolve_audio_input(capture_audio=True, audio_device=None) == "audio=@alt_usb"
        assert resolve_audio_input(capture_audio=True, audio_device="USB Mic") == "audio=@alt_usb"


# ---------------------------------------------------------------------------
# CaptureEngine unit tests (mocked subprocess / state machine)
# ---------------------------------------------------------------------------


def test_encoder_probe_order_and_once_caching(tmp_path: Path) -> None:
    clear_encoder_cache()
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs):
        calls.append(cmd)
        # Fail h264_amf, succeed on h264_nvenc.
        if "h264_amf" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "amf unsupported")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    fake_proc = MagicMock()
    fake_proc.poll.return_value = None
    fake_proc.stdin = MagicMock()
    fake_proc.stderr = None

    cfg = CaptureConfig(temp_dir=tmp_path, prefer_hardware_encoder=True, fps=30)
    with (
        patch("quickclip.capture.engine.subprocess.run", side_effect=fake_run),
        patch("quickclip.capture.engine.subprocess.Popen", return_value=fake_proc),
    ):
        eng1 = CaptureEngine(cfg, grabber_factory=lambda _c: _SyntheticGrabber())
        eng1.warm_up()
        assert eng1.encoder_used == "h264_nvenc"
        assert eng1.state == CaptureState.STANDBY
        assert len(calls) == 2

        # Second engine should reuse cached probe without invoking subprocess.run again.
        eng2 = CaptureEngine(cfg, grabber_factory=lambda _c: _SyntheticGrabber())
        eng2.warm_up()
        assert eng2.encoder_used == "h264_nvenc"
        assert len(calls) == 2

        eng1.shutdown()
        eng2.shutdown()

    clear_encoder_cache()


def test_start_before_warmup_and_shutdown_in_standby(tmp_path: Path) -> None:
    cfg = CaptureConfig(temp_dir=tmp_path, fps=30)
    eng = CaptureEngine(cfg, grabber_factory=lambda _c: _SyntheticGrabber())
    assert eng.state == CaptureState.COLD

    with pytest.raises(RuntimeError, match="warm_up"):
        eng.start_recording()

    clear_encoder_cache()
    eng.warm_up()
    assert eng.state == CaptureState.STANDBY
    standby_proc = eng._proc
    assert standby_proc is not None
    assert standby_proc.poll() is None

    # Idempotent warm_up while in STANDBY keeps the same standby process
    eng.warm_up()
    assert eng.state == CaptureState.STANDBY
    assert eng._proc is standby_proc

    eng.shutdown()
    assert eng.state == CaptureState.COLD
    assert standby_proc.poll() is not None
    # Incomplete standby temp file should be cleaned up when shut down before recording.
    assert not eng.output_path.exists()


# ---------------------------------------------------------------------------
# Real FFmpeg smoke tests
# ---------------------------------------------------------------------------


def test_real_ffmpeg_smoke_synthetic_frames_and_keyframes(tmp_path: Path) -> None:
    clear_encoder_cache()
    cfg = CaptureConfig(
        temp_dir=tmp_path,
        temp_filename="temp_raw_synth.mkv",
        fps=30,
        capture_audio=False,
        prefer_hardware_encoder=True,
        keyframe_interval_sec=0.5,
    )
    grabber = _SyntheticGrabber(320, 240)
    engine = CaptureEngine(cfg, grabber_factory=lambda _c: grabber)

    try:
        engine.warm_up()
        assert engine.state == CaptureState.STANDBY
        assert engine.encoder_used == "h264_amf"
        assert engine.grabber_backend == "synthetic"
        assert engine.capture_size == (320, 240)
        assert engine.has_audio is False

        engine.start_recording()
        assert engine.state == CaptureState.RECORDING
        time.sleep(1.6)

        result = engine.stop_recording()
        assert engine.state == CaptureState.COLD
        assert result.is_valid, f"Capture failed: {result}"
        assert result.error is None
        assert result.encoder_used == "h264_amf"
        assert result.video_codec == "h264"
        assert result.width == 320
        assert result.height == 240
        assert result.has_audio is False
        assert result.start_latency_ms < 250.0
        assert 1.1 <= result.duration_sec <= 2.2

        # Idempotent second stop_recording() returns same result.
        second = engine.stop_recording()
        assert second == result

        probe = _ffprobe_info(result.raw_file_path)
        stream = probe["streams"][0]
        assert stream["codec_name"] == "h264"
        assert stream["has_b_frames"] == 0
        assert stream["width"] == 320
        assert stream["height"] == 240

        keyframes = _ffprobe_keyframes(result.raw_file_path)
        assert len(keyframes) >= 3
        assert keyframes[0] == pytest.approx(0.0, abs=0.05)
        assert keyframes[1] == pytest.approx(0.5, abs=0.1)
    finally:
        engine.shutdown()
        assert grabber.closed is True


def test_real_ffmpeg_static_screen_and_libx264_fallback(tmp_path: Path) -> None:
    clear_encoder_cache()
    cfg = CaptureConfig(
        temp_dir=tmp_path,
        temp_filename="temp_raw_x264.mkv",
        fps=30,
        prefer_hardware_encoder=False,
        keyframe_interval_sec=0.5,
    )
    grabber = _SyntheticGrabber(320, 240, static_after_first=True)
    engine = CaptureEngine(cfg, grabber_factory=lambda _c: grabber)

    try:
        engine.warm_up()
        assert engine.encoder_used == "libx264"
        engine.start_recording()
        time.sleep(1.2)
        result = engine.stop_recording()

        assert result.is_valid
        assert result.encoder_used == "libx264"
        assert result.start_latency_ms < 250.0
        assert result.duration_sec >= 0.8

        keyframes = _ffprobe_keyframes(result.raw_file_path)
        assert len(keyframes) >= 2
    finally:
        engine.shutdown()
        clear_encoder_cache()


def test_real_ffmpeg_screen_grabber_autostop_and_concurrency(tmp_path: Path) -> None:
    clear_encoder_cache()
    auto_results: list[CaptureResult] = []
    auto_done = threading.Event()

    def _on_auto(res: CaptureResult) -> None:
        auto_results.append(res)
        auto_done.set()

    cfg = CaptureConfig(
        temp_dir=tmp_path,
        temp_filename="temp_raw_screen.mkv",
        fps=30,
        prefer_hardware_encoder=True,
        keyframe_interval_sec=0.5,
        monitor_index=1,
        auto_stop_seconds=1.5,
    )
    engine = CaptureEngine(cfg, on_auto_stop=_on_auto)

    try:
        engine.warm_up()
        assert engine.state == CaptureState.STANDBY
        assert engine.encoder_used == "h264_amf"
        assert engine.grabber_backend in {"dxcam", "mss"}

        engine.start_recording()
        assert engine.state == CaptureState.RECORDING
        assert auto_done.wait(timeout=10.0), "auto_stop callback did not fire"

        assert len(auto_results) == 1
        result = auto_results[0]
        assert result.is_valid, f"Screen capture failed: {result}"
        assert result.error is None
        assert result.start_latency_ms < 500.0
        assert 1.0 <= result.duration_sec <= 2.3

        # Manual stop after auto-stop must be safe & idempotent.
        manual_after = engine.stop_recording()
        assert manual_after == result

        probe = _ffprobe_info(result.raw_file_path)
        assert probe["streams"][0]["codec_name"] == "h264"
        assert probe["streams"][0]["has_b_frames"] == 0

        keyframes = _ffprobe_keyframes(result.raw_file_path)
        assert len(keyframes) >= 2
    finally:
        engine.shutdown()
        assert engine.state == CaptureState.COLD


def test_manual_stop_cancels_autostop_without_double_callback(tmp_path: Path) -> None:
    clear_encoder_cache()
    auto_called = threading.Event()

    cfg = CaptureConfig(
        temp_dir=tmp_path,
        temp_filename="temp_raw_manual.mkv",
        fps=30,
        prefer_hardware_encoder=True,
        keyframe_interval_sec=0.5,
        auto_stop_seconds=5.0,
    )
    engine = CaptureEngine(
        cfg,
        on_auto_stop=lambda _res: auto_called.set(),
        grabber_factory=lambda _c: _SyntheticGrabber(320, 240),
    )

    try:
        engine.warm_up()
        engine.start_recording()
        time.sleep(0.6)
        res = engine.stop_recording()
        assert res.is_valid
        assert not auto_called.is_set()
    finally:
        engine.shutdown()


def test_build_encoder_args_uses_transcoding_for_amf() -> None:
    amf_args = build_encoder_args("h264_amf", gop=30)
    assert "-usage" in amf_args
    assert amf_args[amf_args.index("-usage") + 1] == "transcoding"
    assert "-bf" in amf_args
    assert amf_args[amf_args.index("-bf") + 1] == "0"
    assert "-g" in amf_args
    assert amf_args[amf_args.index("-g") + 1] == "30"


def test_multi_cycle_and_audio_missing_device_degradation(tmp_path: Path) -> None:
    clear_encoder_cache()
    cfg = CaptureConfig(
        temp_dir=tmp_path,
        temp_filename="temp_raw_cycle.mkv",
        fps=30,
        capture_audio=True,
        audio_device="NonExistent Audio Device 99999",
        prefer_hardware_encoder=True,
        keyframe_interval_sec=0.5,
    )
    engine = CaptureEngine(cfg, grabber_factory=lambda _c: _SyntheticGrabber(320, 240))
    try:
        for _ in range(2):
            engine.warm_up()
            assert engine.state == CaptureState.STANDBY
            assert engine.has_audio is False
            engine.start_recording()
            time.sleep(0.7)
            res = engine.stop_recording()
            assert res.is_valid
            assert res.has_audio is False
            assert res.start_latency_ms < 250.0
            assert engine.state == CaptureState.COLD
    finally:
        engine.shutdown()


def test_shutdown_during_in_flight_warmup_aborts_cleanly_to_cold(tmp_path: Path) -> None:
    clear_encoder_cache()
    grabber_entered = threading.Event()
    release_grabber_init = threading.Event()
    created_grabbers: list[_SyntheticGrabber] = []
    popen_calls: list[list[str]] = []

    def slow_grabber_factory(_cfg: CaptureConfig) -> _SyntheticGrabber:
        grabber_entered.set()
        release_grabber_init.wait(timeout=5.0)
        g = _SyntheticGrabber(320, 240)
        created_grabbers.append(g)
        return g

    real_popen = subprocess.Popen

    def spy_popen(cmd, *args, **kwargs):
        popen_calls.append(list(cmd))
        return real_popen(cmd, *args, **kwargs)

    cfg = CaptureConfig(temp_dir=tmp_path, fps=30, prefer_hardware_encoder=True)
    engine = CaptureEngine(cfg, grabber_factory=slow_grabber_factory)
    warm_exc: list[BaseException] = []

    def _run_warm() -> None:
        try:
            engine.warm_up()
        except BaseException as exc:
            warm_exc.append(exc)

    with patch("quickclip.capture.engine.subprocess.Popen", side_effect=spy_popen):
        t_warm = threading.Thread(target=_run_warm, daemon=True)
        t_warm.start()
        assert grabber_entered.wait(timeout=5.0)

        shutdown_done = threading.Event()

        def _run_shutdown() -> None:
            engine.shutdown()
            shutdown_done.set()

        t_shut = threading.Thread(target=_run_shutdown, daemon=True)
        t_shut.start()

        # shutdown() must wait for in-flight warm_up() cleanup rather than returning early.
        time.sleep(0.05)
        assert not shutdown_done.is_set()

        release_grabber_init.set()
        assert shutdown_done.wait(timeout=5.0)
        t_warm.join(timeout=2.0)
        t_shut.join(timeout=2.0)

    assert len(warm_exc) == 1
    assert engine.state == CaptureState.COLD
    assert [c for c in popen_calls if "pipe:0" in c] == []  # Aborted before spawning standby FFmpeg.
    assert len(created_grabbers) == 1
    assert created_grabbers[0].closed is True
    assert engine._proc is None
    assert not engine.output_path.exists()


def test_shutdown_shortly_after_real_warmup_start_leaves_cold(tmp_path: Path) -> None:
    clear_encoder_cache()
    cfg = CaptureConfig(temp_dir=tmp_path, fps=30, prefer_hardware_encoder=True)
    engine = CaptureEngine(cfg, grabber_factory=lambda _c: _SyntheticGrabber(320, 240))
    errors: list[BaseException] = []

    def _warm() -> None:
        try:
            engine.warm_up()
        except BaseException as exc:
            errors.append(exc)

    t = threading.Thread(target=_warm, daemon=True)
    t.start()
    time.sleep(0.01)
    engine.shutdown()
    t.join(timeout=5.0)

    assert engine.state == CaptureState.COLD
    assert engine._proc is None
    assert not engine.output_path.exists()


def test_concurrent_start_recording_vs_shutdown_and_stop_no_orphans(tmp_path: Path) -> None:
    clear_encoder_cache()
    auto_stopped = threading.Event()
    cfg = CaptureConfig(
        temp_dir=tmp_path,
        temp_filename="temp_race.mkv",
        fps=30,
        prefer_hardware_encoder=True,
        auto_stop_seconds=0.25,
    )

    for iteration in range(4):
        auto_stopped.clear()
        engine = CaptureEngine(
            cfg,
            on_auto_stop=lambda _res: auto_stopped.set(),
            grabber_factory=lambda _c: _SyntheticGrabber(320, 240),
        )
        engine.warm_up()
        assert engine.state == CaptureState.STANDBY

        barrier = threading.Barrier(2)
        start_errors: list[BaseException] = []

        def _do_start() -> None:
            barrier.wait(timeout=2.0)
            try:
                engine.start_recording()
            except RuntimeError as exc:
                start_errors.append(exc)

        def _do_stop_or_shutdown() -> None:
            barrier.wait(timeout=2.0)
            if iteration % 2 == 0:
                engine.shutdown()
            else:
                engine.stop_recording()

        t1 = threading.Thread(target=_do_start, daemon=True)
        t2 = threading.Thread(target=_do_stop_or_shutdown, daemon=True)
        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)

        engine.shutdown()
        # Give any canceled timer window > auto_stop_seconds to prove no orphan fires.
        time.sleep(0.32)

        assert engine.state == CaptureState.COLD
        assert engine._capture_thread is None
        assert engine._auto_stop_timer is None
        assert engine._proc is None
        assert not auto_stopped.is_set()


def test_bundled_binaries_meipass_bin_priority_with_empty_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clear_encoder_cache()
    meipass_dir = tmp_path / "meipass"
    bin_dir = meipass_dir / "bin"
    bin_dir.mkdir(parents=True)
    exe_suffix = ".exe" if sys.platform == "win32" else ""
    bundled_ffmpeg = bin_dir / f"ffmpeg{exe_suffix}"
    bundled_ffprobe = bin_dir / f"ffprobe{exe_suffix}"
    bundled_ffmpeg.write_bytes(b"stub")
    bundled_ffprobe.write_bytes(b"stub")

    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass_dir), raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)

    run_cmds: list[list[str]] = []
    popen_cmds: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs):
        run_cmds.append(list(cmd))
        if "-list_devices" in cmd:
            return subprocess.CompletedProcess(
                cmd,
                1,
                "",
                '[in#0 @ 0] "Bundled Mic" (audio)\n',
            )
        if "-show_entries" in cmd:
            return subprocess.CompletedProcess(
                cmd,
                0,
                json.dumps(
                    {
                        "format": {"duration": "1.25"},
                        "streams": [{"codec_type": "video", "codec_name": "h264", "width": 320, "height": 240}],
                    }
                ),
                "",
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    fake_proc = MagicMock()
    fake_proc.poll.return_value = None
    fake_proc.stdin = MagicMock()
    fake_proc.stderr = None

    def fake_popen(cmd: list[str], **_kwargs):
        popen_cmds.append(list(cmd))
        return fake_proc

    cfg = CaptureConfig(
        temp_dir=tmp_path / "cap",
        fps=30,
        capture_audio=True,
        audio_device="Bundled Mic",
        prefer_hardware_encoder=True,
    )
    engine = CaptureEngine(cfg, grabber_factory=lambda _c: _SyntheticGrabber(320, 240))
    assert engine.resolved_ffmpeg_path == str(bundled_ffmpeg)
    assert engine.resolved_ffprobe_path == str(bundled_ffprobe)

    with (
        patch("quickclip.capture.engine.subprocess.run", side_effect=fake_run),
        patch("quickclip.capture.audio.subprocess.run", side_effect=fake_run),
        patch("quickclip.capture.engine.subprocess.Popen", side_effect=fake_popen),
    ):
        engine.warm_up()
        assert engine.state == CaptureState.STANDBY
        assert engine.has_audio is True

        # Trigger ffprobe invocation through _probe_output_metadata.
        probe_out = engine._probe_output_metadata(tmp_path / "dummy.mkv")
        assert probe_out["duration_sec"] == pytest.approx(1.25)

        # Direct audio helper checks under empty PATH.
        devs = list_audio_devices()
        assert [d.name for d in devs] == ["Bundled Mic"]
        assert probe_audio_device("Bundled Mic") is True
        assert resolve_audio_input(True, "Bundled Mic") == "audio=Bundled Mic"

        engine.shutdown()

    assert popen_cmds and popen_cmds[0][0] == str(bundled_ffmpeg)
    ffmpeg_runs = [c for c in run_cmds if c[0] == str(bundled_ffmpeg)]
    ffprobe_runs = [c for c in run_cmds if c[0] == str(bundled_ffprobe)]
    assert len(ffmpeg_runs) >= 4  # encoder probe + audio list/probe calls
    assert len(ffprobe_runs) == 1
    clear_encoder_cache()


def test_bundled_binaries_frozen_exe_dir_priority_and_cache_keying(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clear_encoder_cache()
    frozen_dir = tmp_path / "dist_app"
    bin_dir = frozen_dir / "bin"
    bin_dir.mkdir(parents=True)
    exe_suffix = ".exe" if sys.platform == "win32" else ""
    frozen_exe = frozen_dir / f"QuickClip{exe_suffix}"
    frozen_exe.write_bytes(b"app")
    frozen_ffmpeg = bin_dir / f"ffmpeg{exe_suffix}"
    frozen_ffprobe = bin_dir / f"ffprobe{exe_suffix}"
    frozen_ffmpeg.write_bytes(b"stub")
    frozen_ffprobe.write_bytes(b"stub")

    monkeypatch.setenv("PATH", "")
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(frozen_exe))

    run_cmds: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs):
        run_cmds.append(list(cmd))
        # Simulate frozen ffmpeg supporting h264_amf, custom ffmpeg only libx264.
        if cmd[0] == "custom_ffmpeg_bin" and "h264_amf" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "no amf")
        if cmd[0] == "custom_ffmpeg_bin" and ("h264_nvenc" in cmd or "h264_qsv" in cmd):
            return subprocess.CompletedProcess(cmd, 1, "", "no hw")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    fake_proc = MagicMock()
    fake_proc.poll.return_value = None
    fake_proc.stdin = MagicMock()
    fake_proc.stderr = None

    cfg = CaptureConfig(temp_dir=tmp_path / "cap", fps=30, prefer_hardware_encoder=True)
    frozen_engine = CaptureEngine(cfg, grabber_factory=lambda _c: _SyntheticGrabber(320, 240))
    custom_engine = CaptureEngine(
        cfg,
        ffmpeg_path="custom_ffmpeg_bin",
        ffprobe_path="custom_ffprobe_bin",
        grabber_factory=lambda _c: _SyntheticGrabber(320, 240),
    )

    assert frozen_engine.resolved_ffmpeg_path == str(frozen_ffmpeg)
    assert frozen_engine.resolved_ffprobe_path == str(frozen_ffprobe)
    assert custom_engine.resolved_ffmpeg_path == "custom_ffmpeg_bin"
    assert custom_engine.resolved_ffprobe_path == "custom_ffprobe_bin"

    with (
        patch("quickclip.capture.engine.subprocess.run", side_effect=fake_run),
        patch("quickclip.capture.engine.subprocess.Popen", return_value=fake_proc) as popen_mock,
    ):
        frozen_engine.warm_up()
        assert frozen_engine.encoder_used == "h264_amf"
        assert popen_mock.call_args.args[0][0] == str(frozen_ffmpeg)

        # Separate resolved ffmpeg path must NOT collide in _ENCODER_CACHE.
        custom_engine.warm_up()
        assert custom_engine.encoder_used == "libx264"
        assert popen_mock.call_args.args[0][0] == "custom_ffmpeg_bin"

        frozen_engine.shutdown()
        custom_engine.shutdown()

    assert any(c[0] == str(frozen_ffmpeg) for c in run_cmds)
    assert any(c[0] == "custom_ffmpeg_bin" for c in run_cmds)
    clear_encoder_cache()


def test_list_monitors_and_dxcam_rect_matching() -> None:
    monitors = list_monitors()
    assert len(monitors) >= 1
    assert all(isinstance(m, MonitorInfo) and m.index >= 1 and m.width > 0 and m.height > 0 for m in monitors)
    rect = resolve_monitor_rect(monitors[0].index)
    assert rect == (monitors[0].left, monitors[0].top, monitors[0].width, monitors[0].height)

    class _Coords:
        def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
            self.left = left
            self.top = top
            self.right = right
            self.bottom = bottom

    class _Desc:
        def __init__(self, coords: _Coords) -> None:
            self.DesktopCoordinates = coords

    class _Out:
        def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
            self.desc = _Desc(_Coords(left, top, right, bottom))

    class _Factory:
        outputs = [
            [_Out(-1920, 0, 0, 1080), _Out(0, 0, 3840, 2160)],
        ]

    from types import SimpleNamespace

    fake_dxcam = SimpleNamespace(__factory=_Factory())

    assert _DXCamGrabber._match_dxcam_output(
        fake_dxcam, monitor_index=2, target_rect=(-1920, 0, 1920, 1080)
    ) == (0, 0)
    assert _DXCamGrabber._match_dxcam_output(
        fake_dxcam, monitor_index=1, target_rect=(0, 0, 3840, 2160)
    ) == (0, 1)


def test_capture_engine_set_monitor_rewarms_and_records_new_dimensions(tmp_path: Path) -> None:
    clear_encoder_cache()
    created: list[int] = []

    def _grabber_for_monitor(cfg: CaptureConfig) -> _SyntheticGrabber:
        created.append(cfg.monitor_index)
        if cfg.monitor_index == 2:
            return _SyntheticGrabber(width=320, height=240)
        return _SyntheticGrabber(width=640, height=360)

    cfg = CaptureConfig(
        temp_dir=tmp_path,
        temp_filename="temp_raw_mon.mkv",
        fps=30,
        monitor_index=1,
        prefer_hardware_encoder=False,
    )
    engine = CaptureEngine(cfg, grabber_factory=_grabber_for_monitor)

    try:
        engine.warm_up()
        assert engine.state == CaptureState.STANDBY
        assert engine.capture_size == (640, 360)
        assert created == [1]

        engine.set_monitor(2, rewarm=True)
        assert engine.state == CaptureState.STANDBY
        assert cfg.monitor_index == 2
        assert engine.capture_size == (320, 240)
        assert created == [1, 2]

        engine.start_recording()
        with pytest.raises(RuntimeError, match="Cannot change monitor"):
            engine.set_monitor(1)
        time.sleep(0.8)
        result = engine.stop_recording()
        assert result.is_valid
        assert result.width == 320
        assert result.height == 240
    finally:
        engine.shutdown()
        clear_encoder_cache()


def test_capture_engine_set_capture_region_and_small_region_fallback(tmp_path: Path) -> None:
    clear_encoder_cache()

    def _grabber_for_cfg(cfg: CaptureConfig) -> _SyntheticGrabber:
        if cfg.capture_region is not None:
            return _SyntheticGrabber(
                width=cfg.capture_region.width,
                height=cfg.capture_region.height,
            )
        return _SyntheticGrabber(width=640, height=360)

    cfg = CaptureConfig(
        temp_dir=tmp_path,
        temp_filename="temp_raw_region.mkv",
        fps=30,
        monitor_index=1,
        prefer_hardware_encoder=True,
    )
    engine = CaptureEngine(cfg, grabber_factory=_grabber_for_cfg)

    try:
        engine.warm_up()
        assert engine.capture_size == (640, 360)

        # Standard region size (>= 128x128) captures at exact region dimensions.
        region = CaptureRegion(left=40, top=20, width=320, height=180, monitor_index=1)
        engine.set_capture_region(region, rewarm=True)
        assert engine.state == CaptureState.STANDBY
        assert engine.capture_size == (320, 180)
        engine.start_recording()
        with pytest.raises(RuntimeError, match="Cannot change capture region"):
            engine.set_capture_region(None)
        time.sleep(0.7)
        res = engine.stop_recording()
        assert res.is_valid
        assert res.width == 320
        assert res.height == 180

        # Tiny region (< 128x128) automatically falls back from hardware encoder to libx264.
        tiny = CaptureRegion(left=10, top=10, width=64, height=64, monitor_index=1)
        engine.set_capture_region(tiny, rewarm=True)
        assert engine.state == CaptureState.STANDBY
        assert engine.capture_size == (64, 64)
        assert engine.encoder_used == "libx264"
        engine.start_recording()
        time.sleep(0.6)
        tiny_res = engine.stop_recording()
        assert tiny_res.is_valid
        assert tiny_res.width == 64
        assert tiny_res.height == 64
    finally:
        engine.shutdown()
        clear_encoder_cache()



