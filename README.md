# QuickClip

Instant **Warm Standby** screen recorder, minimalist preview trimmer, and lossless FFmpeg clip exporter for Windows.

## Architecture Overview

QuickClip splits capture, UI preview/trimming, lossless export, and orchestration behind clean inter-module contracts in [`src/quickclip/contracts.py`](src/quickclip/contracts.py):

```text
+-------------------------------------------------------------------+
| QuickClipController (src/quickclip/app.py)                        |
|  - Marshals background signals -> Qt main thread (QueuedConnection)|
|  - Serializes heavy capture/export/cleanup on ThreadPoolExecutor  |
+----+-----------------------+-----------------------+--------------+
     |                       |                       |
     v                       v                       v
+-----------------+  +------------------+  +------------------------+
| capture/        |  | ui/              |  | exporter/              |
| - CaptureEngine |  | - StandbyWindow  |  | - ExporterService      |
| - HotkeyListener|  | - TrimmerWindow  |  |   ffmpeg -c copy       |
| - audio helpers |  | - RangeSlider    |  |   -movflags +faststart |
+-----------------+  +------------------+  +------------------------+
```

### Key Design Mechanics

1. **Warm Standby (`CaptureEngine`):**
   - `warm_up()` probes and caches the fastest available H.264 encoder once (`h264_amf` -> `h264_nvenc` -> `h264_qsv` -> `libx264`), initializes the screen grabber (`dxcam` DXGI Desktop Duplication primary with automatic `mss` fallback), and pre-spawns a standby `ffmpeg` subprocess reading `rawvideo` (`bgra`) over `stdin` into `temp_raw.mkv`.
   - `start_recording()` immediately begins feeding grabbed frames into the already-open pipe (no process spawn delay on hotkey press), writing fixed-interval keyframes (`keyframe_interval_sec=0.5`, `-bf 0`) so downstream stream-copy trimming seeks accurately.
   - `stop_recording()` closes `stdin`, waits for `ffmpeg` to finalize `temp_raw.mkv`, probes duration/metadata via `ffprobe`, and returns a `CaptureResult`.

2. **Minimalist Preview & Trimming (`TrimmerWindow` & `RangeSlider`):**
   - Opens automatically when a valid `CaptureResult` arrives, displaying a `QMediaPlayer` preview and dual-handle timeline (`RangeSlider`) with Spacebar play/pause and optional loop playback.
   - Calls `release_media()` (`stop()` + `setSource(QUrl())`) before emitting `exportRequested` or `discardRequested` so Windows releases file locks on `temp_raw.mkv`.

3. **Lossless Stream-Copy Export (`ExporterService`):**
   - Trim export places both `-ss` and `-to` before `-i` with `-c copy -movflags +faststart` (`ffmpeg -y -ss <start> -to <end> -i <source> -c copy -movflags +faststart <output>`) for instant zero-re-encode clipping.
   - Direct save remuxes directly via `-c copy -movflags +faststart`.
   - Safely removes `temp_raw.mkv` with Windows lock retry only after output validation succeeds, then `QuickClipController` re-arms `CaptureEngine` back to `STANDBY`.

4. **Inter-Module Contracts (`src/quickclip/contracts.py`):**
   - `CaptureState(str, Enum)`: `COLD`, `WARMING`, `STANDBY`, `RECORDING`, `STOPPING`, `ERROR`
   - `CaptureConfig`, `CaptureResult` (with `.is_valid`), `TrimRequest` (with `.validate()`), `TrimResult`, `AppConfig`
   - Runtime-checkable protocols: `ICaptureEngine`, `IExporterService`, `IHotkeyListener`

## Requirements & Setup

- **OS:** Windows 10/11
- **Python:** 3.12+
- **External tools on `PATH`:** `ffmpeg` and `ffprobe` (verified with FFmpeg 9.0.2)

Create and populate the virtual environment (already configured in `.venv/`):

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev,dxgi]"
```

## Running QuickClip

```powershell
.\.venv\Scripts\python.exe main.py
```

Useful CLI flags:

```powershell
.\.venv\Scripts\python.exe main.py --help
.\.venv\Scripts\python.exe main.py --hotkey F9 --fps 60 --output-dir "$env:USERPROFILE\Videos\QuickClip"
.\.venv\Scripts\python.exe main.py --auto-stop 5.0
.\.venv\Scripts\python.exe main.py --cpu-only --audio
.\.venv\Scripts\python.exe main.py --headless-check
```

## Building Standalone Windows `.exe` & Shortcuts

Build the `--onedir --windowed` standalone bundle (bundling `ffmpeg.exe`, `ffprobe.exe`, `assets/quickclip.ico`, `assets/quickclip.png`, PySide6 QtMultimedia plugins, `dxcam`, `mss`, and `keyboard`) into `dist\QuickClip\QuickClip.exe`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_exe.ps1
```

Create Desktop and Start Menu shortcuts pointing to `dist\QuickClip\QuickClip.exe`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/create_shortcuts.ps1
```

> **Note:** `QuickClip.exe` runs single-instance (`QLocalServer`) — launching a second time restores and focuses the running QuickClip window. Toggle **"Windows ile Başlat"** on the standby bar to register/unregister `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.

## Running Tests

Run the full test suite (contracts, capture, UI, exporter, and end-to-end integration):

```powershell
.\.venv\Scripts\python.exe -m pytest -v
```

Run in headless/offscreen Qt mode:

```powershell
$env:QT_QPA_PLATFORM="offscreen"
.\.venv\Scripts\python.exe -m pytest -v
```
