"""Windows-friendly Tk UI. All Telegram operations run off the UI thread."""

import asyncio
import concurrent.futures
from datetime import datetime
import queue
import re
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
import webbrowser

from .core import Settings
from .storage import data_directory, load_config, save_config
from .telegram import UserCancelled, explain_error, run_session


class Worker:
    def __init__(self, emit, directory, api_id, api_hash, chat, settings):
        self.emit = emit
        self.arguments = (directory, api_id, api_hash, chat, settings)
        self.loop = None
        self.stop_event = None
        self.stop_requested = threading.Event()
        self.thread = threading.Thread(target=self._thread, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_requested.set()
        if self.loop is not None and self.stop_event is not None:
            try:
                self.loop.call_soon_threadsafe(self.stop_event.set)
            except RuntimeError:
                pass  # The worker already finished.

    async def _prompt(self, text, secret):
        future = concurrent.futures.Future()
        self.emit("prompt", text, secret, future)
        answer = await asyncio.wrap_future(future)
        if answer is None:
            raise UserCancelled()
        return answer

    def _thread(self):
        try:
            asyncio.run(self._run())
        except UserCancelled:
            self.emit("log", "Вход отменён.")
        except Exception as error:
            self.emit("error", explain_error(error))
        finally:
            self.emit("finished")

    async def _run(self):
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        if self.stop_requested.is_set():
            self.stop_event.set()
        job = asyncio.create_task(run_session(*self.arguments, self.emit, self._prompt))
        stop = asyncio.create_task(self.stop_event.wait())
        try:
            done, _ = await asyncio.wait((job, stop), return_when=asyncio.FIRST_COMPLETED)
            if stop in done:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
                self.emit("log", "Остановлено. Ожидающие нажатия отменены.")
            else:
                await job
        finally:
            stop.cancel()
            await asyncio.gather(stop, return_exceptions=True)
            if not job.done():
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)


