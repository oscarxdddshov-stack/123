"""Configure the bot's actual confirmation phrases without guessed defaults."""

from tkinter import messagebox, simpledialog, ttk

from .core import Settings
from .editing import install_editing
from .telegram import ConfigurationError, parse_chat


class ResultDialog(simpledialog.Dialog):
    def __init__(self, parent, mine="", other="", success_chat="", chats=None):
        self.mine = mine
        self.other = other
        self.success_chat = success_chat
        self.chats = chats or {}
        super().__init__(parent, title="Признаки результата")

    def body(self, master):
        ttk.Label(master, text="Укажите фразы результата; варианты разделяйте ;.\n"
                  "Если полученная заявка приходит в личный диалог бота, укажите его ID / @username ниже.\n"
                  "Тогда «Забрал я» можно оставить пустым: проверяются оба тега\n"
                  "в конце сообщения, например #AD773474352 [#SourDog].", wraplength=560).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        self.entries = []
        for row, label, value in ((1, "Забрал я", self.mine), (2, "Забрал другой", self.other),
                                  (3, "Бот подтверждения", self.success_chat)):
            ttk.Label(master, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10))
            entry = ttk.Entry(master, width=55)
            entry.insert(0, value)
            install_editing(entry)
            entry.grid(row=row, column=1, sticky="ew", pady=5)
            self.entries.append(entry)
        if self.chats:
            self.picker = ttk.Combobox(master, state="readonly", values=list(self.chats), width=52)
            self.picker.grid(row=4, column=1, sticky="ew", pady=5)
            self.picker.bind("<<ComboboxSelected>>", self.choose_bot)
        ttk.Label(master, text="Оставьте поле бота пустым, чтобы использовать только фразы.\n"
                  "Диалог бота не добавляйте в основной список чатов с кнопками.", wraplength=560).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))
        return self.entries[0]

    def choose_bot(self, event=None):
        self.entries[2].delete(0, "end")
        self.entries[2].insert(0, str(self.chats[self.picker.get()]))

    def validate(self):
        try:
            Settings("тест", "кнопка", 1, 1, repeat_until_result=True,
                     mine_phrases=self.entries[0].get(), other_phrases=self.entries[1].get(),
                     success_chat=self.entries[2].get())
            if self.entries[2].get().strip():
                parse_chat(self.entries[2].get())
        except (ValueError, ConfigurationError) as error:
            messagebox.showerror("Признаки результата", str(error), parent=self)
            return False
        return True

    def apply(self):
        self.result = tuple(entry.get().strip() for entry in self.entries)
