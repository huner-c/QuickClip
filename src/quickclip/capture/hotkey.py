"""Global hotkey registration using Win32 RegisterHotKey with optional keyboard fallback."""

from __future__ import annotations

from collections.abc import Callable
import ctypes
from ctypes import wintypes
import logging
import sys
import threading
from typing import Any

logger = logging.getLogger(__name__)

# Win32 constants for RegisterHotKey / GetMessageW loop
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_QUIT = 0x0012
WM_HOTKEY = 0x0312
PM_NOREMOVE = 0x0000

_MODIFIER_MAP: dict[str, int] = {
    "alt": MOD_ALT,
    "lalt": MOD_ALT,
    "ralt": MOD_ALT,
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "lctrl": MOD_CONTROL,
    "rctrl": MOD_CONTROL,
    "shift": MOD_SHIFT,
    "lshift": MOD_SHIFT,
    "rshift": MOD_SHIFT,
    "win": MOD_WIN,
    "windows": MOD_WIN,
    "super": MOD_WIN,
    "meta": MOD_WIN,
    "lwin": MOD_WIN,
    "rwin": MOD_WIN,
}

_SPECIAL_KEY_MAP: dict[str, int] = {
    "backspace": 0x08,
    "back": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "pause": 0x13,
    "capslock": 0x14,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "spacebar": 0x20,
    "pageup": 0x21,
    "pgup": 0x21,
    "prior": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "next": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "printscreen": 0x2C,
    "prtsc": 0x2C,
    "snapshot": 0x2C,
    "insert": 0x2D,
    "ins": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "plus": 0xBB,
    "minus": 0xBD,
    "comma": 0xBC,
    "period": 0xBE,
    "slash": 0xBF,
    "grave": 0xC0,
}
# Function keys F1..F24 -> 0x70..0x87
for _fn_idx in range(1, 25):
    _SPECIAL_KEY_MAP[f"f{_fn_idx}"] = 0x6F + _fn_idx
# Numpad digits numpad0..numpad9 -> 0x60..0x69
for _np_idx in range(10):
    _SPECIAL_KEY_MAP[f"numpad{_np_idx}"] = 0x60 + _np_idx


def parse_hotkey(hotkey: str) -> tuple[int, int]:
    """Parse a human-readable hotkey string into Win32 ``(modifiers, vk_code)``.

    Supports modifier combinations such as ``"ctrl+shift+r"``, ``"alt+f9"``,
    ``"win+shift+s"``, and single keys such as ``"F9"`` or ``"space"``.

    Args:
        hotkey: Hotkey specification string (e.g., ``"F9"``, ``"ctrl+shift+r"``).

    Returns:
        Tuple of ``(modifiers_mask, virtual_key_code)``. Note that ``MOD_NOREPEAT``
        is NOT included in ``modifiers_mask`` and is applied at registration time.

    Raises:
        ValueError: If ``hotkey`` is empty, lacks a main key, contains multiple
            non-modifier keys, or references an unrecognized key token.
    """
    if not isinstance(hotkey, str) or not hotkey.strip():
        raise ValueError("Hotkey string must not be empty.")

    tokens = [part.strip().lower() for part in hotkey.split("+")]
    if any(not token for token in tokens):
        raise ValueError(f"Invalid hotkey syntax: {hotkey!r}")

    modifiers = 0
    key_token: str | None = None

    for token in tokens:
        if token in _MODIFIER_MAP:
            modifiers |= _MODIFIER_MAP[token]
            continue
        if key_token is not None:
            raise ValueError(
                f"Hotkey {hotkey!r} specifies multiple non-modifier keys "
                f"({key_token!r} and {token!r})."
            )
        key_token = token

    if key_token is None:
        raise ValueError(f"Hotkey {hotkey!r} has no main key.")

    if key_token in _SPECIAL_KEY_MAP:
        vk_code = _SPECIAL_KEY_MAP[key_token]
    elif len(key_token) == 1 and ("a" <= key_token <= "z" or "0" <= key_token <= "9"):
        vk_code = ord(key_token.upper())
    else:
        raise ValueError(f"Unsupported key token {key_token!r} in hotkey {hotkey!r}.")

    return modifiers, vk_code