class App:
    def __init__(self, root, directory=None):
        self.root = root
        self.directory = directory or data_directory()
        self.events = queue.Queue()
        self.worker = None
        self.closing = False
        self.chat_ids = {}
        self.config = load_config(self.directory)
        self.inputs = []
        self.variables = {}
        root.title("AyuClicker — кнопки по условию")
        root.geometry("740x680")
        root.minsize(650, 620)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self.root.after(100, self.poll)

    def _build(self):
        frame = ttk.Frame(self.root, padding=20)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="AyuClicker", font=("Segoe UI", 20, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        ttk.Label(frame, text="Нажимает кнопку бота, когда сообщение содержит заданное слово.", wraplength=650).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(0, 18)
        )
        self._entry(frame, 2, "API ID", "api_id", "")
        self._entry(frame, 3, "API hash", "api_hash", "", show="•")
        ttk.Button(frame, text="Получить API ID / hash", command=lambda: webbrowser.open(
            "https://my.telegram.org/apps"
        )).grid(row=4, column=1, sticky="w", pady=(0, 5))
        self.remember = tk.BooleanVar(value=bool(self.config.get("remember_hash", False)))
        remember_box = ttk.Checkbutton(frame, text="Сохранить API hash локально", variable=self.remember)
        remember_box.grid(row=5, column=1, sticky="w", pady=(0, 12))
        self.inputs.append(remember_box)

        ttk.Label(frame, text="Чат").grid(row=6, column=0, sticky="w", padx=(0, 12))
        self.variables["chat"] = tk.StringVar(value=str(self.config.get("chat", "")))
        self.chat_box = ttk.Combobox(frame, textvariable=self.variables["chat"])
        self.chat_box.grid(row=6, column=1, sticky="ew", pady=5)
        self.inputs.append(self.chat_box)
        self.load_button = ttk.Button(frame, text="Загрузить чаты", command=lambda: self.start(list_only=True))
        self.load_button.grid(row=6, column=2, padx=(10, 0))
        ttk.Label(frame, text="Выберите чат из списка либо введите @username или ID.", wraplength=440).grid(
            row=7, column=1, columnspan=2, sticky="w", pady=(0, 8)
        )
        self._entry(frame, 8, "Слово / фраза", "word", "")
        self._entry(frame, 9, "Название кнопки", "button", "")
        self.whole_word = tk.BooleanVar(value=bool(self.config.get("whole_word", True)))
        whole_box = ttk.Checkbutton(frame, text="Искать целое слово / фразу", variable=self.whole_word)
        whole_box.grid(row=10, column=1, sticky="w", pady=(0, 8))
        self.inputs.append(whole_box)

        ttk.Label(frame, text="Задержка, сек.").grid(row=11, column=0, sticky="w", padx=(0, 12))
        delay = ttk.Frame(frame)
        delay.grid(row=11, column=1, sticky="w", pady=5)
        for name, default, label in (("minimum", "2", "от"), ("maximum", "5", "до")):
            ttk.Label(delay, text=label).pack(side="left", padx=(0, 5))
            variable = tk.StringVar(value=str(self.config.get(name, default)))
            self.variables[name] = variable
            entry = ttk.Entry(delay, width=9, textvariable=variable)
            entry.pack(side="left", padx=(0, 14))
            self.inputs.append(entry)

        controls = ttk.Frame(frame)
        controls.grid(row=12, column=0, columnspan=3, sticky="ew", pady=(14, 10))
        self.start_button = ttk.Button(controls, text="Старт", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="Стоп", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=10)
        self.status = tk.StringVar(value="Остановлено")
        ttk.Label(controls, textvariable=self.status).pack(side="right")

        ttk.Label(frame, text="Журнал действий").grid(row=13, column=0, columnspan=3, sticky="w")
        log_frame = ttk.Frame(frame)
        log_frame.grid(row=14, column=0, columnspan=3, sticky="nsew", pady=(5, 0))
        frame.rowconfigure(14, weight=1)
        self.log_widget = tk.Text(log_frame, height=8, state="disabled", wrap="word", font=("Consolas", 10))
        self.log_widget.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_widget.yview)
        scrollbar.pack(side="right", fill="y")
        self.log_widget.configure(yscrollcommand=scrollbar.set)

    def _entry(self, frame, row, label, name, default, **kwargs):
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12))
        variable = tk.StringVar(value=str(self.config.get(name, default)))
        self.variables[name] = variable
        entry = ttk.Entry(frame, textvariable=variable, **kwargs)
        entry.grid(row=row, column=1, columnspan=2, sticky="ew", pady=5)
        self.inputs.append(entry)

    def emit(self, *event):
        self.events.put(event)

    def log(self, text):
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", f"{datetime.now():%H:%M:%S}  {text}\n")
        # Keep the widget bounded during long sessions.
        lines = int(self.log_widget.index("end-1c").split(".")[0])
        if lines > 600:
            self.log_widget.delete("1.0", f"{lines - 500}.0")
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def busy(self, value):
        state = "disabled" if value else "normal"
        for widget in self.inputs:
            widget.configure(state=state)
        self.start_button.configure(state=state)
        self.load_button.configure(state=state)
        self.stop_button.configure(state="normal" if value else "disabled")

    def start(self, list_only=False):
        if self.worker is not None:
            return
        try:
            values = {name: variable.get().strip() for name, variable in self.variables.items()}
            try:
                api_id = int(values["api_id"])
            except ValueError as error:
                raise ValueError("API ID должен быть положительным целым числом.") from error
            if api_id <= 0:
                raise ValueError("API ID должен быть положительным целым числом.")
            if re.fullmatch(r"[0-9a-fA-F]{32}", values["api_hash"]) is None:
                raise ValueError("API hash должен содержать 32 символа с my.telegram.org.")
            chat = str(self.chat_ids.get(values["chat"], values["chat"]))
            settings = None
            if not list_only:
                if not chat:
                    raise ValueError("Выберите чат. Можно сначала нажать «Загрузить чаты».")
                try:
                    low = float(values["minimum"].replace(",", "."))
                    high = float(values["maximum"].replace(",", "."))
                except ValueError as error:
                    raise ValueError("Задержки нужно указать числами в секундах.") from error
                settings = Settings(values["word"], values["button"], low, high, self.whole_word.get())
            config = {
                **values, "chat": chat, "whole_word": self.whole_word.get(),
                "remember_hash": self.remember.get(),
                "api_hash": values["api_hash"] if self.remember.get() else "",
            }
            save_config(self.directory, config)
        except (ValueError, OSError) as error:
            messagebox.showerror("Проверьте настройки", str(error), parent=self.root)
            return
        self.busy(True)
        self.status.set("Подключение…")
        self.worker = Worker(self.emit, self.directory, api_id, values["api_hash"], chat, settings)
        self.worker.start()

    def stop(self):
        if self.worker is not None:
            self.status.set("Остановка…")
            self.stop_button.configure(state="disabled")
            self.worker.stop()

    def poll(self):
        for _ in range(100):
            try:
                kind, *args = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.log(args[0])
            elif kind == "error":
                self.log(args[0])
                if not self.closing:
                    messagebox.showerror("AyuClicker", args[0], parent=self.root)
            elif kind == "watching":
                self.status.set("Работает")
            elif kind == "finished":
                self.worker = None
                self.busy(False)
                self.status.set("Остановлено")
            elif kind == "dialogs":
                current = str(self.chat_ids.get(self.variables["chat"].get(), self.variables["chat"].get()))
                self.chat_ids = {f"{name} [{identifier}]": identifier for name, identifier in args[0]}
                self.chat_box.configure(values=list(self.chat_ids))
                for label, identifier in self.chat_ids.items():
                    if str(identifier) == current:
                        self.variables["chat"].set(label)
                        break
            elif kind == "prompt":
                text, secret, future = args
                if future.done():
                    continue
                if self.closing:
                    future.set_result(None)
                    continue
                answer = simpledialog.askstring("Вход в Telegram", text, show="•" if secret else "", parent=self.root)
                if not future.done():
                    future.set_result(answer)
        if self.closing and self.worker is None:
            self.root.destroy()
            return
        self.root.after(100, self.poll)

    def close(self):
        self.closing = True
        if self.worker is not None:
            self.stop()
        else:
            self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()
