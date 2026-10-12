import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon import Button, events, functions, types

from ayuclicker.core import Message, Settings
from ayuclicker.telegram import (
    ConfigurationError, TelegramAdapter, parse_chat, project_message, run_session,
)


def raw_message(message_id=12, text="Есть подарок", with_callback=True):
    callback = Button.inline("Забрать", b"claim")
    url = Button.url("Забрать", "https://example.com")
    return SimpleNamespace(
        id=message_id, chat_id=42, raw_text=text,
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


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_click_sends_exact_callback_data(self):
        client = AsyncMock()
        client.get_messages.return_value = raw_message()
        adapter = TelegramAdapter(client, "entity", Settings("подарок", "Забрать", 0, 0))
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
            adapter = TelegramAdapter(client, "entity", Settings("подарок", "Забрать", 0, 0))
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
