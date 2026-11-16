"""CLI entrypoint for QuickClip."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import sys
import tempfile

from quickclip.app import QuickClipController, run_app
from quickclip.contracts import AppConfig, CaptureConfig


def configure_logging(*, verbose: bool = False, temp_dir: Path | None = None) -> None:
    """Configure root logging safely for both console and ``--windowed`` runs."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    if sys.stderr is not None:
        logging.basicConfig(level=level, format=fmt)
        return

    log_dir = Path(temp_dir) if temp_dir is not None else Path(tempfile.gettempdir()) / "quickclip"
    handler: logging.Handler
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_dir / "quickclip.log", encoding="utf-8")
    except OSError:
        handler = logging.NullHandler()

    logging.basicConfig(level=level, format=fmt, handlers=[handler], force=True)


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser for QuickClip."""
    parser = argparse.ArgumentParser(
        prog="quickclip",
        description="QuickClip — instant Warm Standby screen capture, preview trimmer, and lossless exporter.",
    )
    parser.add_argument(
        "--hotkey",
        default="F9",
        help="Global hotkey to start/stop recording (default: F9).",
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=60,
        help="Capture frame rate in FPS (default: 60).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Destination folder for exported MP4 clips (default: ~/Videos/QuickClip).",
    )
    parser.add_argument(
        "--temp-dir",
        type=Path,
        default=None,
        help="Temporary directory for raw MKV captures (default: system temp/quickclip).",
    )
    parser.add_argument(
        "--auto-stop",
        type=float,
        default=None,
        help="Optional automatic stop duration in seconds (default: manual toggle).",
    )
    parser.add_argument(
        "--cpu-only",
        action="store_true",
        help="Disable hardware encoder probing and force libx264.",
    )
    parser.add_argument(
        "--audio",
        action="store_true",
        help="Enable system/microphone audio capture if an audio device is available.",
    )
    parser.add_argument(
        "--audio-device",
        default=None,
        help="Optional DirectShow audio device name when --audio is enabled.",
    )
    parser.add_argument(
        "--monitor",
        type=int,
        default=1,
        help="1-based display index to capture (default: 1).",
    )
    parser.add_argument(
        "--no-tray",
        action="store_true",
        help="Disable the system tray icon.",
    )
    parser.add_argument(
        "--headless-check",
        action="store_true",
        help="Verify Qt, controller, and capture/export wiring without starting the interactive loop.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser


def build_app_config(args: argparse.Namespace) -> AppConfig:
    """Construct an :class:`AppConfig` from parsed CLI arguments."""
    defaults = AppConfig()
    capture_defaults = CaptureConfig()
    capture_cfg = CaptureConfig(
        temp_dir=args.temp_dir if args.temp_dir is not None else capture_defaults.temp_dir,
        hotkey=args.hotkey,
        fps=args.fps,
        capture_audio=bool(args.audio),
        audio_device=args.audio_device,
        prefer_hardware_encoder=not bool(args.cpu_only),
        monitor_index=args.monitor,
        auto_stop_seconds=args.auto_stop,
    )
    return AppConfig(
        capture=capture_cfg,
        output_dir=args.output_dir if args.output_dir is not None else defaults.output_dir,
    )


def run_headless_check(config: AppConfig) -> int:
    """Instantiate Qt and controller components and verify clean shutdown."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    controller = QuickClipController(config=config, enable_tray=False, quit_on_exit=False)
    try:
        trimmer = controller._ensure_trimmer_window()
        assert trimmer is not None
    finally:
        controller.shutdown()

    msg = (
        f"QuickClip headless check OK: hotkey={config.capture.hotkey}, "
        f"fps={config.capture.fps}, output_dir={config.output_dir}"
    )
    logging.getLogger(__name__).info(msg)
    if sys.stdout is not None:
        print(msg)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and start QuickClip."""
    parser = build_parser()
    args = parser.parse_args(argv)

    config = build_app_config(args)
    configure_logging(verbose=bool(args.verbose), temp_dir=config.capture.temp_dir)

    if args.headless_check:
        return run_headless_check(config)

    return run_app(config=config, enable_tray=not bool(args.no_tray))


if __name__ == "__main__":
    raise SystemExit(main())
