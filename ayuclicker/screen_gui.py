"""Screen-region clicker for Windows, using the built-in Windows OCR engine."""

from datetime import datetime
import json
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from .core import Settings
from .editing import install_editing
from .screen_core import Reading, Rect, ScreenSchedule, normalize
from .storage import data_directory
from .windows_screen import OcrProcess, ScreenError, Stopped, StopHotkey, WindowsDesktop


class ScreenWorker:
    def __init__(self, desktop, target, text, button, settings, language, seen, emit, *, ocr_factory=OcrProcess, poll_interval=0.25):
        self.desktop = desktop
        self.target = target
        self.text = text
        self.button = button
        self.settings = settings
        self.language = language
        self.seen = seen
        self.emit = emit
        self.ocr_factory = ocr_factory
        self.poll_interval = poll_interval
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def run(self):
        ocr = None
        schedule = ScreenSchedule(self.settings, self.seen)
        active = None
        try:
            ocr = self.ocr_factory(self.language, self.stop_event)
            self.emit("log", f"Windows OCR: {ocr.info.get('language', '?')}. Переключитесь в AyuGram.")
            while not self.stop_event.is_set():
                ready = self.desktop.is_ready(self.target)
                if ready != active:
                    active = ready
                    self.emit("status", "Наблюдение" if ready else "Ожидание окна AyuGram")
                if not ready:
                    if schedule.cancel().kind == "cancelled":
                        self.emit("log", "Окно не активно: ожидающее нажатие отменено.")
                    self.stop_event.wait(0.2)
                    continue
                text, button = self.desktop.regions(self.target, self.text, self.button)
                reading = ocr.recognize(text, button)
                if self.stop_event.is_set():
                    break
                # OCR may take time: check focus and window position again.
                if not self.desktop.is_ready(self.target) or (text, button) != self.desktop.regions(self.target, self.text, self.button):
                    schedule.cancel()
                    continue
                decision = schedule.observe(reading, time.monotonic())
                if decision.kind == "scheduled":
                    self.emit("log", f"Условие найдено дважды. Задержка: {decision.delay:.2f} с.")
                elif decision.kind == "cancelled":
                    self.emit("log", "Текст или кнопка изменились: ожидающее нажатие отменено.")
                elif decision.kind == "ready":
                    if not self.stop_event.is_set() and schedule.claim(reading):
                        if self.desktop.click(self.target, button, self.stop_event):
                            self.emit("log", "Нажатие мышью отправлено. Этот текст повторно не обрабатывается.")
                        else:
                            self.emit("log", "Нажатие отменено: окно не активно или включена остановка.")
                self.stop_event.wait(self.poll_interval)
        except Stopped:
            pass
        except Exception as error:
            self.emit("error", str(error) if isinstance(error, ScreenError) else f"Ошибка режима экрана: {type(error).__name__}.")
        finally:
            schedule.cancel()
            if ocr is not None:
                ocr.close()
            self.emit("finished")


