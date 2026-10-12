import asyncio
import os
from pathlib import Path
import tempfile
import threading
import tkinter as tk
import unittest
from unittest.mock import patch

from ayuclicker.gui import App, Worker
from ayuclicker.storage import load_config


class WorkerTests(unittest.TestCase):
    def test_stop_cancels_session_and_waits_for_cleanup(self):
        entered = threading.Event()
        cleaned = threading.Event()
        received = []

        async def fake_session(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        with patch("ayuclicker.gui.run_session", fake_session):
            worker = Worker(lambda *event: received.append(event), Path("unused"), 1, "unused", "", None)
            worker.start()
            self.assertTrue(entered.wait(timeout=2))
            worker.stop()
            worker.thread.join(timeout=2)
            self.assertFalse(worker.thread.is_alive())
            self.assertTrue(cleaned.is_set())
            self.assertEqual(received[-1], ("finished",))

    def test_cancel_login_finishes_without_exposing_input(self):
        received = []

        async def fake_session(*args):
            await args[-1]("Номер телефона:", False)

        def emit(*event):
            received.append(event)
            if event[0] == "prompt":
                event[-1].set_result(None)

        with patch("ayuclicker.gui.run_session", fake_session):
            worker = Worker(emit, Path("unused"), 1, "unused", "", None)
            worker.start()
            worker.thread.join(timeout=2)
            self.assertFalse(worker.thread.is_alive())
            self.assertIn(("log", "Вход отменён."), received)
            self.assertEqual(received[-1], ("finished",))


@unittest.skipUnless(os.environ.get("DISPLAY") or os.name == "nt", "GUI requires a display")
class GuiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.app = App(self.root, directory=Path(self.temporary.name))
        self.root.update()

    def tearDown(self):
        for identifier in self.root.tk.call("after", "info"):
            self.root.after_cancel(identifier)
        self.root.destroy()
        self.temporary.cleanup()

    def test_start_serializes_controls_without_saving_hash_by_default(self):
        values = {
            "api_id": "123", "api_hash": "a" * 32, "chat": "@test_bot",
            "word": "подарок", "button": "Забрать", "minimum": "2,5", "maximum": "7",
        }
        for name, value in values.items():
            self.app.variables[name].set(value)
        with patch("ayuclicker.gui.Worker") as constructor:
            self.app.start()
            settings = constructor.call_args.args[-1]
            self.assertEqual((settings.minimum, settings.maximum), (2.5, 7))
            self.assertEqual(str(self.app.start_button.cget("state")), "disabled")
            self.app.stop()
            constructor.return_value.stop.assert_called_once()
        config = load_config(Path(self.temporary.name))
        self.assertEqual(config["api_hash"], "")
        self.assertEqual(config["word"], "подарок")
        self.app.emit("finished")
        self.app.poll()
        self.assertEqual(str(self.app.start_button.cget("state")), "normal")

    def test_load_private_chat_preserves_id_and_logs(self):
        self.app.variables["chat"].set("-100123")
        self.app.emit("dialogs", [("Бот", 45), ("Приватная группа", -100123)])
        self.app.emit("log", "Проверка журнала")
        self.app.poll()
        self.assertEqual(self.app.variables["chat"].get(), "Приватная группа [-100123]")
        self.assertEqual(self.app.chat_ids[self.app.variables["chat"].get()], -100123)
        self.assertIn("Проверка журнала", self.app.log_widget.get("1.0", "end"))

    def test_invalid_range_does_not_start_worker(self):
        values = {"api_id": "123", "api_hash": "a" * 32, "chat": "@test_bot",
                  "word": "подарок", "button": "Забрать", "minimum": "5", "maximum": "2"}
        for name, value in values.items():
            self.app.variables[name].set(value)
        with patch("ayuclicker.gui.Worker") as constructor, patch("ayuclicker.gui.messagebox.showerror") as error:
            self.app.start()
            constructor.assert_not_called()
            error.assert_called_once()

    def test_api_paste_buttons_replace_old_values_and_pass_validation(self):
        for name, old, pasted in (("api_id", "999", "123"), ("api_hash", "incorrect", "a" * 32)):
            self.app.variables[name].set(old)
            self.root.clipboard_clear()
            self.root.clipboard_append(pasted)
            self.app.paste_buttons[name].invoke()
            self.assertEqual(self.app.variables[name].get(), pasted)
        with patch("ayuclicker.gui.Worker") as constructor, patch("ayuclicker.gui.messagebox.showerror") as error:
            self.app.start(list_only=True)
            error.assert_not_called()
            self.assertEqual(constructor.call_args.args[2:4], (123, "a" * 32))

    def test_invalid_hash_message_reports_length_without_exposing_hash(self):
        self.app.variables["api_id"].set("123")
        self.app.variables["api_hash"].set("b" * 31)
        with patch("ayuclicker.gui.Worker") as constructor, patch("ayuclicker.gui.messagebox.showerror") as error:
            self.app.start(list_only=True)
            constructor.assert_not_called()
            report = error.call_args.args[1]
            self.assertIn("31", report)
            self.assertIn("Вставить", report)
            self.assertNotIn("b" * 31, report)
