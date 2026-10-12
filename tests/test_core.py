import asyncio
import unittest

from ayuclicker.core import Clicker, Message, Settings


class MemoryStore:
    def __init__(self):
        self.keys = set()
        self.outcomes = {}

    def contains(self, key):
        return key in self.keys

    def claim(self, key):
        if key in self.keys:
            return False
        self.keys.add(key)
        return True

    def complete(self, key, outcome):
        self.outcomes[key] = outcome


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

    def test_comma_separated_terms_match_any_one(self):
        settings = Settings(" BANCORP, Сбер, Альфа ", "Забрать", 0, 0)
        for text in ("Предложение BANCORP", "Найден СБЕР!", "Альфа: новое сообщение"):
            with self.subTest(text=text):
                self.assertTrue(settings.matches(msg(text)))
        self.assertFalse(settings.matches(msg("Другой банк")))
        self.assertFalse(settings.matches(msg("Сбербанк")))
        self.assertFalse(settings.matches(msg("BANCORP", buttons=("Другая кнопка",))))

    def test_phrases_and_regex_characters_are_literal_alternatives(self):
        settings = Settings(" a+b, новый подарок, foo|bar ", "Забрать", 0, 0)
        for text in ("Есть a+b!", "Получен новый подарок.", "Текст foo|bar"):
            self.assertTrue(settings.matches(msg(text)))
        for text in ("aaab", "foo", "подарок"):
            self.assertFalse(settings.matches(msg(text)))

    def test_empty_list_items_are_ignored_and_empty_list_is_rejected(self):
        settings = Settings(", , Сбер, ,", "Забрать", 0, 0)
        self.assertTrue(settings.matches(msg("Сбер")))
        self.assertFalse(settings.matches(msg("Нет подходящего слова")))
        with self.assertRaises(ValueError):
            Settings(", , ,", "Забрать", 0, 0)

    def test_substring_mode_applies_to_each_term(self):
        settings = Settings("bancorp, сбер", "Забрать", 0, 0, whole_word=False)
        self.assertTrue(settings.matches(msg("BANCORP123")))
        self.assertTrue(settings.matches(msg("Сбербанк")))
        self.assertFalse(settings.matches(msg("Другой банк")))

    def test_bank_and_bin_are_both_required_in_one_message(self):
        settings = Settings("Bancorp(514377)", "Забрать", 0, 0)
        for text in ("BANCORP\n/bin 514377", "BIN: 514377, банк Bancorp", "Bancorp (514377)"):
            with self.subTest(text=text):
                self.assertTrue(settings.matches(msg(text)))
        for text in ("Bancorp", "/bin 514377", "Bancorp /bin 514378", "Другой банк /bin 514377"):
            with self.subTest(text=text):
                self.assertFalse(settings.matches(msg(text)))
        self.assertFalse(settings.matches(msg("Bancorp", buttons=("514377", "Забрать"))))

    def test_plain_banks_still_work_without_bin_in_a_mixed_list(self):
        settings = Settings("Bancorp(514377), Сбер, Альфа", "Забрать", 0, 0)
        self.assertTrue(settings.matches(msg("Сбер")))
        self.assertTrue(settings.matches(msg("Альфа")))
        self.assertTrue(settings.matches(msg("Bancorp /bin 514377")))
        self.assertFalse(settings.matches(msg("Bancorp /bin 514378")))
        self.assertFalse(settings.matches(msg("Bancorp")))

    def test_bin_is_not_a_prefix_or_part_of_a_longer_number(self):
        for whole_word in (True, False):
            settings = Settings("Bancorp(514377)", "Забрать", 0, 0, whole_word)
            for number in ("5143770", "1514377", "15143770"):
                with self.subTest(whole_word=whole_word, number=number):
                    self.assertFalse(settings.matches(msg(f"Bancorp /bin {number}")))

    def test_multiple_bin_rules_remain_separate_conditions(self):
        settings = Settings("Bancorp(514377), Другой банк(123456)", "Забрать", 0, 0)
        self.assertTrue(settings.matches(msg("Другой банк /bin 123456")))
        self.assertFalse(settings.matches(msg("Bancorp /bin 123456")))
        self.assertFalse(settings.matches(msg("Другой банк /bin 514377")))

    def test_bin_condition_spaces_and_name_matching_mode(self):
        settings = Settings(" Bancorp ( 514377 ), Сбер ", "Забрать", 0, 0)
        self.assertTrue(settings.matches(msg("bancorp: /bin 514377")))
        self.assertFalse(settings.matches(msg("BancorpX /bin 514377")))
        self.assertTrue(Settings("Bancorp(514377)", "Забрать", 0, 0, False).matches(msg("BancorpX /bin 514377")))

    def test_malformed_bin_conditions_are_rejected(self):
        for value in ("Bancorp()", "(514377)", "Bancorp(51O377)", "Bancorp(514377", "Bancorp514377)",
                      "Bancorp(514377,123456)"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings(value, "Забрать", 0, 0)


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

    async def test_changed_message_can_match_another_list_term(self):
        self.clicker.settings = Settings("подарок, бонус", "Забрать", 2, 5)
        self.clicker.observe(msg("Получен подарок"))
        latest = msg("Получен бонус")
        self.adapter.message = latest
        self.clicker.observe(latest)
        await self.finish()
        self.assertEqual(self.adapter.clicked, [((123, 1), "Забрать")])

    async def test_bin_is_rechecked_before_a_scheduled_click(self):
        self.clicker.settings = Settings("Bancorp(514377)", "Забрать", 2, 5)
        matching = msg("Bancorp /bin 514377")
        self.clicker.observe(matching)
        self.adapter.message = msg("Bancorp /bin 514378")
        await self.finish()
        self.assertEqual(self.adapter.clicked, [])
        self.assertEqual(self.store.keys, set())

    async def test_messages_for_different_terms_each_click_once(self):
        messages = [msg("BANCORP", message_id=10), msg("Сбер", message_id=11), msg("Другое", message_id=12)]

        class Adapter(FakeAdapter):
            async def fetch(self, key):
                return next((message for message in messages if message.key == key), None)

        adapter = Adapter(None)
        clicker = Clicker(Settings("BANCORP, Сбер", "Забрать", 0, 0), adapter, MemoryStore(), self.logs.append)
        try:
            for message in messages:
                clicker.observe(message)
                clicker.observe(message)
            await asyncio.gather(*list(clicker.pending.values()))
            self.assertEqual(sorted(adapter.clicked), [((123, 10), "Забрать"), ((123, 11), "Забрать")])
        finally:
            await clicker.close()


if __name__ == "__main__":
    unittest.main()