class SelectionOverlay:
    def __init__(self, root, done, cancel):
        self.done = done
        self.cancel = cancel
        self.start = None
        self.rectangle = None
        self.window = tk.Toplevel(root)
        self.window.attributes("-fullscreen", True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-alpha", 0.30)
        self.window.configure(background="black", cursor="crosshair")
        self.canvas = tk.Canvas(self.window, bg="black", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(25, 25, anchor="nw", text="Выделите область мышью. Esc — отмена.", fill="white", font=("Segoe UI", 16))
        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        self.window.bind("<Escape>", lambda event: self.abort())
        self.window.focus_force()

    def press(self, event):
        self.start = (event.x_root, event.y_root)
        if self.rectangle is not None:
            self.canvas.delete(self.rectangle)
        self.rectangle = self.canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#00ffb3", width=3)

    def drag(self, event):
        if self.start is not None:
            self.canvas.coords(self.rectangle, self.start[0] - self.window.winfo_rootx(),
                               self.start[1] - self.window.winfo_rooty(), event.x, event.y)

    def release(self, event):
        if self.start is None:
            return
        x, y = self.start
        rect = Rect(min(x, event.x_root), min(y, event.y_root), abs(event.x_root - x), abs(event.y_root - y))
        self.window.destroy()
        self.done(rect)

    def abort(self):
        self.window.destroy()
        self.cancel()


class ScreenApp:
    def __init__(self, root, desktop, *, directory=None, register_hotkey=True):
        self.root = root
        self.desktop = desktop
        self.directory = directory or data_directory()
        self.events = queue.Queue()
        self.worker = None
        self.target = None
        self.text_region = None
        self.button_region = None
        self.seen = set()
        self.overlay = None
        self.closing = False
        self.inputs = []
        self.hotkey = StopHotkey(desktop, self.emit) if register_hotkey else None
        try:
            config = json.loads((self.directory / "screen_settings.json").read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                config = {}
        except (ValueError, OSError):
            config = {}
        self.values = {name: tk.StringVar(value=str(config.get(name, default))) for name, default in (
            ("word", ""), ("button", ""), ("minimum", "2"), ("maximum", "5"), ("language", "Русский"),
        )}
        self.whole_word = tk.BooleanVar(value=bool(config.get("whole_word", True)))
        self.status = tk.StringVar(value="Остановлено")
        self.text_status = tk.StringVar(value="Не выбрана")
        self.button_status = tk.StringVar(value="Не выбрана")
        root.title("AyuClicker — без API")
        root.geometry("780x710")
        root.minsize(720, 670)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.build()
        self.root.after(100, self.poll)

    def build(self):
        frame = ttk.Frame(self.root, padding=20)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="AyuClicker — без API", font=("Segoe UI", 18, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(frame, text="Выберите текст и кнопку ОДНОГО сообщения. Не выделяйте весь чат.\n"
                  "AyuGram должен быть видимым и активным. Остановка — F8.", wraplength=680).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(5, 16))
        for row, name, label in ((2, "word", "Слова / фразы"), (3, "button", "Название кнопки")):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12))
            entry = ttk.Entry(frame, textvariable=self.values[name])
            install_editing(entry)
            entry.grid(row=row, column=1, columnspan=2, sticky="ew", pady=5)
            self.inputs.append(entry)
        matching = ttk.Frame(frame)
        matching.grid(row=4, column=1, columnspan=2, sticky="w", pady=(0, 10))
        checkbox = ttk.Checkbutton(matching, text="Искать целые слова / фразы", variable=self.whole_word)
        checkbox.pack(anchor="w")
        ttk.Label(matching, text="Пример: Bancorp(497213,518913), Сбер. В скобках — любой из указанных BIN.", wraplength=480).pack(anchor="w")
        self.inputs.append(checkbox)

        ttk.Label(frame, text="Задержка, сек.").grid(row=5, column=0, sticky="w")
        delays = ttk.Frame(frame)
        delays.grid(row=5, column=1, sticky="w")
        for name, label in (("minimum", "от"), ("maximum", "до")):
            ttk.Label(delays, text=label).pack(side="left", padx=(0, 5))
            entry = ttk.Entry(delays, width=10, textvariable=self.values[name])
            install_editing(entry)
            entry.pack(side="left", padx=(0, 15))
            self.inputs.append(entry)

        ttk.Label(frame, text="Язык Windows OCR").grid(row=6, column=0, sticky="w", padx=(0, 12), pady=10)
        languages = ttk.Combobox(frame, values=("Русский", "English", "Автоматически"), textvariable=self.values["language"], state="readonly")
        languages.grid(row=6, column=1, sticky="w")
        self.inputs.append(languages)
        self.language_box = languages

        text_button = ttk.Button(frame, text="1. Выбрать текст сообщения", command=lambda: self.select("text"))
        text_button.grid(row=7, column=0, columnspan=2, sticky="w", pady=6)
        ttk.Label(frame, textvariable=self.text_status).grid(row=7, column=2, sticky="w")
        button_button = ttk.Button(frame, text="2. Выбрать кнопку этого сообщения", command=lambda: self.select("button"))
        button_button.grid(row=8, column=0, columnspan=2, sticky="w", pady=6)
        ttk.Label(frame, textvariable=self.button_status).grid(row=8, column=2, sticky="w")
        self.inputs.extend((text_button, button_button))
        ttk.Label(frame, text="Текст выделяйте без кнопок. Кнопку — целиком, с её названием.\n"
                  "Если сообщение сдвинется или изменит высоту, выберите области заново.", wraplength=680).grid(
            row=9, column=0, columnspan=3, sticky="w", pady=(5, 8))

        controls = ttk.Frame(frame)
        controls.grid(row=10, column=0, columnspan=3, sticky="ew", pady=10)
        self.check_button = ttk.Button(controls, text="Проверить распознавание", command=self.check)
        self.check_button.pack(side="left")
        self.start_button = ttk.Button(controls, text="Старт", command=self.start)
        self.start_button.pack(side="left", padx=10)
        self.stop_button = ttk.Button(controls, text="Стоп / F8", command=self.stop, state="disabled")
        self.stop_button.pack(side="left")
        ttk.Label(frame, textvariable=self.status).grid(row=11, column=0, columnspan=3, sticky="w", pady=5)

        log_frame = ttk.Frame(frame)
        log_frame.grid(row=12, column=0, columnspan=3, sticky="nsew")
        frame.rowconfigure(12, weight=1)
        self.log_widget = tk.Text(log_frame, height=7, state="disabled", wrap="word", font=("Consolas", 10))
        self.log_widget.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_widget.yview)
        scrollbar.pack(side="right", fill="y")
        self.log_widget.configure(yscrollcommand=scrollbar.set)

    def emit(self, *event):
        if event[0] == "hotkey" and self.worker is not None:
            self.worker.stop()
        self.events.put(event)

    def log(self, text):
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", f"{datetime.now():%H:%M:%S}  {text}\n")
        lines = int(self.log_widget.index("end-1c").split(".")[0])
        if lines > 600:
            self.log_widget.delete("1.0", f"{lines - 500}.0")
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def select(self, kind):
        if self.worker is not None:
            return
        if kind == "button" and self.text_region is None:
            messagebox.showinfo("Выбор области", "Сначала выделите текст сообщения.", parent=self.root)
            return
        self.root.withdraw()
        self.root.after(200, lambda: self.open_overlay(kind))

    def open_overlay(self, kind):
        self.overlay = SelectionOverlay(self.root, lambda rect: self.root.after(150, lambda: self.selected(kind, rect)), self.cancel_selection)

    def cancel_selection(self):
        self.overlay = None
        self.root.deiconify()

    def selected(self, kind, rect):
        self.overlay = None
        try:
            if not rect.valid():
                raise ScreenError("Область слишком маленькая. Выделите текст или кнопку целиком.")
            target = self.desktop.target_at(rect)
            bounds = self.desktop.bounds(target.handle)
            relative = rect.offset(-bounds.x, -bounds.y)
            if kind == "text":
                self.target = target
                self.text_region = relative
                self.button_region = None
                self.text_status.set(f"{relative.width}×{relative.height}")
                self.button_status.set("Не выбрана")
            else:
                if target.handle != self.target.handle or target.process_id != self.target.process_id:
                    raise ScreenError("Текст и кнопка должны находиться в одном окне AyuGram.")
                if relative.intersects(self.text_region):
                    raise ScreenError("Области текста и кнопки не должны пересекаться.")
                if relative.y < self.text_region.y + self.text_region.height:
                    raise ScreenError("Выберите кнопку под текстом этого сообщения.")
                self.button_region = relative
                self.button_status.set(f"{relative.width}×{relative.height}")
        except ScreenError as error:
            self.root.deiconify()
            messagebox.showerror("Выбор области", str(error), parent=self.root)
        finally:
            self.root.deiconify()

    def language(self):
        return {"Русский": "ru-RU", "English": "en-US", "Автоматически": ""}.get(self.values["language"].get(), "ru-RU")

    def validate_regions(self):
        if self.target is None or self.text_region is None or self.button_region is None:
            raise ScreenError("Выберите область текста и область кнопки одного сообщения.")
        self.desktop.regions(self.target, self.text_region, self.button_region)

    def set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for widget in self.inputs:
            widget.configure(state=state)
        if not busy:
            self.language_box.configure(state="readonly")
        self.start_button.configure(state=state)
        self.check_button.configure(state=state)
        self.stop_button.configure(state="normal" if busy else "disabled")

    def save(self):
        config = {name: value.get() for name, value in self.values.items()}
        config["whole_word"] = self.whole_word.get()
        temporary = self.directory / "screen_settings.json.tmp"
        temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.directory / "screen_settings.json")

    def start(self):
        if self.worker is not None:
            return
        try:
            self.validate_regions()
            settings = Settings(
                normalize(self.values["word"].get()), normalize(self.values["button"].get()),
                float(self.values["minimum"].get().replace(",", ".")),
                float(self.values["maximum"].get().replace(",", ".")), self.whole_word.get(),
            )
            self.save()
        except (ScreenError, ValueError, OSError) as error:
            messagebox.showerror("Проверьте настройки", str(error), parent=self.root)
            return
        self.set_busy(True)
        self.status.set("Запуск Windows OCR…")
        self.worker = ScreenWorker(self.desktop, self.target, self.text_region, self.button_region,
                                   settings, self.language(), self.seen, self.emit)
        self.worker.start()
        self.root.iconify()

    def check(self):
        if self.worker is not None:
            return
        try:
            self.validate_regions()
        except ScreenError as error:
            messagebox.showerror("Выберите области", str(error), parent=self.root)
            return
        # Reuse the same lifecycle and stop control as normal observation.
        language = self.language()
        worker = ScreenWorker(self.desktop, self.target, self.text_region, self.button_region,
                              Settings("проверка", "проверка", 0, 0), language, self.seen, self.emit)
        self.worker = worker
        self.set_busy(True)
        self.status.set("Проверка: переключитесь в AyuGram")
        self.root.iconify()

        def run_check():
            ocr = None
            try:
                ocr = OcrProcess(language, worker.stop_event)
                timeout = time.monotonic() + 30
                while not self.desktop.is_ready(self.target):
                    if worker.stop_event.wait(0.1):
                        raise Stopped()
                    if time.monotonic() > timeout:
                        raise ScreenError("Для проверки переключитесь в выбранное окно AyuGram.")
                regions = self.desktop.regions(self.target, self.text_region, self.button_region)
                reading = ocr.recognize(*regions)
                if not worker.stop_event.is_set():
                    self.emit("preview", reading)
            except Stopped:
                pass
            except Exception as error:
                self.emit("error", str(error) if isinstance(error, ScreenError) else f"Ошибка OCR: {type(error).__name__}.")
            finally:
                if ocr is not None:
                    ocr.close()
                self.emit("finished")

        worker.thread = threading.Thread(target=run_check, daemon=True)
        worker.start()

    def stop(self):
        if self.worker is not None:
            self.worker.stop()
            self.status.set("Остановка…")
            self.stop_button.configure(state="disabled")

    def poll(self):
        for _ in range(100):
            try:
                kind, *args = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.log(args[0])
            elif kind == "status":
                self.status.set(args[0])
            elif kind == "hotkey":
                self.stop()
                if not self.closing:
                    self.root.deiconify()
            elif kind == "error":
                self.log(args[0])
                if not self.closing:
                    self.root.deiconify()
                    messagebox.showerror("Windows OCR", args[0], parent=self.root)
            elif kind == "preview":
                if not self.closing:
                    self.root.deiconify()
                    messagebox.showinfo("Распознанный текст", f"Текст сообщения:\n{args[0].text}\n\nНазвание кнопки:\n{args[0].button}", parent=self.root)
            elif kind == "finished":
                self.worker = None
                self.set_busy(False)
                self.status.set("Остановлено")
                self.log("Остановлено. Ожидающие нажатия отменены.")
                if not self.closing:
                    self.root.deiconify()
        if self.closing and self.worker is None:
            if self.hotkey is not None:
                self.hotkey.close()
            self.root.destroy()
            return
        self.root.after(100, self.poll)

    def close(self):
        self.closing = True
        if self.overlay is not None:
            self.overlay.window.destroy()
            self.overlay = None
        if self.worker is not None:
            self.worker.stop()


def main():
    if os.name != "nt":
        raise SystemExit("Режим без API запускается на Windows 10/11.")
    desktop = WindowsDesktop()
    desktop.enable_dpi()
    root = tk.Tk()
    ScreenApp(root, desktop)
    root.mainloop()
