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
        self.assertEqual(self.app.variables["chat"].get(), "-100123")
        self.assertEqual(self.app.chat_box.get(), "Приватная группа [-100123]")
        self.assertEqual(self.app.chat_ids[self.app.chat_box.get()], -100123)
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

    def test_multiple_chat_ids_are_passed_and_saved_without_replacement(self):
        ids = "-1001111111111\n-1002222222222, -1003333333333"
        for name, value in {"api_id": "123", "api_hash": "a" * 32, "chat": ids,
                            "word": "подарок", "button": "Забрать"}.items():
            self.app.variables[name].set(value)
        with patch("ayuclicker.gui.Worker") as constructor:
            self.app.start()
            self.assertEqual(constructor.call_args.args[4], ids)
        self.assertEqual(load_config(Path(self.temporary.name))["chat"], ids)
        self.assertEqual(self.app.chat_text.get("1.0", "end-1c"), ids)

    def test_chat_picker_appends_selected_id_and_ignores_duplicates(self):
        self.app.variables["chat"].set("-1001111111111")
        self.app.emit("dialogs", [("Группа", -1002222222222)])
        self.app.poll()
        self.app.add_chat_button.invoke()
        self.app.add_chat_button.invoke()
        self.assertEqual(self.app.variables["chat"].get(), "-1001111111111\n-1002222222222")

    def test_loading_dialogs_preserves_all_selected_ids(self):
        ids = "-1001111111111, -1002222222222"
        self.app.variables["chat"].set(ids)
        self.app.emit("dialogs", [("Группа", -1002222222222)])
        self.app.poll()
        self.assertEqual(self.app.variables["chat"].get(), ids)
        self.app.emit("watching", 2)
        self.app.poll()
        self.assertEqual(self.app.status.get(), "Работает: чатов 2")

    def test_result_configuration_enables_repeat_and_is_passed_to_worker(self):
        with patch("ayuclicker.gui.ResultDialog") as dialog:
            dialog.return_value.result = ("Получено вами", "Уже получил другой", "")
            self.app.configure_results()
        self.assertTrue(self.app.repeat_until_result.get())
        for name, value in {"api_id": "123", "api_hash": "a" * 32, "chat": "42",
                            "word": "подарок", "button": "Забрать"}.items():
            self.app.variables[name].set(value)
        with patch("ayuclicker.gui.Worker") as constructor:
            self.app.start()
            settings = constructor.call_args.args[-1]
            self.assertTrue(settings.repeat_until_result)
            self.assertEqual(settings.outcome("Получено вами"), "mine")
        config = load_config(Path(self.temporary.name))
        self.assertTrue(config["repeat_until_result"])
        self.assertEqual(config["other_phrases"], "Уже получил другой")

    def test_repeat_without_confirmation_phrases_cannot_start(self):
        self.app.repeat_until_result.set(True)
        for name, value in {"api_id": "123", "api_hash": "a" * 32, "chat": "42",
                            "word": "подарок", "button": "Забрать"}.items():
            self.app.variables[name].set(value)
        with patch("ayuclicker.gui.Worker") as constructor, patch("ayuclicker.gui.messagebox.showerror") as error:
            self.app.start()
            constructor.assert_not_called()
            self.assertIn("Забрал я", error.call_args.args[1])

    def test_receipt_bot_configuration_allows_empty_mine_phrase_and_is_saved(self):
        self.app.chat_ids = {"Бот [99]": 99}
        with patch("ayuclicker.gui.ResultDialog") as dialog:
            dialog.return_value.result = ("", "уже взял заявку", "99")
            self.app.configure_results()
            self.assertEqual(dialog.call_args.args[-1], {"Бот [99]": 99})
        for name, value in {"api_id": "123", "api_hash": "a" * 32, "chat": "-100123",
                            "word": "подарок", "button": "Забрать"}.items():
            self.app.variables[name].set(value)
        with patch("ayuclicker.gui.Worker") as constructor:
            self.app.start()
            settings = constructor.call_args.args[-1]
            self.assertEqual(settings.success_chat, "99")
            self.assertEqual(settings.mine_markers, ())
        self.assertEqual(load_config(Path(self.temporary.name))["success_chat"], "99")

    def test_result_dialog_can_pick_bot_validate_and_save(self):
        from ayuclicker.result_dialog import ResultDialog

        def fill():
            dialog = next(child for child in self.root.winfo_children() if isinstance(child, ResultDialog))
            dialog.picker.set("Бот [99]")
            dialog.choose_bot()
            dialog.ok()

        self.root.after(50, fill)
        dialog = ResultDialog(self.root, "", "уже взял заявку", "", {"Бот [99]": 99})
        self.assertEqual(dialog.result, ("", "уже взял заявку", "99"))
