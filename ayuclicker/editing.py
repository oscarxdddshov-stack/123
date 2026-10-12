"""Explicit clipboard editing, including shortcuts on Russian Windows layouts."""

import sys
import tkinter as tk
from tkinter import simpledialog, ttk


def shortcut_action(event, *, windows=None):
    if windows is None:
        windows = sys.platform == "win32"
    # Do not intercept AltGr / Ctrl+Alt typing.
    if event.state & 0x0008:
        return None
    if windows:
        physical = {65: "all", 67: "copy", 86: "paste", 88: "cut"}
        if event.keycode in physical:
            return physical[event.keycode]
    return {
        "a": "all", "c": "copy", "v": "paste", "x": "cut",
        "cyrillic_ef": "all", "ф": "all",
        "cyrillic_es": "copy", "с": "copy",
        "cyrillic_em": "paste", "м": "paste",
        "cyrillic_che": "cut", "ч": "cut",
    }.get(event.keysym.casefold())


def is_editable(widget):
    if str(widget.cget("state")) in ("disabled", "readonly"):
        return False
    return not (hasattr(widget, "instate") and (
        widget.instate(("disabled",)) or widget.instate(("readonly",))
    ))


def selection_present(widget):
    return bool(widget.tag_ranges("sel")) if isinstance(widget, tk.Text) else widget.selection_present()


def paste_from_clipboard(widget, *, replace_all=False):
    if not is_editable(widget):
        return False
    try:
        value = widget.clipboard_get()
    except tk.TclError:
        widget.bell()
        return False
    if not value:
        return False
    if isinstance(widget, tk.Text):
        if replace_all:
            widget.delete("1.0", "end")
            widget.mark_set("insert", "1.0")
        elif selection_present(widget):
            first = widget.index("sel.first")
            widget.delete("sel.first", "sel.last")
            widget.mark_set("insert", first)
        widget.insert("insert", value)
        widget.tag_remove("sel", "1.0", "end")
        widget.see("insert")
        widget.focus_set()
        return True
    if replace_all:
        widget.delete(0, "end")
        position = 0
    elif widget.selection_present():
        position = widget.index("sel.first")
        widget.delete("sel.first", "sel.last")
    else:
        position = widget.index("insert")
    widget.insert(position, value)
    widget.selection_clear()
    widget.icursor(position + len(value))
    widget.xview("insert")
    widget.focus_set()
    return True


class EntryEditing:
    def __init__(self, widget):
        self.widget = widget
        self.menu = tk.Menu(widget, tearoff=False)
        for label, action in (("Вырезать", "cut"), ("Копировать", "copy"), ("Вставить", "paste")):
            self.menu.add_command(label=label, command=lambda action=action: self.action(action))
        self.menu.add_separator()
        self.menu.add_command(label="Выделить всё", command=lambda: self.action("all"))
        widget.bind("<Button-3>", self.popup)
        widget.bind("<Control-KeyPress>", self.keypress)
        widget.bind("<Shift-Insert>", lambda event: self.action("paste"))
        widget.bind("<Control-Insert>", lambda event: self.action("copy"))
        widget.bind("<Shift-Delete>", lambda event: self.action("cut"))

    def action(self, action):
        if action == "paste":
            paste_from_clipboard(self.widget)
        elif action == "all":
            if isinstance(self.widget, tk.Text):
                self.widget.tag_add("sel", "1.0", "end-1c")
                self.widget.mark_set("insert", "end-1c")
            else:
                self.widget.selection_range(0, "end")
                self.widget.icursor("end")
        elif action == "copy":
            self.widget.event_generate("<<Copy>>")
        elif action == "cut" and is_editable(self.widget):
            self.widget.event_generate("<<Cut>>")
        return "break"

    def keypress(self, event):
        action = shortcut_action(event)
        if action is not None:
            return self.action(action)

    def popup(self, event):
        selected = selection_present(self.widget)
        editable = is_editable(self.widget)
        self.menu.entryconfigure(0, state="normal" if selected and editable else "disabled")
        self.menu.entryconfigure(1, state="normal" if selected else "disabled")
        self.menu.entryconfigure(2, state="normal" if editable else "disabled")
        self.widget.focus_set()
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()
        return "break"


def install_editing(widget):
    editor = EntryEditing(widget)
    widget._ayuclicker_editor = editor
    return editor


class LoginDialog(simpledialog.Dialog):
    def __init__(self, parent, text, secret=False):
        self.text = text
        self.secret = secret
        super().__init__(parent, title="Вход в Telegram")

    def body(self, master):
        ttk.Label(master, text=self.text, wraplength=420).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.entry = ttk.Entry(master, width=40, show="•" if self.secret else "")
        self.entry.grid(row=1, column=0, sticky="ew")
        install_editing(self.entry)
        ttk.Button(master, text="Вставить", command=lambda: paste_from_clipboard(self.entry, replace_all=True)).grid(
            row=1, column=1, padx=(8, 0))
        return self.entry

    def apply(self):
        self.result = self.entry.get()
