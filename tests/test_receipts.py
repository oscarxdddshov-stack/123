import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon import events, types, utils

from ayuclicker.core import Clicker, Message, Settings, request_tags
from ayuclicker.telegram import ConfigurationError, TelegramAdapter, project_message, run_session
from tests.test_core import MemoryStore
from tests.test_telegram import CHAT_A, MultiChatClient, raw_message


TAGS = "#AD773474352 [#SourDog]"


def settings(**changes):
    values = dict(word="подарок", button="Забрать", minimum=1, maximum=2,
                  repeat_until_result=True, other_phrases="уже взял заявку", success_chat="@receipt_bot")
    values.update(changes)
    return Settings(**values)


class TagTests(unittest.TestCase):
    def test_extracts_complete_trailing_pair_with_whitespace(self):
        self.assertEqual(request_tags("Текст\n" + TAGS + "\n"), ("ad773474352", "sourdog"))
        self.assertEqual(request_tags("#AD398493242 [ #ElatedPheasant ]"), ("ad398493242", "elatedpheasant"))
        self.assertEqual(request_tags("#AD587569977 [#ControlledEchidna]"), ("ad587569977", "controlledechidna"))

    def test_bin_partial_tags_and_non_trailing_pair_do_not_match(self):
        for value in ("/bin 514377", "#AD773474352", "[#SourDog]", "#AD773474352x [#SourDog]",
                      "#AD773474352 [#SourDog x]", TAGS + " ещё текст", "x" + TAGS):
            with self.subTest(value=value):
                self.assertIsNone(request_tags(value))

    def test_bot_mode_allows_empty_mine_phrase_but_requires_other(self):
        self.assertEqual(settings().mine_markers, ())
        with self.assertRaises(ValueError):
            settings(success_chat="")
        with self.assertRaises(ValueError):
            settings(other_phrases="")

    def test_telegram_projects_creation_time(self):
        raw = raw_message()
        raw.date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(project_message(raw).timestamp, raw.date.timestamp())


class ReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryStore()
        self.logs = []
        self.gate = asyncio.Event()

        async def sleep(delay):
            await self.gate.wait()

        self.clicker = Clicker(settings(), None, self.store, self.logs.append, sleep=sleep)
        self.clicker.receipt_chat_id = 99
        self.clicker.receipts_after = 10
        self.source = Message(123, 1, "подарок\n" + TAGS, ("Забрать",), 77)
        self.clicker.observe(self.source)

    async def asyncTearDown(self):
        await self.clicker.close()

    def arm(self, message=None):
        message = message or self.source
        self.store.claim(message.key)
        with patch("ayuclicker.core.time.time", return_value=1000.5):
            self.clicker.arm_receipt(message)

    def receipt(self, text=TAGS, **changes):
        values = dict(chat_id=99, message_id=11, text="Полученная заявка\n" + text,
                      buttons=(), sender_id=99, timestamp=1001)
        values.update(changes)
        return Message(**values)

    async def test_matching_both_tags_confirms_and_cancels(self):
        self.arm()
        self.assertTrue(self.clicker.observe_receipt(self.receipt()))
        self.assertEqual(self.store.outcomes, {self.source.key: "mine"})
        self.assertEqual(self.clicker.pending, {})
        self.assertEqual(self.clicker.receipt_origins, {})
        self.assertIn("забрал я — " + TAGS, self.logs[-1])

    async def test_wrong_id_name_chat_sender_old_message_or_negative_phrase_never_confirms(self):
        self.arm()
        for receipt in (
            self.receipt("#AD7734743520 [#SourDog]"),
            self.receipt("#AD773474352 [#SourDogExtra]"),
            self.receipt("#AD398493242 [#SourDog]"),
            self.receipt(chat_id=98), self.receipt(sender_id=98),
            self.receipt(message_id=10), self.receipt(timestamp=999),
            self.receipt("уже взял заявку\n" + TAGS),
        ):
            # Independent new events: do not accidentally rely on deduplication.
            self.clicker.receipts_after = 10
            with self.subTest(receipt=receipt):
                self.assertFalse(self.clicker.observe_receipt(receipt))
                self.assertEqual(self.store.outcomes, {})

    async def test_receipt_before_attempt_and_its_replayed_edit_do_not_confirm(self):
        receipt = self.receipt()
        self.assertFalse(self.clicker.observe_receipt(receipt))
        self.arm()
        self.assertFalse(self.clicker.observe_receipt(receipt))
        self.assertTrue(self.clicker.observe_receipt(self.receipt(message_id=12)))

    async def test_no_tags_and_ambiguous_pair_do_not_confirm(self):
        self.arm(Message(123, 1, "подарок /bin 514377", ("Забрать",), 77))
        self.assertFalse(self.clicker.observe_receipt(self.receipt()))
        self.clicker.receipts_after = 10
        second = Message(456, 1, self.source.text, self.source.buttons, 77)
        self.clicker.observe(second)
        self.arm(self.source)
        self.arm(second)
        self.assertFalse(self.clicker.observe_receipt(self.receipt()))
        self.assertEqual(self.store.outcomes, {})
        self.assertTrue(any("неоднозначно" in line for line in self.logs))

    async def test_two_groups_with_same_message_id_have_independent_tag_results(self):
        second = Message(456, 1, "подарок #AD398493242 [#ElatedPheasant]", ("Забрать",), 77)
        self.clicker.observe(second)
        self.arm(self.source)
        self.arm(second)
        self.assertTrue(self.clicker.observe_receipt(self.receipt("#AD398493242 [#ElatedPheasant]")))
        self.assertEqual(self.store.outcomes, {second.key: "mine"})
        self.assertIn(self.source.key, self.clicker.pending)
        self.assertIn("забрал я — #AD398493242 [#ElatedPheasant]", self.logs[-1])
        self.assertNotIn(TAGS, self.logs[-1])

    async def test_last_refetched_tags_are_used_before_callback_response_arrives(self):
        client = AsyncMock()
        raw = raw_message(message_id=1, chat_id=123, text="подарок #AD398493242 [#ElatedPheasant]")
        client.get_messages.return_value = raw
        sent = asyncio.Event()

        async def callback(request):
            sent.set()
            await asyncio.Event().wait()  # Bot delivers a private receipt first.

        client.side_effect = callback
        self.clicker.adapter = TelegramAdapter(client, {123: "entity"}, settings())
        self.gate.set()
        await asyncio.wait_for(sent.wait(), timeout=2)
        self.assertFalse(self.clicker.observe_receipt(self.receipt()))
        tasks = list(self.clicker.pending.values())
        self.assertTrue(self.clicker.observe_receipt(self.receipt("#AD398493242 [#ElatedPheasant]", message_id=12,
                                                                timestamp=None)))
        await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(self.store.outcomes[self.source.key], "mine")
        self.assertEqual(client.await_count, 1)
        self.assertIn("#AD398493242 [#ElatedPheasant]", self.logs[-1])
        self.assertNotIn(TAGS, self.logs[-1])

    async def test_other_claim_reply_retains_original_tags_in_log(self):
        self.arm()
        self.assertTrue(self.clicker.observe_reply(Message(123, 2, "Бивер zauron уже взял заявку.", (), 77, 1)))
        self.assertIn("забрал другой — " + TAGS, self.logs[-1])
        self.assertEqual(self.store.outcomes[self.source.key], "other")

    async def test_no_dispatch_when_condition_changed_cannot_arm_receipt(self):
        client = AsyncMock()
        client.get_messages.return_value = raw_message(message_id=1, chat_id=123, text="нет условия")
        adapter = TelegramAdapter(client, {123: "entity"}, settings())
        await adapter.click(self.source, "Забрать", before_dispatch=self.clicker.arm_receipt)
        self.assertEqual(self.clicker.receipt_origins, {})
        self.assertFalse(self.clicker.observe_receipt(self.receipt()))
        client.assert_not_awaited()


