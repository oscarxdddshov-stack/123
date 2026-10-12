import asyncio
import unittest

from ayuclicker.core import Clicker, ClickResponse, Message, RetryLater, Settings
from tests.test_core import MemoryStore


def repeat_settings(**overrides):
    values = dict(word="подарок", button="Забрать", minimum=1, maximum=3,
                  repeat_until_result=True, mine_phrases="Получено вами", other_phrases="Уже получил другой")
    values.update(overrides)
    return Settings(**values)


def original(chat_id=123, message_id=1):
    return Message(chat_id, message_id, "Новый подарок", ("Забрать",), sender_id=77)


class RepeatRulesTests(unittest.TestCase):
    def test_outcomes_are_distinct_case_insensitive_and_allow_alternatives(self):
        settings = repeat_settings(mine_phrases="Получено вами; Вы успешно забрали")
        self.assertEqual(settings.outcome("ВЫ УСПЕШНО\nЗАБРАЛИ!"), "mine")
        self.assertEqual(settings.outcome("Уже получил другой пользователь"), "other")
        self.assertIsNone(settings.outcome("Запрос принят, попробуйте позже"))

    def test_other_marker_takes_precedence_over_broader_success_marker(self):
        settings = repeat_settings(mine_phrases="получил", other_phrases="получил другой")
        self.assertEqual(settings.outcome("Это получил другой"), "other")

    def test_repeat_requires_both_markers_and_positive_delay(self):
        for values in ({"mine_phrases": ""}, {"other_phrases": ""}, {"minimum": 0},
                       {"mine_phrases": "ГОТОВО", "other_phrases": "готово"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                repeat_settings(**values)


class RepeatAdapter:
    def __init__(self, replies):
        self.messages = {original().key: original()}
        self.replies = list(replies)
        self.clicks = []

    async def fetch(self, key):
        return self.messages.get(key)

    async def click(self, message, title):
        self.clicks.append(message.key)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return ClickResponse(reply)


class RepeatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.logs = []
        self.delays = []
        self.store = MemoryStore()
        self.clickers = []

    async def asyncTearDown(self):
        for clicker in self.clickers:
            await clicker.close()

    def make(self, adapter, *, sleep=None):
        async def fast_sleep(delay):
            self.delays.append(delay)
            await asyncio.sleep(0)

        clicker = Clicker(repeat_settings(), adapter, self.store, self.logs.append,
                          sleep=sleep or fast_sleep, uniform=lambda low, high: 2)
        self.clickers.append(clicker)
        return clicker

    async def finish(self, clicker):
        await asyncio.gather(*list(clicker.pending.values()), return_exceptions=True)

    async def test_retries_until_successful_callback_answer(self):
        adapter = RepeatAdapter(["Попробуйте позже", "Нет подтверждения", "Получено вами"])
        clicker = self.make(adapter)
        clicker.observe(original())
        clicker.observe(original())
        await self.finish(clicker)
        self.assertEqual(adapter.clicks, [original().key] * 3)
        self.assertEqual(self.delays, [2, 2, 2])
        self.assertEqual(self.store.outcomes, {original().key: "mine"})
        clicker.observe(original())
        self.assertEqual(clicker.pending, {})

    async def test_another_user_confirmation_stops_immediately(self):
        adapter = RepeatAdapter(["Уже получил другой"])
        clicker = self.make(adapter)
        clicker.observe(original())
        await self.finish(clicker)
        self.assertEqual(len(adapter.clicks), 1)
        self.assertEqual(self.store.outcomes[original().key], "other")

    async def test_confirmed_edit_stops_a_pending_timer_without_another_click(self):
        gate = asyncio.Event()

        async def sleep(delay):
            await gate.wait()

        adapter = RepeatAdapter([])
        clicker = self.make(adapter, sleep=sleep)
        clicker.observe(original())
        tasks = list(clicker.pending.values())
        await asyncio.sleep(0)
        clicker.observe(Message(123, 1, "Уже получил другой", (), sender_id=77))
        await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(adapter.clicks, [])
        self.assertEqual(self.store.outcomes[original().key], "other")

    async def test_fetch_detects_terminal_result_even_when_button_disappeared(self):
        adapter = RepeatAdapter([])
        adapter.messages[original().key] = Message(123, 1, "Получено вами", ())
        clicker = self.make(adapter)
        clicker.observe(original())
        await self.finish(clicker)
        self.assertEqual(adapter.clicks, [])
        self.assertEqual(self.store.outcomes[original().key], "mine")

    async def test_missing_button_or_deleted_message_is_not_confirmation(self):
        for latest in (None, Message(123, 1, "Новый подарок", ())):
            adapter = RepeatAdapter([])
            adapter.messages[original().key] = latest
            clicker = self.make(adapter)
            clicker.observe(original())
            await self.finish(clicker)
            self.assertEqual(adapter.clicks, [])
            self.assertEqual(self.store.outcomes, {})
        self.assertTrue(any("не подтверждён" in line for line in self.logs))

    async def test_transient_error_and_telegram_pause_are_respected(self):
        adapter = RepeatAdapter([RetryLater("TimeoutError"), RetryLater("FloodWaitError", 8), "Получено вами"])
        clicker = self.make(adapter)
        clicker.observe(original())
        await self.finish(clicker)
        self.assertEqual(self.delays, [2, 2, 8])
        self.assertEqual(len(adapter.clicks), 3)
        self.assertEqual(self.store.outcomes[original().key], "mine")

    async def test_stop_cancels_retry_after_an_unconfirmed_attempt(self):
        gate = asyncio.Event()
        waiting = asyncio.Event()
        calls = 0

        async def sleep(delay):
            nonlocal calls
            calls += 1
            if calls >= 2:
                waiting.set()
                await gate.wait()

        adapter = RepeatAdapter(["Пока не готово"])
        clicker = self.make(adapter, sleep=sleep)
        clicker.observe(original())
        await asyncio.wait_for(waiting.wait(), timeout=2)
        await clicker.close()
        gate.set()
        self.assertEqual(len(adapter.clicks), 1)
        self.assertEqual(clicker.pending, {})
        self.assertEqual(self.store.outcomes, {})

    async def test_intermediate_button_removal_waits_for_later_confirmation(self):
        adapter = RepeatAdapter(["В обработке"])
        calls = 0

        async def sleep(delay):
            nonlocal calls
            calls += 1
            if calls == 2:
                adapter.messages[original().key] = Message(123, 1, "Обрабатываем", ())
            elif calls == 3:
                adapter.messages[original().key] = Message(123, 1, "Получено вами", ())
            await asyncio.sleep(0)

        clicker = self.make(adapter, sleep=sleep)
        clicker.observe(original())
        await self.finish(clicker)
        self.assertEqual(len(adapter.clicks), 1)
        self.assertEqual(calls, 3)
        self.assertEqual(self.store.outcomes[original().key], "mine")

    async def test_reply_must_refer_to_this_message_chat_and_sender(self):
        gate = asyncio.Event()

        async def sleep(delay):
            await gate.wait()

        clicker = self.make(RepeatAdapter([]), sleep=sleep)
        clicker.observe(original())
        for reply in (
            Message(456, 10, "Получено вами", (), 77, 1),
            Message(123, 10, "Получено вами", (), 88, 1),
            Message(123, 10, "Получено вами", (), 77, 2),
            Message(123, 10, "Получено вами", (), 77, None),
        ):
            self.assertFalse(clicker.observe_reply(reply))
        tasks = list(clicker.pending.values())
        self.assertTrue(clicker.observe_reply(Message(123, 10, "Получено вами", (), 77, 1)))
        await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(self.store.outcomes[original().key], "mine")

    async def test_same_message_ids_in_two_chats_have_independent_results(self):
        class Adapter:
            def __init__(self):
                self.clicks = []

            async def fetch(self, key):
                return original(*key)

            async def click(self, message, title):
                self.clicks.append(message.key)
                return ClickResponse("Получено вами" if message.chat_id == 123 else "Уже получил другой")

        adapter = Adapter()
        clicker = self.make(adapter)
        clicker.observe(original(123, 1))
        clicker.observe(original(456, 1))
        await self.finish(clicker)
        self.assertEqual(set(adapter.clicks), {(123, 1), (456, 1)})
        self.assertEqual(self.store.outcomes, {(123, 1): "mine", (456, 1): "other"})
