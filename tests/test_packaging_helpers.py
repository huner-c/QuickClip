"""Unit tests for binary/resource path resolution, Windows startup helper, and icon assets."""

from __future__ import annotations

from pathlib import Path
import sys

from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon, QImage
import pytest

import quickclip.paths as paths_mod
import quickclip.startup as startup_mod
from quickclip.paths import (
    resolve_binary_path,
    resolve_ffmpeg_path,
    resolve_ffprobe_path,
    resolve_resource_path,
)
from quickclip.startup import (
    RUN_KEY_PATH,
    default_startup_command,
    is_run_at_startup_enabled,
    set_run_at_startup,
)
from scripts.generate_icon import ICON_SIZES, generate_assets


class _FakeRegKey:
    def __init__(self, store: dict[str, dict[str, tuple[int, str]]], subkey: str, access: int) -> None:
        self._store = store
        self._subkey = subkey
        self._access = access

    def __enter__(self) -> _FakeRegKey:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeWinReg:
    HKEY_CURRENT_USER = object()
    KEY_READ = 0x20019
    KEY_SET_VALUE = 0x0002
    REG_SZ = 1

    def __init__(self, *, create_run_key: bool = True) -> None:
        self.keys: dict[str, dict[str, tuple[int, str]]] = {}
        if create_run_key:
            self.keys[RUN_KEY_PATH] = {}
        self.raise_on_open: OSError | None = None

    def OpenKey(self, root, subkey: str, reserved: int, access: int) -> _FakeRegKey:
        if self.raise_on_open is not None:
            raise self.raise_on_open
        if subkey not in self.keys:
            raise FileNotFoundError(subkey)
        return _FakeRegKey(self.keys, subkey, access)

    def CreateKeyEx(self, root, subkey: str, reserved: int, access: int) -> _FakeRegKey:
        if self.raise_on_open is not None:
            raise self.raise_on_open
        self.keys.setdefault(subkey, {})
        return _FakeRegKey(self.keys, subkey, access)

    def QueryValueEx(self, key: _FakeRegKey, name: str) -> tuple[str, int]:
        bucket = key._store.get(key._subkey, {})
        if name not in bucket:
            raise FileNotFoundError(name)
        reg_type, val = bucket[name]
        return val, reg_type

    def SetValueEx(self, key: _FakeRegKey, name: str, reserved: int, reg_type: int, value: str) -> None:
        key._store.setdefault(key._subkey, {})[name] = (reg_type, value)

    def DeleteValue(self, key: _FakeRegKey, name: str) -> None:
        bucket = key._store.get(key._subkey, {})
        if name not in bucket:
            raise FileNotFoundError(name)
        del bucket[name]


def _make_fake_repo_paths(monkeypatch: pytest.MonkeyPatch, fake_repo: Path) -> None:
    fake_paths_file = fake_repo / "src" / "quickclip" / "paths.py"
    fake_paths_file.parent.mkdir(parents=True, exist_ok=True)
    fake_paths_file.write_text("# stub", encoding="utf-8")
    monkeypatch.setattr(paths_mod, "__file__", str(fake_paths_file))


def test_resolve_binary_priority_and_preservation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_repo = tmp_path / "repo"
    _make_fake_repo_paths(monkeypatch, fake_repo)

    meipass = tmp_path / "meipass"
    exe_dir = tmp_path / "dist" / "QuickClip"
    exe_path = exe_dir / "QuickClip.exe"
    exe_path.parent.mkdir(parents=True, exist_ok=True)
    exe_path.write_bytes(b"exe")

    # Populate all candidate tiers for ffmpeg.exe to verify exact precedence.
    meipass_bin = meipass / "bin" / "ffmpeg.exe"
    meipass_root = meipass / "ffmpeg.exe"
    frozen_bin = exe_dir / "bin" / "ffmpeg.exe"
    frozen_root = exe_dir / "ffmpeg.exe"
    frozen_internal = exe_dir / "_internal" / "bin" / "ffmpeg.exe"
    repo_bin = fake_repo / "bin" / "ffmpeg.exe"
    for p in (meipass_bin, meipass_root, frozen_bin, frozen_root, frozen_internal, repo_bin):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"bin")

    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_path))
    monkeypatch.setenv("PATH", "")

    assert resolve_ffmpeg_path("ffmpeg") == str(meipass_bin)
    meipass_bin.unlink()
    assert resolve_binary_path("FFMPEG.EXE") == str(meipass_root)
    meipass_root.unlink()
    assert resolve_binary_path("ffmpeg") == str(frozen_bin)
    frozen_bin.unlink()
    assert resolve_binary_path("ffmpeg") == str(frozen_root)
    frozen_root.unlink()
    assert resolve_binary_path("ffmpeg") == str(frozen_internal)
    frozen_internal.unlink()
    assert resolve_binary_path("ffmpeg") == str(repo_bin)
    repo_bin.unlink()

    # Empty PATH fallback returns input unchanged.
    monkeypatch.setattr(paths_mod.shutil, "which", lambda _name: None)
    assert resolve_binary_path("ffmpeg") == "ffmpeg"
    assert resolve_ffprobe_path("ffprobe") == "ffprobe"

    # Non-empty PATH fallback resolves via shutil.which.
    which_target = str(tmp_path / "sys_bin" / "ffprobe.exe")
    monkeypatch.setattr(paths_mod.shutil, "which", lambda name: which_target if "ffprobe" in name else None)
    assert resolve_ffprobe_path() == which_target

    # Custom names and explicit paths are left untouched even when bundled binaries exist.
    meipass_bin.write_bytes(b"bin")
    assert resolve_binary_path("custom_ffmpeg") == "custom_ffmpeg"
    assert resolve_binary_path(r"C:\Tools\ffmpeg.exe") == r"C:\Tools\ffmpeg.exe"


