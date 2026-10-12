import os
from types import SimpleNamespace
import tkinter as tk
from tkinter import ttk
import unittest

from ayuclicker.editing import LoginDialog, install_editing, paste_from_clipboard, shortcut_action


class ShortcutTests(unittest.TestCase):
    def test_windows_virtual_key_works_independently_of_layout(self):
        for code, action in ((65, "all"), (67, "copy"), (86, "paste"), (88, "cut")):
            event = SimpleNamespace(state=4, keycode=code, keysym="unknown")
            self.assertEqual(shortcut_action(event, windows=True), action)

    def test_russian_keysyms_and_altgr(self):
        for symbol, action in (("Cyrillic_em", "paste"), ("с", "copy"), ("ф", "all"), ("Cyrillic_che", "cut")):
            event = SimpleNamespace(state=4, keycode=0, keysym=symbol)
            self.assertEqual(shortcut_action(event, windows=False), action)
        self.assertIsNone(shortcut_action(SimpleNamespace(state=12, keycode=86, keysym="v"), windows=True))


@unittest.skipUnless(os.environ.get("DISPLAY") or os.name == "nt", "GUI requires a display")
class ClipboardTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.entry = ttk.Entry(self.root)
        self.entry.pack()
        self.editor = install_editing(self.entry)
        self.root.update()

    def tearDown(self):
        for identifier in self.root.tk.call("after", "info"):
            self.root.after_cancel(identifier)
        self.root.destroy()

    def clipboard(self, value):
        self.root.clipboard_clear()
        self.root.clipboard_append(value)

    def test_paste_replaces_selected_text_and_preserves_rest(self):
        self.entry.insert(0, "old value end")
        self.entry.selection_range(4, 9)
        self.clipboard("новое")
        self.assertTrue(paste_from_clipboard(self.entry))
        self.assertEqual(self.entry.get(), "old новое end")

    def test_replace_all_for_masked_credential(self):
        self.entry.configure(show="•")
        self.entry.insert(0, "incorrect")
        self.clipboard("a" * 32)
        self.assertTrue(paste_from_clipboard(self.entry, replace_all=True))
        self.assertEqual(self.entry.get(), "a" * 32)
        self.assertEqual(self.entry.cget("show"), "•")

    def test_ctrl_v_binding_inserts_once(self):
        self.entry.insert(0, "old")
        self.entry.selection_range(0, "end")
        self.clipboard("pasted")
        self.entry.focus_force()
        self.root.update()
        self.entry.event_generate("<Control-KeyPress-v>")
        self.root.update()
        self.assertEqual(self.entry.get(), "pasted")

    def test_disabled_and_readonly_fields_are_not_modified(self):
        self.entry.insert(0, "original")
        self.clipboard("new")
        for state in ("disabled", "readonly"):
            self.entry.configure(state=state)
            self.assertFalse(paste_from_clipboard(self.entry, replace_all=True))
            self.assertEqual(self.entry.get(), "original")

    def test_empty_clipboard_keeps_current_value(self):
        self.entry.insert(0, "original")
        self.root.clipboard_clear()
        self.assertFalse(paste_from_clipboard(self.entry, replace_all=True))
        self.assertEqual(self.entry.get(), "original")

    def test_login_dialog_can_paste_and_returns_the_input(self):
        self.clipboard("12345")

        def complete():
            for child in self.root.winfo_children():
                if isinstance(child, LoginDialog):
                    paste_from_clipboard(child.entry, replace_all=True)
                    child.ok()
                    return
            self.root.after(10, complete)

        def safety_cancel():
            for child in self.root.winfo_children():
                if isinstance(child, LoginDialog):
                    child.cancel()

        self.root.after(50, complete)
        self.root.after(2000, safety_cancel)
        dialog = LoginDialog(self.root, "Код входа:", secret=True)
        self.assertEqual(dialog.result, "12345")
