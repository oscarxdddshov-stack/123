import os
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from ayuclicker.core import Settings
from ayuclicker.screen_core import Reading, Rect, ScreenSchedule
from ayuclicker.screen_gui import ScreenApp, ScreenWorker
from ayuclicker.windows_screen import Target


class ScreenScheduleTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings("подарок", "Забрать", 2, 6)
        self.seen = set()
        self.schedule = ScreenSchedule(self.settings, self.seen, uniform=lambda low, high: 4)
        self.reading = Reading("Получен ПОДАРОК!", "Забрать")

    def scheduled(self):
        self.assertEqual(self.schedule.observe(self.reading, 10).kind, "stabilizing")
        decision = self.schedule.observe(self.reading, 11)
        self.assertEqual((decision.kind, decision.delay), ("scheduled", 4))

    def test_two_reads_random_delay_and_one_attempt(self):
        self.scheduled()
        self.assertFalse(self.schedule.claim(self.reading))
        self.assertEqual(self.schedule.observe(self.reading, 14.9).kind, "waiting")
        self.assertEqual(self.schedule.observe(self.reading, 15).kind, "ready")
        self.assertTrue(self.schedule.claim(self.reading))
        self.assertFalse(self.schedule.claim(self.reading))
        self.assertEqual(self.schedule.observe(self.reading, 100).kind, "idle")

    def test_word_in_button_alone_does_not_match_message(self):
        schedule = ScreenSchedule(Settings("подарок", "подарок", 0, 0), set())
        self.assertEqual(schedule.observe(Reading("Другой текст", "подарок"), 1).kind, "idle")
        self.assertEqual(self.schedule.observe(Reading("Другой текст", "Забрать"), 2).kind, "idle")

    def test_partial_button_title_does_not_match(self):
        self.assertEqual(self.schedule.observe(Reading("подарок", "Забрать всё"), 1).kind, "idle")

    def test_changed_or_missing_text_cancels_pending(self):
        for latest in (None, Reading("Слово удалено", "Забрать"), Reading("подарок", "Отмена")):
            self.scheduled()
            self.assertEqual(self.schedule.observe(latest, 20).kind, "cancelled")
            self.assertFalse(self.schedule.claim(self.reading))

    def test_new_matching_text_requires_new_stability_and_timer(self):
        self.scheduled()
        new = Reading("Новый подарок", "Забрать")
        self.assertEqual(self.schedule.observe(new, 14).kind, "cancelled")
        self.assertEqual(self.schedule.observe(new, 15).kind, "scheduled")
        self.assertEqual(self.schedule.observe(new, 18).kind, "waiting")
        self.assertEqual(self.schedule.observe(new, 19).kind, "ready")

    def test_click_latch_requires_two_absent_reads_before_another_text(self):
        self.scheduled()
        self.schedule.observe(self.reading, 15)
        self.assertTrue(self.schedule.claim(self.reading))
        changed = Reading("Получен подарок, осталось 8 секунд", "Забрать")
        self.assertEqual(self.schedule.observe(changed, 20).kind, "idle")
        absent = Reading("Нет нужного слова", "Забрать")
        self.schedule.observe(absent, 21)
        self.schedule.observe(changed, 22)  # One OCR failure cannot rearm it.
        self.assertTrue(self.schedule.locked)
        self.schedule.observe(absent, 23)
        self.schedule.observe(absent, 24)
        self.assertFalse(self.schedule.locked)
        self.assertEqual(self.schedule.observe(changed, 25).kind, "stabilizing")

    def test_focus_loss_does_not_rearm_after_a_click(self):
        self.scheduled()
        self.schedule.observe(self.reading, 15)
        self.schedule.claim(self.reading)
        self.schedule.observe(None, 20)
        self.schedule.observe(None, 21)
        self.assertTrue(self.schedule.locked)

    def test_identical_text_stays_seen_in_new_schedule(self):
        self.scheduled()
        self.schedule.observe(self.reading, 15)
        self.schedule.claim(self.reading)
        another = ScreenSchedule(self.settings, self.seen)
        equivalent = Reading("Получен\n ПОДАРОК!", "забрать")
        self.assertEqual(another.observe(equivalent, 100).kind, "idle")


class RegionTests(unittest.TestCase):
    def test_containment_intersection_and_offsets(self):
        outer = Rect(-20, 10, 200, 100)
        text = Rect(0, 20, 150, 30)
        button = Rect(0, 60, 100, 20)
        self.assertTrue(text.valid())
        self.assertTrue(text.inside(outer))
        self.assertFalse(text.intersects(button))
        self.assertTrue(text.intersects(Rect(10, 25, 30, 20)))
        self.assertEqual(button.offset(5, -10), Rect(5, 50, 100, 20))
        self.assertFalse(Rect(0, 0, 5, 20).valid())