def test_resolve_resource_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_repo = tmp_path / "repo"
    _make_fake_repo_paths(monkeypatch, fake_repo)
    meipass = tmp_path / "meipass"
    exe_dir = tmp_path / "dist"
    exe_path = exe_dir / "QuickClip.exe"
    exe_path.parent.mkdir(parents=True, exist_ok=True)

    repo_asset = fake_repo / "assets" / "quickclip.ico"
    internal_asset = exe_dir / "_internal" / "assets" / "quickclip.ico"
    meipass_asset = meipass / "assets" / "quickclip.ico"
    for p in (repo_asset, internal_asset, meipass_asset):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"ico")

    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_path))

    assert resolve_resource_path("assets/quickclip.ico") == meipass_asset
    meipass_asset.unlink()
    assert resolve_resource_path("assets/quickclip.ico") == internal_asset
    internal_asset.unlink()
    assert resolve_resource_path("assets/quickclip.ico") == repo_asset

    abs_path = tmp_path / "abs.png"
    assert resolve_resource_path(abs_path) == abs_path


def test_startup_helpers_mocked_winreg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_reg = _FakeWinReg(create_run_key=False)
    monkeypatch.setattr(startup_mod, "winreg", fake_reg)

    # Frozen command line uses sys.executable directly.
    frozen_exe = tmp_path / "QuickClip.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(frozen_exe))
    assert default_startup_command() == f'"{frozen_exe.resolve()}"'

    # Non-frozen command prefers pythonw.exe next to python.exe when present.
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    py_dir = tmp_path / "py"
    py_dir.mkdir()
    py_exe = py_dir / "python.exe"
    pyw_exe = py_dir / "pythonw.exe"
    py_exe.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(py_exe))
    assert default_startup_command().startswith(f'"{py_exe.resolve()}" ')
    pyw_exe.write_bytes(b"")
    assert default_startup_command().startswith(f'"{pyw_exe.resolve()}" ')

    assert is_run_at_startup_enabled("QuickClip") is False
    # Disabling when key doesn't exist is safe and idempotent.
    assert set_run_at_startup(False, app_name="QuickClip") is True

    with pytest.raises(ValueError, match="non-empty"):
        set_run_at_startup(True, command="   ")

    assert set_run_at_startup(True, app_name="QuickClip") is True
    assert is_run_at_startup_enabled("QuickClip") is True
    reg_type, stored_cmd = fake_reg.keys[RUN_KEY_PATH]["QuickClip"]
    assert reg_type == fake_reg.REG_SZ
    assert "main.py" in stored_cmd

    assert set_run_at_startup(False, app_name="QuickClip") is True
    assert is_run_at_startup_enabled("QuickClip") is False
    assert set_run_at_startup(False, app_name="QuickClip") is True

    fake_reg.raise_on_open = PermissionError("access denied")
    assert is_run_at_startup_enabled("QuickClip") is False
    assert set_run_at_startup(True, command='"C:\\App.exe"') is False

    monkeypatch.setattr(startup_mod, "winreg", None)
    assert is_run_at_startup_enabled("QuickClip") is False
    assert set_run_at_startup(True, command='"C:\\App.exe"') is False


def test_icon_assets_multires_and_generator(qtbot, tmp_path: Path) -> None:
    ico_path = resolve_resource_path("assets/quickclip.ico")
    png_path = resolve_resource_path("assets/quickclip.png")
    assert ico_path.is_file() and ico_path.stat().st_size > 0
    assert png_path.is_file() and png_path.stat().st_size > 0

    png_img = QImage(str(png_path))
    assert not png_img.isNull()
    assert png_img.width() == 256 and png_img.height() == 256

    icon = QIcon(str(ico_path))
    assert not icon.isNull()
    available = {(s.width(), s.height()) for s in icon.availableSizes()}
    assert available == {(sz, sz) for sz in ICON_SIZES}

    gen_ico, gen_png = generate_assets(tmp_path / "assets")
    assert gen_ico.is_file() and gen_png.is_file()
    gen_icon = QIcon(str(gen_ico))
    assert {(s.width(), s.height()) for s in gen_icon.availableSizes()} == {
        (sz, sz) for sz in ICON_SIZES
    }
    assert QSize(256, 256) in gen_icon.availableSizes()