class HotkeyListener:
    """Global hotkey listener backed by Win32 ``RegisterHotKey``.

    Runs a dedicated background daemon message-loop thread with ``MOD_NOREPEAT``
    so registration requires no elevated privileges. Falls back optionally to the
    ``keyboard`` package if Win32 ``RegisterHotKey`` fails.
    """

    def __init__(
        self,
        *,
        use_keyboard_fallback: bool = True,
        hotkey_id: int = 1,
    ) -> None:
        self._use_keyboard_fallback = use_keyboard_fallback
        self._hotkey_id = int(hotkey_id)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._hotkey: str | None = None
        self._backend: str | None = None
        self._keyboard_handle: Any = None
        self._keyboard_module: Any = None

    @property
    def is_registered(self) -> bool:
        """Return True if a hotkey is currently registered."""
        with self._lock:
            return self._backend is not None

    @property
    def hotkey(self) -> str | None:
        """Return the currently registered hotkey string, or None."""
        with self._lock:
            return self._hotkey

    @property
    def backend(self) -> str | None:
        """Return the active backend name (``'win32'``, ``'keyboard'``, or None)."""
        with self._lock:
            return self._backend

    def register(self, hotkey: str, callback: Callable[[], None]) -> None:
        """Register ``hotkey`` to invoke ``callback`` when pressed.

        Replaces any previously active registration before binding the new key.
        """
        if not callable(callback):
            raise TypeError("callback must be callable.")

        modifiers, vk_code = parse_hotkey(hotkey)
        self.unregister()

        win32_error: str | None = None
        if sys.platform == "win32":
            ok, win32_error = self._register_win32(hotkey, modifiers, vk_code, callback)
            if ok:
                return
            logger.warning(
                "Win32 RegisterHotKey failed for %r (%s).",
                hotkey,
                win32_error,
            )
        else:
            win32_error = f"Unsupported platform for Win32 RegisterHotKey: {sys.platform}"

        if self._use_keyboard_fallback:
            try:
                self._register_keyboard_fallback(hotkey, callback)
                return
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to register hotkey {hotkey!r} via Win32 ({win32_error}) "
                    f"and keyboard fallback ({exc})."
                ) from exc

        raise RuntimeError(f"Failed to register hotkey {hotkey!r}: {win32_error}")

    @staticmethod
    def _post_wm_quit(user32: Any, thread_id: int) -> None:
        try:
            post_fn = user32.PostThreadMessageW
            if isinstance(post_fn, ctypes._CFuncPtr):
                post_fn.argtypes = (
                    wintypes.DWORD,
                    wintypes.UINT,
                    wintypes.WPARAM,
                    wintypes.LPARAM,
                )
                post_fn.restype = wintypes.BOOL
            post_fn(int(thread_id), WM_QUIT, 0, 0)
        except Exception:
            logger.debug("PostThreadMessageW(WM_QUIT) failed.", exc_info=True)

    def unregister(self) -> None:
        """Unregister the active hotkey binding and stop listener resources."""
        thread_to_join: threading.Thread | None = None
        thread_id: int | None = None
        kb_handle: Any = None
        kb_mod: Any = None
        backend: str | None = None

        with self._lock:
            backend = self._backend
            thread_to_join = self._thread
            thread_id = self._thread_id
            kb_handle = self._keyboard_handle
            kb_mod = self._keyboard_module

            self._backend = None
            self._hotkey = None
            self._thread = None
            self._thread_id = None
            self._keyboard_handle = None
            self._keyboard_module = None

        if backend == "win32" and thread_to_join is not None:
            if thread_id is not None:
                user32 = getattr(getattr(ctypes, "windll", None), "user32", None)
                if user32 is not None:
                    self._post_wm_quit(user32, thread_id)
            if thread_to_join is not threading.current_thread():
                thread_to_join.join(timeout=2.0)

        if backend == "keyboard" and kb_mod is not None and kb_handle is not None:
            try:
                kb_mod.remove_hotkey(kb_handle)
            except Exception:
                logger.debug("keyboard.remove_hotkey failed.", exc_info=True)

    def _register_win32(
        self,
        hotkey: str,
        modifiers: int,
        vk_code: int,
        callback: Callable[[], None],
    ) -> tuple[bool, str]:
        user32 = getattr(getattr(ctypes, "windll", None), "user32", None)
        kernel32 = getattr(getattr(ctypes, "windll", None), "kernel32", None)
        if user32 is None or kernel32 is None:
            return False, "ctypes.windll.user32/kernel32 unavailable"

        ready_event = threading.Event()
        outcome: dict[str, Any] = {"ok": False, "error": "unknown error", "tid": None}
        hotkey_id = self._hotkey_id

        def _invoke_safe() -> None:
            try:
                callback()
            except Exception:
                logger.exception("Unhandled exception inside hotkey callback for %r.", hotkey)

        def _message_loop() -> None:
            tid = int(kernel32.GetCurrentThreadId())
            outcome["tid"] = tid
            msg = wintypes.MSG()
            # Ensure this thread's Win32 message queue exists before ready_event is set.
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)

            registered = bool(
                user32.RegisterHotKey(
                    None,
                    hotkey_id,
                    modifiers | MOD_NOREPEAT,
                    vk_code,
                )
            )
            if not registered:
                err_code = int(kernel32.GetLastError())
                outcome["ok"] = False
                outcome["error"] = f"RegisterHotKey returned 0 (GetLastError={err_code})"
                ready_event.set()
                return

            outcome["ok"] = True
            outcome["error"] = ""
            ready_event.set()

            try:
                while True:
                    ret = int(user32.GetMessageW(ctypes.byref(msg), None, 0, 0))
                    if ret <= 0:
                        break
                    if msg.message == WM_HOTKEY and msg.wParam == hotkey_id:
                        _invoke_safe()
            finally:
                try:
                    user32.UnregisterHotKey(None, hotkey_id)
                except Exception:
                    logger.debug("UnregisterHotKey failed during cleanup.", exc_info=True)

        thread = threading.Thread(
            target=_message_loop,
            name="QuickClip-HotkeyListener",
            daemon=True,
        )
        thread.start()

        if not ready_event.wait(timeout=3.0):
            if outcome["tid"] is not None:
                self._post_wm_quit(user32, int(outcome["tid"]))
            thread.join(timeout=1.0)
            return False, "Timed out waiting for Win32 hotkey thread initialization"

        if not outcome["ok"]:
            thread.join(timeout=1.0)
            return False, str(outcome["error"])

        with self._lock:
            self._thread = thread
            self._thread_id = outcome["tid"]
            self._hotkey = hotkey
            self._backend = "win32"

        return True, ""

    def _register_keyboard_fallback(
        self,
        hotkey: str,
        callback: Callable[[], None],
    ) -> None:
        import keyboard

        normalized = "+".join(part.strip().lower() for part in hotkey.split("+"))

        def _invoke_safe() -> None:
            try:
                callback()
            except Exception:
                logger.exception(
                    "Unhandled exception inside keyboard fallback callback for %r.",
                    hotkey,
                )

        handle = keyboard.add_hotkey(normalized, _invoke_safe, suppress=False)
        with self._lock:
            self._keyboard_module = keyboard
            self._keyboard_handle = handle
            self._hotkey = hotkey
            self._backend = "keyboard"
