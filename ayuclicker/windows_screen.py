"""Native Windows window, mouse and OCR access. No Telegram API keys."""

import base64
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

from .screen_core import Reading, Rect


class ScreenError(Exception):
    pass


class Stopped(Exception):
    pass


@dataclass(frozen=True)
class Target:
    handle: int
    process_id: int
    width: int
    height: int
    title: str


class WindowsDesktop:
    def __init__(self):
        if os.name != "nt":
            raise ScreenError("Режим экрана работает только на Windows 10/11.")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user.GetForegroundWindow.restype = wintypes.HWND
        self.user.WindowFromPoint.argtypes = [wintypes.POINT]
        self.user.WindowFromPoint.restype = wintypes.HWND
        self.user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user.GetAncestor.restype = wintypes.HWND
        self.user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user.GetWindowRect.restype = wintypes.BOOL
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user.GetWindowTextW.restype = ctypes.c_int
        self.user.IsIconic.argtypes = [wintypes.HWND]
        self.user.IsIconic.restype = wintypes.BOOL
        self.user.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        self.user.SetCursorPos.restype = wintypes.BOOL
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.user.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        self.user.RegisterHotKey.restype = wintypes.BOOL
        self.user.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user.UnregisterHotKey.restype = wintypes.BOOL
        self.user.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        self.user.GetMessageW.restype = wintypes.BOOL
        self.user.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user.PostThreadMessageW.restype = wintypes.BOOL
        self.kernel.GetCurrentThreadId.restype = wintypes.DWORD

    def enable_dpi(self):
        try:
            self.user.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            self.user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except AttributeError:
            self.user.SetProcessDPIAware()

    def bounds(self, handle) -> Rect:
        rect = wintypes.RECT()
        if not self.user.GetWindowRect(handle, ctypes.byref(rect)):
            raise ScreenError("Выбранное окно закрыто. Выберите области заново.")
        return Rect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)

    def process_id(self, handle) -> int:
        value = wintypes.DWORD()
        self.user.GetWindowThreadProcessId(handle, ctypes.byref(value))
        return value.value

    def target_at(self, rect: Rect) -> Target:
        point = wintypes.POINT(rect.x + rect.width // 2, rect.y + rect.height // 2)
        handle = self.user.GetAncestor(self.user.WindowFromPoint(point), 2)
        if not handle:
            raise ScreenError("Окно не найдено. Выберите область внутри AyuGram.")
        pid = self.process_id(handle)
        process = self.kernel.OpenProcess(0x1000, False, pid)
        if not process:
            raise ScreenError("Не удалось прочитать выбранное окно. Откройте AyuGram обычным способом.")
        try:
            path = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(path))
            if not self.kernel.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(length)):
                raise ScreenError("Не удалось определить приложение выбранного окна.")
            executable = Path(path.value).name.casefold()
        finally:
            self.kernel.CloseHandle(process)
        if "ayu" not in executable and "telegram" not in executable:
            raise ScreenError("Выберите сообщение в окне AyuGram / Telegram Desktop.")
        bounds = self.bounds(handle)
        if not rect.inside(bounds):
            raise ScreenError("Область должна полностью находиться внутри окна AyuGram.")
        title = ctypes.create_unicode_buffer(512)
        self.user.GetWindowTextW(handle, title, len(title))
        return Target(handle, pid, bounds.width, bounds.height, title.value)

    def is_ready(self, target: Target) -> bool:
        return (
            self.user.GetForegroundWindow() == target.handle
            and self.process_id(target.handle) == target.process_id
            and not self.user.IsIconic(target.handle)
        )

    def regions(self, target: Target, text: Rect, button: Rect) -> tuple[Rect, Rect]:
        bounds = self.bounds(target.handle)
        if (bounds.width, bounds.height) != (target.width, target.height):
            raise ScreenError("Размер окна изменился. Остановите кликер и выберите области заново.")
        local_bounds = Rect(0, 0, bounds.width, bounds.height)
        if not text.inside(local_bounds) or not button.inside(local_bounds):
            raise ScreenError("Выбранные области вышли за границы окна.")
        return text.offset(bounds.x, bounds.y), button.offset(bounds.x, bounds.y)

    def click(self, target: Target, button: Rect, stop: threading.Event) -> bool:
        if stop.is_set() or not self.is_ready(target):
            return False
        x, y = button.x + button.width // 2, button.y + button.height // 2
        # Do not move another application's cursor and then click a stale point.
        if not self.user.SetCursorPos(x, y):
            raise ScreenError("Не удалось переместить курсор.")
        root = self.user.GetAncestor(self.user.WindowFromPoint(wintypes.POINT(x, y)), 2)
        if stop.is_set() or not self.is_ready(target) or root != target.handle:
            return False
        # Explicit Win32 input types work on both 32-bit and 64-bit Python.
        class MouseInput(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class InputUnion(ctypes.Union):
            _fields_ = [("mi", MouseInput)]

        class Input(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("data", InputUnion)]

        self.user.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
        self.user.SendInput.restype = wintypes.UINT
        inputs = (Input * 2)()
        inputs[0].type = inputs[1].type = 0
        inputs[0].data.mi.dwFlags = 0x0002
        inputs[1].data.mi.dwFlags = 0x0004
        sent = self.user.SendInput(2, inputs, ctypes.sizeof(Input))
        if sent != 2:
            # Release a partially submitted press before reporting failure.
            release = Input()
            release.data.mi.dwFlags = 0x0004
            self.user.SendInput(1, ctypes.byref(release), ctypes.sizeof(Input))
            raise ScreenError("Windows не приняла нажатие. Запустите AyuGram без прав администратора.")
        return True


class OcrProcess:
    def __init__(self, language: str, stop: threading.Event):
        self.stop = stop
        self.responses = queue.Queue()
        source = Path(__file__).with_name("windows_ocr.ps1").read_text(encoding="utf-8")
        encoded = base64.b64encode(source.encode("utf-16-le")).decode("ascii")
        executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        try:
            self.process = subprocess.Popen(
                [str(executable), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as error:
            raise ScreenError("Не удалось запустить Windows PowerShell 5.1.") from error
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self._send({"language": language})
            self.info = self._receive()
            if not self.info.get("ready"):
                raise ScreenError("Windows OCR не запустилась.")
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.responses.put(json.loads(line))
                except ValueError:
                    continue
        except (OSError, ValueError):
            pass
        finally:
            self.responses.put({"error": "Windows OCR завершилась. Нужны Windows 10/11 и компонент распознавания текста."})

    def _send(self, data):
        try:
            self.process.stdin.write(json.dumps(data, ensure_ascii=True) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise ScreenError("Соединение с Windows OCR прервано.") from error

    def _receive(self):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.stop.is_set():
                raise Stopped()
            try:
                response = self.responses.get(timeout=0.1)
            except queue.Empty:
                continue
            if "error" in response:
                raise ScreenError(response["error"])
            return response
        raise ScreenError("Windows OCR не ответила за 30 секунд. Проверьте компонент OCR в параметрах языка Windows.")

    def recognize(self, text: Rect, button: Rect) -> Reading:
        def coordinates(rect):
            return {"x": rect.x, "y": rect.y, "width": rect.width, "height": rect.height}

        self._send({"text": coordinates(text), "button": coordinates(button)})
        response = self._receive()
        return Reading(response.get("text", ""), response.get("button", ""))

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass


class StopHotkey:
    def __init__(self, desktop: WindowsDesktop, emit):
        self.desktop = desktop
        self.emit = emit
        self.thread_id = None
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        self.thread_id = self.desktop.kernel.GetCurrentThreadId()
        if not self.desktop.user.RegisterHotKey(None, 101, 0x4000, 0x77):
            self.emit("log", "F8 занята другим приложением. Для остановки используйте кнопку «Стоп».")
            return
        try:
            if self.closed.is_set():
                return
            message = wintypes.MSG()
            while self.desktop.user.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == 0x0312 and message.wParam == 101:
                    self.emit("hotkey")
        finally:
            self.desktop.user.UnregisterHotKey(None, 101)

    def close(self):
        self.closed.set()
        if self.thread_id is not None:
            self.desktop.user.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)
        self.thread.join(timeout=1)
