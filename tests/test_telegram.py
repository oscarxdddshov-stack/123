import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon import Button, events, functions, types

from ayuclicker.core import Clicker, Message, Settings
from ayuclicker.telegram import (
    ConfigurationError, TelegramAdapter, parse_chat, parse_chats, project_message, run_session,
)


def raw_message(message_id=12, text="Есть подарок", with_callback=True, chat_id=42):
    callback = Button.inline("Забрать", b"claim")
    url = Button.url("Забрать", "https://example.com")
    return SimpleNamespace(
        id=message_id, chat_id=chat_id, raw_text=text,
        buttons=[[SimpleNamespace(text=item.text, button=item) for item in (
            (callback, url) if with_callback else (url,)
        )]],
    )


class TelegramMappingTests(unittest.TestCase):
    def test_only_callback_buttons_are_actionable(self):
        projected = project_message(raw_message())
        self.assertEqual(projected, Message(42, 12, "Есть подарок", ("Забрать",)))
        self.assertEqual(project_message(raw_message(with_callback=False)).buttons, ())

    def test_chat_formats_and_invalid_urls(self):
        for value in ("@example_bot", "https://t.me/example_bot", "t.me/example_bot/"):
            self.assertEqual(parse_chat(value), "example_bot")
        self.assertEqual(parse_chat("-100123"), -100123)
        for value in ("", "https://example.com", "t.me/+privateInvite", "t.me/c/123/4", "@example bot"):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                parse_chat(value)

    def test_multiple_ids_and_aliases_are_parsed_and_deduplicated(self):
        self.assertEqual(parse_chats("-100111, -100222\n-100111; 42"), (-100111, -100222, 42))
        self.assertEqual(parse_chats("@Example_bot, https://t.me/example_bot\nother_bot"), ("Example_bot", "other_bot"))
        self.assertEqual(parse_chats(" , -100111, \n"), (-100111,))
        for value in ("", " , ;\n", "0", "--100", "42, https://example.com"):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                parse_chats(value)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_click_sends_exact_callback_data(self):
        client = AsyncMock()
        client.get_messages.return_value = raw_message()
        adapter = TelegramAdapter(client, {42: "entity"}, Settings("подарок", "Забрать", 0, 0))
        message = await adapter.fetch((42, 12))
        await adapter.click(message, "забрать")
        request = client.call_args.args[0]
        self.assertIsInstance(request, functions.messages.GetBotCallbackAnswerRequest)
        self.assertEqual((request.peer, request.msg_id, request.data), ("entity", 12, b"claim"))
        self.assertEqual(client.await_count, 1)

    async def test_changed_word_or_url_never_sends_callback(self):
        for latest in (None, raw_message(text="условие исчезло"), raw_message(with_callback=False)):
            client = AsyncMock()
            client.get_messages.return_value = latest
            adapter = TelegramAdapter(client, {42: "entity"}, Settings("подарок", "Забрать", 0, 0))
            with self.assertRaises(ConfigurationError):
                await adapter.click(Message(42, 12, "подарок", ("Забрать",)), "Забрать")
            client.assert_not_awaited()

    async def test_adapter_refuses_an_unselected_chat_or_wrong_peer_result(self):
        client = AsyncMock()
        client.get_messages.return_value = raw_message(chat_id=43)
        adapter = TelegramAdapter(client, {42: "entity"}, Settings("подарок", "Забрать", 0, 0))
        with self.assertRaises(ConfigurationError):
            await adapter.fetch((99, 12))
        client.get_messages.assert_not_awaited()
        with self.assertRaises(ConfigurationError):
            await adapter.fetch((42, 12))
        with self.assertRaises(ConfigurationError):
            await adapter.click(Message(42, 12, "подарок", ("Забрать",)), "Забрать")
        client.assert_not_awaited()


class FakeClient:
    def __init__(self):
        self.disconnected = asyncio.get_running_loop().create_future()
        self.handlers = []
        self.clicked = []
        self.disconnects = 0
        self.current = raw_message(message_id=11)

    async def connect(self):
        pass

    async def disconnect(self):
        self.disconnects += 1

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return SimpleNamespace(id=7, bot=False)

    async def get_input_entity(self, target):
        return types.InputPeerUser(42, 12345)

    async def get_messages(self, entity, limit=None, ids=None):
        return [raw_message(message_id=10)] if limit else self.current

    def add_event_handler(self, handler, builder):
        self.handlers.append((handler, builder))

    async def iter_dialogs(self):
        yield SimpleNamespace(name="Test Bot", id=42)

    async def __call__(self, request):
        self.clicked.append(request)


