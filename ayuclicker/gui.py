"""Windows-friendly Tk UI. All Telegram operations run off the UI thread."""

import asyncio
import concurrent.futures
from datetime import datetime
import queue
import re
import threading
import tkinter as tk
from tkinter import messagebox, ttk
import webbrowser

from .core import Settings
from .editing import LoginDialog, install_editing, paste_from_clipboard
from .storage import data_directory, load_config, save_config
from .telegram import ConfigurationError, UserCancelled, explain_error, parse_chats, run_session


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
        self.entries = {}
        self.paste_buttons = {}
        root.title("AyuClicker — кнопки по условию")
        root.geometry("780x740")
        root.minsize(700, 680)
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
        ttk.Label(frame, text="Нажимает кнопку бота, когда сообщение содержит любое из заданных слов.", wraplength=650).grid(
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

        ttk.Label(frame, text="Чаты (ID)").grid(row=6, column=0, sticky="nw", padx=(0, 12), pady=5)
        self.variables["chat"] = tk.StringVar(value=str(self.config.get("chat", "")))
        chat_frame = ttk.Frame(frame)
        chat_frame.grid(row=6, column=1, columnspan=2, sticky="ew", pady=5)
        self.chat_text = tk.Text(chat_frame, height=3, width=35, wrap="word", undo=True, exportselection=False)
        self.chat_text.pack(side="left", fill="both", expand=True)
        chat_scrollbar = ttk.Scrollbar(chat_frame, command=self.chat_text.yview)
        chat_scrollbar.pack(side="right", fill="y")
        self.chat_text.configure(yscrollcommand=chat_scrollbar.set)
        self.chat_text.insert("1.0", self.variables["chat"].get())
        self.chat_text.edit_modified(False)
        install_editing(self.chat_text)
        self.chat_text.bind("<<Modified>>", self._chat_modified)
        self.variables["chat"].trace_add("write", self._sync_chat_text)
        self.inputs.append(self.chat_text)

        self.chat_box = ttk.Combobox(frame, state="readonly")
        install_editing(self.chat_box)
        self.chat_box.grid(row=7, column=1, sticky="ew", pady=5)
        self.inputs.append(self.chat_box)
        self.add_chat_button = ttk.Button(frame, text="Добавить чат", command=self.add_selected_chat)
        self.add_chat_button.grid(row=7, column=2, padx=(10, 0), sticky="ew")
        self.inputs.append(self.add_chat_button)
        self.load_button = ttk.Button(frame, text="Загрузить чаты", command=lambda: self.start(list_only=True))
        self.load_button.grid(row=8, column=2, padx=(10, 0), sticky="ew")
        ttk.Label(frame, text="ID через запятую или с новой строки. Можно добавить чаты из списка.", wraplength=420).grid(
            row=8, column=1, sticky="w", pady=(0, 8)
        )
        self._entry(frame, 9, "Слова / фразы", "word", "")
        self._entry(frame, 10, "Название кнопки", "button", "")
        self.whole_word = tk.BooleanVar(value=bool(self.config.get("whole_word", True)))
        matching = ttk.Frame(frame)
        matching.grid(row=11, column=1, columnspan=2, sticky="w", pady=(0, 8))
        whole_box = ttk.Checkbutton(matching, text="Искать целые слова / фразы", variable=self.whole_word)
        whole_box.pack(anchor="w")
        ttk.Label(matching, text="Через запятую — любое из слов: BANCORP, Сбер, Альфа", wraplength=490).pack(anchor="w")
        self.inputs.append(whole_box)

        ttk.Label(frame, text="Задержка, сек.").grid(row=12, column=0, sticky="w", padx=(0, 12))
        delay = ttk.Frame(frame)
        delay.grid(row=12, column=1, sticky="w", pady=5)
        for name, default, label in (("minimum", "2", "от"), ("maximum", "5", "до")):
            ttk.Label(delay, text=label).pack(side="left", padx=(0, 5))
            variable = tk.StringVar(value=str(self.config.get(name, default)))
            self.variables[name] = variable
            entry = ttk.Entry(delay, width=9, textvariable=variable)
            install_editing(entry)
            entry.pack(side="left", padx=(0, 14))
            self.inputs.append(entry)

        controls = ttk.Frame(frame)
        controls.grid(row=13, column=0, columnspan=3, sticky="ew", pady=(14, 10))
        self.start_button = ttk.Button(controls, text="Старт", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="Стоп", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=10)
        self.status = tk.StringVar(value="Остановлено")
        ttk.Label(controls, textvariable=self.status).pack(side="right")

        ttk.Label(frame, text="Журнал действий").grid(row=14, column=0, columnspan=3, sticky="w")
        log_frame = ttk.Frame(frame)
        log_frame.grid(row=15, column=0, columnspan=3, sticky="nsew", pady=(5, 0))
        frame.rowconfigure(15, weight=1)
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
        self.entries[name] = entry
        install_editing(entry)
        credentials = name in ("api_id", "api_hash")
        entry.grid(row=row, column=1, columnspan=1 if credentials else 2, sticky="ew", pady=5)
        self.inputs.append(entry)
        if credentials:
            paste = ttk.Button(frame, text="Вставить", command=lambda: paste_from_clipboard(entry, replace_all=True))
            paste.grid(row=row, column=2, sticky="ew", padx=(10, 0))
            self.paste_buttons[name] = paste
            self.inputs.append(paste)

    def emit(self, *event):
        self.events.put(event)

    def _chat_modified(self, event=None):
        if self.chat_text.edit_modified():
            self.variables["chat"].set(self.chat_text.get("1.0", "end-1c"))
            self.chat_text.edit_modified(False)

    def _sync_chat_text(self, *_):
        value = self.variables["chat"].get()
        if self.chat_text.get("1.0", "end-1c") != value:
            state = str(self.chat_text.cget("state"))
            self.chat_text.configure(state="normal")
            self.chat_text.delete("1.0", "end")
            self.chat_text.insert("1.0", value)
            self.chat_text.edit_modified(False)
            self.chat_text.configure(state=state)

    def add_selected_chat(self):
        target = self.chat_ids.get(self.chat_box.get())
        if target is None:
            messagebox.showinfo("Добавить чат", "Загрузите чаты и выберите нужный в списке.", parent=self.root)
            return
        current = self.chat_text.get("1.0", "end-1c").strip()
        try:
            targets = list(parse_chats(current)) if current else []
        except ConfigurationError as error:
            messagebox.showerror("Список чатов", str(error), parent=self.root)
            return
        if target not in targets:
            targets.append(target)
        self.variables["chat"].set("\n".join(str(item) for item in targets))

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
        if not value:
            self.chat_box.configure(state="readonly")
        self.start_button.configure(state=state)
        self.load_button.configure(state=state)
        self.stop_button.configure(state="normal" if value else "disabled")

    def start(self, list_only=False):
        if self.worker is not None:
            return
        try:
            self.variables["chat"].set(self.chat_text.get("1.0", "end-1c"))
            values = {name: variable.get().strip() for name, variable in self.variables.items()}
            try:
                api_id = int(values["api_id"])
            except ValueError as error:
                raise ValueError("API ID должен быть положительным целым числом.") from error
            if api_id <= 0:
                raise ValueError("API ID должен быть положительным целым числом.")
            if re.fullmatch(r"[0-9a-fA-F]{32}", values["api_hash"]) is None:
                raise ValueError(
                    "API hash должен содержать ровно 32 символа: цифры 0–9 и буквы a–f.\n"
                    f"Сейчас введено символов: {len(values['api_hash'])}.\n"
                    "Скопируйте App api_hash с my.telegram.org и нажмите «Вставить» рядом с API hash."
                )
            chat = values["chat"]
            settings = None
            if not list_only:
                parse_chats(chat)
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
        except (ConfigurationError, ValueError, OSError) as error:
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
                self.status.set(f"Работает: чатов {args[0]}" if args else "Работает")
            elif kind == "finished":
                self.worker = None
                self.busy(False)
                self.status.set("Остановлено")
            elif kind == "dialogs":
                self.chat_ids = {f"{name} [{identifier}]": identifier for name, identifier in args[0]}
                self.chat_box.configure(values=list(self.chat_ids))
                try:
                    targets = parse_chats(self.variables["chat"].get())
                except ConfigurationError:
                    targets = ()
                for label, identifier in self.chat_ids.items():
                    if identifier in targets:
                        self.chat_box.set(label)
                        break
                else:
                    if self.chat_ids:
                        self.chat_box.set(next(iter(self.chat_ids)))
            elif kind == "prompt":
                text, secret, future = args
                if future.done():
                    continue
                if self.closing:
                    future.set_result(None)
                    continue
                answer = LoginDialog(self.root, text, secret).result
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
