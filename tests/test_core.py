import asyncio
import unittest

from ayuclicker.core import Clicker, Message, Settings


class MemoryStore:
    def __init__(self):
        self.keys = set()

    def contains(self, key):
        return key in self.keys

    def claim(self, key):
        if key in self.keys:
            return False
        self.keys.add(key)
        return True


class FakeAdapter:
    def __init__(self, message):
        self.message = message
        self.clicked = []
        self.error = None

    async def fetch(self, key):
        return self.message

    async def click(self, message, title):
        self.clicked.append((message.key, title))
        if self.error:
            raise self.error


def msg(text="Получен ПОДАРОК!", buttons=("Забрать",), message_id=1):
    return Message(123, message_id, text, buttons)


class MatchingTests(unittest.TestCase):
    def test_whole_word_unicode_case_and_punctuation(self):
        settings = Settings("подарок", "забрать", 1, 3)
        self.assertTrue(settings.matches(msg()))
        self.assertFalse(settings.matches(msg("подарочный")))
        self.assertFalse(settings.matches(msg("нет совпадения")))
        self.assertFalse(settings.matches(msg(buttons=("Забрать всё",))))

    def test_substring_and_literal_special_characters(self):
        self.assertTrue(Settings("дар", "Забрать", 0, 0, False).matches(msg()))
        self.assertTrue(Settings("a+b", "OK", 0, 0).matches(msg(" a+b!", ("OK",))))
        self.assertFalse(Settings("a+b", "OK", 0, 0).matches(msg("aaab", ("OK",))))

    def test_invalid_settings(self):
        for low, high in [(-1, 2), (3, 2), (float("nan"), 1), (0, float("inf"))]:
            with self.subTest(low=low, high=high), self.assertRaises(ValueError):
                Settings("слово", "кнопка", low, high)
        with self.assertRaises(ValueError):
            Settings("  ", "кнопка", 0, 1)


class SchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter = FakeAdapter(msg())
        self.store = MemoryStore()
        self.gate = asyncio.Event()
        self.delays = []
        self.logs = []

        async def sleep(delay):
            self.delays.append(delay)
            await self.gate.wait()

        self.clicker = Clicker(
            Settings("подарок", "Забрать", 2, 5),
            self.adapter, self.store, self.logs.append,
            sleep=sleep, uniform=lambda low, high: (low + high) / 2,
        )

    async def asyncTearDown(self):
        await self.clicker.close()

    async def finish(self):
        tasks = list(self.clicker.pending.values())
        self.gate.set()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_delay_and_duplicate_notifications(self):
        self.clicker.observe(msg())
        self.clicker.observe(msg())
        await asyncio.sleep(0)
        self.assertEqual(self.delays, [3.5])
        self.assertEqual(self.adapter.clicked, [])
        await self.finish()
        self.clicker.observe(msg())
        await self.finish()
        self.assertEqual(self.adapter.clicked, [((123, 1), "Забрать")])

    async def test_wrong_word_or_button_never_scheduled(self):
        self.clicker.observe(msg("другой текст"))
        self.clicker.observe(msg(buttons=("Отмена",)))
        self.assertEqual(self.clicker.pending, {})

    async def test_refetch_cancels_changed_or_deleted_message(self):
        for latest in [None, msg("другой текст"), msg(buttons=())]:
            self.gate.clear()
            self.clicker.observe(msg())
            self.adapter.message = latest
            await self.finish()
        self.assertEqual(self.adapter.clicked, [])
        self.assertEqual(self.store.keys, set())

    async def test_edit_cancels_timer(self):
        self.clicker.observe(msg())
        task = self.clicker.pending[msg().key]
        await asyncio.sleep(0)
        self.clicker.observe(msg("слово удалено"))
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.clicker.pending, {})
        self.assertEqual(self.adapter.clicked, [])

    async def test_stop_prevents_click_and_future_scheduling(self):
        self.clicker.observe(msg())
        await asyncio.sleep(0)
        await self.clicker.close()
        self.gate.set()
        self.clicker.observe(msg(message_id=2))
        self.assertEqual(self.adapter.clicked, [])
        self.assertEqual(self.clicker.pending, {})

    async def test_ambiguous_network_error_is_not_retried(self):
        self.adapter.error = TimeoutError()
        self.clicker.observe(msg())
        await self.finish()
        self.clicker.observe(msg())
        await self.finish()
        self.assertEqual(len(self.adapter.clicked), 1)
        self.assertTrue(any("TimeoutError" in line for line in self.logs))


if __name__ == "__main__":
    unittest.main()