class ReceiptClient(MultiChatClient):
    def __init__(self, bot=True):
        super().__init__()
        self.entities[99] = types.InputPeerUser(99, 9)
        self.baselines[99] = 10
        self.bot = bot
        self.messages[(CHAT_A, 101)].raw_text += "\n" + TAGS
        self.messages[(CHAT_A, 101)].sender_id = 77

    async def get_input_entity(self, target):
        return await super().get_input_entity(99 if target == "receipt_bot" else target)

    async def get_entity(self, entity):
        return types.User(id=99, bot=self.bot)


class ReceiptSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_separate_bot_subscription_ignores_history_and_never_clicks_receipts(self):
        client = ReceiptClient()
        ready = asyncio.Event()
        clickers = []

        def make(*args):
            clicker = Clicker(*args)
            clickers.append(clicker)
            return clicker

        with tempfile.TemporaryDirectory() as temporary, patch("ayuclicker.telegram.TelegramClient", return_value=client), patch(
            "ayuclicker.telegram.Clicker", side_effect=make,
        ):
            task = asyncio.create_task(run_session(Path(temporary), 123, "hash", str(CHAT_A), settings(),
                                                  lambda kind, *args: ready.set() if kind == "watching" else None, AsyncMock()))
            try:
                await asyncio.wait_for(ready.wait(), timeout=2)
                source_handler = next(h for h, b in client.handlers if isinstance(b, events.NewMessage)
                                      and utils.get_peer_id(b.chats[0]) == CHAT_A)
                receipt_handler = next(h for h, b in client.handlers if isinstance(b, events.NewMessage)
                                       and utils.get_peer_id(b.chats[0]) == 99)
                self.assertEqual(len([b for h, b in client.handlers if isinstance(b, events.MessageEdited)]), 1)
                await source_handler(SimpleNamespace(message=client.messages[(CHAT_A, 101)]))
                source = project_message(client.messages[(CHAT_A, 101)])
                # Exercise the receipt event router independently of its delay.
                clickers[0].attempts.claim(source.key)
                clickers[0].arm_receipt(source)
                raw = raw_message(message_id=10, chat_id=99, text=source.text)
                raw.sender_id = 99
                await receipt_handler(SimpleNamespace(message=raw))
                self.assertIn(source.key, clickers[0].pending)
                raw.id = 11
                await receipt_handler(SimpleNamespace(message=raw))
                self.assertNotIn(source.key, clickers[0].pending)
                self.assertEqual(client.clicked, [])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(client.disconnects, 1)

    async def test_invalid_non_bot_overlapping_or_missing_receipt_chat_prevents_start(self):
        for bot, target in ((False, "@receipt_bot"), (True, str(CHAT_A)), (True, "@unknown")):
            client = ReceiptClient(bot)
            emitted = []
            with self.subTest(bot=bot, target=target), tempfile.TemporaryDirectory() as temporary, patch(
                "ayuclicker.telegram.TelegramClient", return_value=client,
            ):
                with self.assertRaises(ConfigurationError):
                    await run_session(Path(temporary), 123, "hash", str(CHAT_A), settings(success_chat=target),
                                      lambda *args: emitted.append(args), AsyncMock())
                self.assertFalse(any(value[0] == "watching" for value in emitted))
                self.assertEqual(client.handlers, [])
                self.assertEqual(client.disconnects, 1)
