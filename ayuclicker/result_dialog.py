"""Configure the bot's actual confirmation phrases without guessed defaults."""

from tkinter import messagebox, simpledialog, ttk

from .core import Settings
from .editing import install_editing


class ResultDialog(simpledialog.Dialog):
    def __init__(self, parent, mine="", other=""):
        self.mine = mine
        self.other = other
        super().__init__(parent, title="Признаки результата")

    def body(self, master):
        ttk.Label(master, text="Укажите фразы из ответа бота, которые подтверждают результат.\n"
                  "Проверяется ответ на кнопку, изменённый текст сообщения\n"
                  "или ответ того же бота на это сообщение. Варианты разделяйте ;.", wraplength=540).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        self.entries = []
        for row, label, value in ((1, "Забрал я", self.mine), (2, "Забрал другой", self.other)):
            ttk.Label(master, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10))
            entry = ttk.Entry(master, width=55)
            entry.insert(0, value)
            install_editing(entry)
            entry.grid(row=row, column=1, sticky="ew", pady=5)
            self.entries.append(entry)
        return self.entries[0]

    def validate(self):
        try:
            Settings("тест", "кнопка", 1, 1, repeat_until_result=True,
                     mine_phrases=self.entries[0].get(), other_phrases=self.entries[1].get())
        except ValueError as error:
            messagebox.showerror("Признаки результата", str(error), parent=self)
            return False
        return True

    def apply(self):
        self.result = tuple(entry.get().strip() for entry in self.entries)