class FakeDesktop:
    def __init__(self):
        self.clicks = []
        self.focus = True
        self.focus_checks = 0

    def is_ready(self, target):
        self.focus_checks += 1
        return self.focus

    def bounds(self, handle):
        return Rect(0, 0, 700, 500)

    def regions(self, target, text, button):
        return text, button

    def click(self, target, button, stop):
        if stop.is_set() or not self.focus:
            return False
        self.clicks.append(button)
        stop.set()
        return True


class ScreenWorkerTests(unittest.TestCase):
    def make_worker(self, reading, *, stop_during_ocr=False, lose_focus=False):
        desktop = FakeDesktop()
        events = []
        instances = []

        class FakeOcr:
            def __init__(self, language, stop):
                self.stop = stop
                self.info = {"language": language}
                self.reads = 0
                self.closed = False
                instances.append(self)

            def recognize(self, text, button):
                self.reads += 1
                if stop_during_ocr or self.reads >= 4:
                    self.stop.set()
                if lose_focus and self.reads == 3:
                    desktop.focus = False
                return reading

            def close(self):
                self.closed = True

        def emit(*event):
            events.append(event)
            if lose_focus and event == ("status", "Ожидание окна AyuGram"):
                worker.stop()

        worker = ScreenWorker(
            desktop, Target(1, 2, 700, 500, "AyuGram"), Rect(0, 0, 100, 40), Rect(0, 50, 100, 30),
            Settings("подарок", "Забрать", 0, 0), "ru-RU", set(), emit,
            ocr_factory=FakeOcr, poll_interval=0,
        )
        return worker, desktop, events, instances

    def test_worker_rechecks_and_clicks_only_after_matching_reads(self):
        worker, desktop, events, instances = self.make_worker(Reading("подарок", "Забрать"))
        worker.start()
        worker.thread.join(timeout=2)
        self.assertFalse(worker.thread.is_alive())
        self.assertEqual(len(desktop.clicks), 1)
        self.assertEqual(instances[0].reads, 3)
        self.assertTrue(instances[0].closed)
        self.assertEqual(events[-1], ("finished",))

    def test_nonmatching_message_never_clicks(self):
        worker, desktop, events, instances = self.make_worker(Reading("Другой текст", "Забрать"))
        worker.start()
        worker.thread.join(timeout=2)
        self.assertEqual(desktop.clicks, [])
        self.assertTrue(instances[0].closed)

    def test_stop_or_focus_loss_during_ocr_prevents_click(self):
        for mode in ({"stop_during_ocr": True}, {"lose_focus": True}):
            worker, desktop, events, instances = self.make_worker(Reading("подарок", "Забрать"), **mode)
            worker.start()
            worker.thread.join(timeout=2)
            self.assertFalse(worker.thread.is_alive())
            self.assertEqual(desktop.clicks, [])
            self.assertTrue(instances[0].closed)


@unittest.skipUnless(os.environ.get("DISPLAY") or os.name == "nt", "GUI requires a display")
class ScreenGuiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.desktop = FakeDesktop()
        self.app = ScreenApp(self.root, self.desktop, directory=Path(self.temporary.name), register_hotkey=False)
        self.root.update()

    def tearDown(self):
        for identifier in self.root.tk.call("after", "info"):
            self.root.after_cancel(identifier)
        self.root.destroy()
        self.temporary.cleanup()

    def configure(self):
        self.app.target = Target(1, 2, 700, 500, "AyuGram")
        self.app.text_region = Rect(0, 0, 100, 40)
        self.app.button_region = Rect(0, 50, 100, 30)
        for key, value in {"word": "подарок", "button": "Забрать", "minimum": "2,5", "maximum": "5"}.items():
            self.app.values[key].set(value)

    def test_no_api_fields_and_start_stop(self):
        self.assertNotIn("api_id", self.app.values)
        self.assertNotIn("api_hash", self.app.values)
        self.configure()
        with patch("ayuclicker.screen_gui.ScreenWorker") as constructor:
            self.app.start()
            settings = constructor.call_args.args[4]
            self.assertEqual(settings.minimum, 2.5)
            self.assertEqual(str(self.app.start_button.cget("state")), "disabled")
            self.app.emit("hotkey")
            constructor.return_value.stop.assert_called_once()
            self.app.poll()
            self.app.emit("finished")
            self.app.poll()
            self.assertEqual(str(self.app.start_button.cget("state")), "normal")

    def test_missing_regions_prevent_start(self):
        with patch("ayuclicker.screen_gui.ScreenWorker") as constructor, patch("ayuclicker.screen_gui.messagebox.showerror") as error:
            self.app.start()
            constructor.assert_not_called()
            error.assert_called_once()

    def test_preview_does_not_start_observation_or_click(self):
        with patch("ayuclicker.screen_gui.messagebox.showinfo") as information:
            self.app.emit("preview", Reading("подарок", "Забрать"))
            self.app.poll()
            information.assert_called_once()
        self.assertIsNone(self.app.worker)
        self.assertEqual(self.desktop.clicks, [])