CHAT_A = -1000000000111
CHAT_B = -1000000000222
CHAT_C = -1000000000333


class MultiChatClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.entities = {
            CHAT_A: types.InputPeerChannel(111, 1),
            CHAT_B: types.InputPeerChannel(222, 2),
        }
        self.baselines = {CHAT_A: 100, CHAT_B: 10}
        self.messages = {
            (CHAT_A, 101): raw_message(message_id=101, chat_id=CHAT_A),
            (CHAT_B, 11): raw_message(message_id=11, chat_id=CHAT_B),
            (CHAT_B, 101): raw_message(message_id=101, chat_id=CHAT_B),
        }
        self.dialog_reads = 0
        self.history_reads = []

    async def get_input_entity(self, target):
        if target == "same_chat":
            return self.entities[CHAT_A]
        if target not in self.entities:
            raise ValueError("Unknown chat")
        return self.entities[target]

    async def iter_dialogs(self):
        self.dialog_reads += 1
        for identifier in self.entities:
            yield SimpleNamespace(name="Test", id=identifier)

    async def get_messages(self, entity, limit=None, ids=None):
        from telethon import utils
        identifier = utils.get_peer_id(entity)
        if limit:
            self.history_reads.append(identifier)
            return [raw_message(message_id=self.baselines[identifier], chat_id=identifier)]
        return self.messages.get((identifier, ids))


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_ignores_history_and_cancels_on_stop(self):
        client = FakeClient()
        emitted = []
        ready = asyncio.Event()

        def emit(*value):
            emitted.append(value)
            if value[0] == "watching":
                ready.set()

        with tempfile.TemporaryDirectory() as temporary, patch(
            "ayuclicker.telegram.TelegramClient", return_value=client,
        ) as constructor:
            task = asyncio.create_task(run_session(
                Path(temporary), 123, "hash", "@test_bot",
                Settings("подарок", "Забрать", 0, 0), emit, AsyncMock(),
            ))
            await asyncio.wait_for(ready.wait(), timeout=2)
            observe = next(handler for handler, builder in client.handlers if isinstance(builder, events.NewMessage))
            await observe(SimpleNamespace(message=raw_message(message_id=10)))
            await asyncio.sleep(0)
            self.assertEqual(client.clicked, [])
            await observe(SimpleNamespace(message=raw_message(message_id=11)))
            for _ in range(6):
                await asyncio.sleep(0)
            self.assertEqual(len(client.clicked), 1)
            await observe(SimpleNamespace(message=raw_message(message_id=11)))
            await asyncio.sleep(0)
            self.assertEqual(len(client.clicked), 1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(client.disconnects, 1)
            self.assertEqual(constructor.call_args.kwargs["request_retries"], 0)

    async def test_dialog_list_has_ids_without_running_clicker(self):
        client = FakeClient()
        emitted = []
        with tempfile.TemporaryDirectory() as temporary, patch(
            "ayuclicker.telegram.TelegramClient", return_value=client,
        ):
            await run_session(Path(temporary), 123, "hash", "", None, lambda *args: emitted.append(args), AsyncMock())
        self.assertIn(("dialogs", [("Test Bot", 42)]), emitted)
        self.assertEqual(client.handlers, [])
        self.assertEqual(client.disconnects, 1)

    async def test_multiple_chats_use_own_baselines_peers_and_deduplication(self):
        from telethon import utils
        client = MultiChatClient()
        ready = asyncio.Event()
        emitted = []

        def emit(*value):
            emitted.append(value)
            if value[0] == "watching":
                ready.set()

        with tempfile.TemporaryDirectory() as temporary, patch("ayuclicker.telegram.TelegramClient", return_value=client):
            task = asyncio.create_task(run_session(
                Path(temporary), 123, "hash", f"{CHAT_A}\n{CHAT_B}, {CHAT_A}",
                Settings("подарок", "Забрать", 0, 0), emit, AsyncMock(),
            ))
            try:
                await asyncio.wait_for(ready.wait(), timeout=2)
                observe, builder = next((handler, builder) for handler, builder in client.handlers if isinstance(builder, events.NewMessage))
                self.assertEqual({utils.get_peer_id(peer) for peer in builder.chats}, {CHAT_A, CHAT_B})
                self.assertEqual(client.dialog_reads, 1)
                self.assertIn(("watching", 2), emitted)
                # Old messages and an unselected chat are ignored, even when
                # invoking the handler directly without Telethon's own filter.
                for identifier, message_id in ((CHAT_A, 100), (CHAT_B, 10), (CHAT_C, 500)):
                    await observe(SimpleNamespace(message=raw_message(message_id=message_id, chat_id=identifier)))
                for message in client.messages.values():
                    await observe(SimpleNamespace(message=message))
                    await observe(SimpleNamespace(message=message))
                for _ in range(8):
                    await asyncio.sleep(0)
                actual = {(utils.get_peer_id(request.peer), request.msg_id) for request in client.clicked}
                self.assertEqual(actual, {(CHAT_A, 101), (CHAT_B, 11), (CHAT_B, 101)})
                self.assertEqual(len(client.clicked), 3)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(client.disconnects, 1)

    async def test_deleted_message_cancels_only_its_own_chat_timer(self):
        client = MultiChatClient()
        ready = asyncio.Event()
        clickers = []

        def make_clicker(*args):
            clicker = Clicker(*args)
            clickers.append(clicker)
            return clicker

        with tempfile.TemporaryDirectory() as temporary, patch("ayuclicker.telegram.TelegramClient", return_value=client), patch(
            "ayuclicker.telegram.Clicker", side_effect=make_clicker,
        ):
            task = asyncio.create_task(run_session(
                Path(temporary), 123, "hash", f"{CHAT_A}, {CHAT_B}", Settings("подарок", "Забрать", 10, 10),
                lambda kind, *args: ready.set() if kind == "watching" else None, AsyncMock(),
            ))
            try:
                await asyncio.wait_for(ready.wait(), timeout=2)
                observe = next(handler for handler, builder in client.handlers if isinstance(builder, events.NewMessage))
                deleted = next(handler for handler, builder in client.handlers if isinstance(builder, events.MessageDeleted))
                for identifier in (CHAT_A, CHAT_B):
                    await observe(SimpleNamespace(message=client.messages[(identifier, 101)]))
                self.assertEqual(set(clickers[0].pending), {(CHAT_A, 101), (CHAT_B, 101)})
                await deleted(SimpleNamespace(chat_id=CHAT_C, deleted_ids=[101]))
                await deleted(SimpleNamespace(chat_id=None, deleted_ids=[101]))
                self.assertEqual(len(clickers[0].pending), 2)
                await deleted(SimpleNamespace(chat_id=CHAT_A, deleted_ids=[101]))
                self.assertEqual(set(clickers[0].pending), {(CHAT_B, 101)})
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(clickers[0].pending, {})
            self.assertEqual(client.clicked, [])

    async def test_aliases_resolving_to_one_chat_start_one_subscription(self):
        client = MultiChatClient()
        ready = asyncio.Event()
        emitted = []

        def emit(*value):
            emitted.append(value)
            if value[0] == "watching":
                ready.set()

        with tempfile.TemporaryDirectory() as temporary, patch("ayuclicker.telegram.TelegramClient", return_value=client):
            task = asyncio.create_task(run_session(
                Path(temporary), 123, "hash", f"{CHAT_A}, @same_chat", Settings("подарок", "Забрать", 0, 0), emit, AsyncMock(),
            ))
            try:
                await asyncio.wait_for(ready.wait(), timeout=2)
                self.assertIn(("watching", 1), emitted)
                self.assertEqual(client.history_reads, [CHAT_A])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_missing_chat_reports_id_and_does_not_start_partial_watch(self):
        client = MultiChatClient()
        emitted = []
        with tempfile.TemporaryDirectory() as temporary, patch("ayuclicker.telegram.TelegramClient", return_value=client):
            with self.assertRaisesRegex(ConfigurationError, str(CHAT_C)):
                await run_session(
                    Path(temporary), 123, "hash", f"{CHAT_A}, {CHAT_C}",
                    Settings("подарок", "Забрать", 0, 0), lambda *args: emitted.append(args), AsyncMock(),
                )
        self.assertEqual(client.handlers, [])
        self.assertFalse(any(value[0] == "watching" for value in emitted))
        self.assertEqual(client.clicked, [])
        self.assertEqual(client.disconnects, 1)
