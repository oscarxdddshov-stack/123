"""Telegram user-account adapter; only inline callback buttons are supported."""

import asyncio
from pathlib import Path
from typing import Awaitable, Callable

from telethon import TelegramClient, errors, events, functions, types, utils

from .core import Clicker, Message, Settings
from .storage import SqliteAttempts


class UserCancelled(Exception):
    pass


class ConfigurationError(Exception):
    pass


def explain_error(error: Exception) -> str:
    if isinstance(error, ConfigurationError):
        return str(error)
    if isinstance(error, errors.FloodWaitError):
        return f"Telegram просит подождать {error.seconds} с. Запустите снова после этого."
    explanations = {
        "ApiIdInvalidError": "Проверьте API ID и API hash с my.telegram.org.",
        "PhoneNumberInvalidError": "Неверный номер телефона. Нужен международный формат: +7…",
        "PhoneCodeExpiredError": "Код входа истёк. Повторите вход.",
        "SessionRevokedError": "Сессия отозвана. Удалите локальную сессию и войдите снова.",
        "AuthKeyUnregisteredError": "Сессия недействительна. Удалите локальную сессию и войдите снова.",
        "AuthKeyDuplicatedError": "Одна сессия используется на разных устройствах. Войдите заново.",
        "ChannelPrivateError": "Аккаунт не имеет доступа к выбранному чату.",
        "ChatWriteForbiddenError": "В этом чате операция недоступна для аккаунта.",
        "ConnectionError": "Соединение с Telegram потеряно. Проверьте сеть и запустите снова.",
        "TimeoutError": "Telegram не ответил вовремя. Проверьте сеть и запустите снова.",
    }
    return explanations.get(
        type(error).__name__,
        f"Ошибка {type(error).__name__}. Проверьте сеть, доступ к чату и параметры входа.",
    )


def is_callback(button) -> bool:
    return (
        isinstance(button.button, types.KeyboardInlineButton)
        and isinstance(button.button.type, types.InlineButtonTypeCallback)
    )


def project_message(raw) -> Message:
    buttons = tuple(
        button.text
        for row in (raw.buttons or [])
        for button in row
        if is_callback(button)
    )
    return Message(raw.chat_id, raw.id, raw.raw_text or "", buttons)


def parse_chat(value: str) -> int | str:
    value = value.strip()
    if not value:
        raise ConfigurationError("Выберите чат или введите его @username / числовой ID.")
    if value.lstrip("-").isdigit():
        return int(value)
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if value.startswith(prefix):
            value = value[len(prefix):].rstrip("/")
            break
    username = value.lstrip("@")
    if not username or not all(char.isascii() and (char.isalnum() or char == "_") for char in username):
        raise ConfigurationError(
            "Укажите @username, ссылку t.me/username или выберите приватный чат из списка."
        )
    return username


class TelegramAdapter:
    def __init__(self, client: TelegramClient, entity, settings: Settings):
        self.client = client
        self.entity = entity
        self.settings = settings

    async def fetch(self, key: tuple[int, int]) -> Message | None:
        raw = await self.client.get_messages(self.entity, ids=key[1])
        if raw is None or isinstance(raw, types.MessageEmpty):
            return None
        return project_message(raw)

    async def click(self, message: Message, title: str) -> None:
        # Fetch again before sending the exact callback data, not coordinates.
        raw = await self.client.get_messages(self.entity, ids=message.message_id)
        if raw is None or isinstance(raw, types.MessageEmpty) or not self.settings.matches(project_message(raw)):
            raise ConfigurationError("Сообщение изменилось перед нажатием.")
        for row in raw.buttons or []:
            for button in row:
                if (
                    is_callback(button)
                    and button.text.strip().casefold() == title.casefold()
                ):
                    await self.client(functions.messages.GetBotCallbackAnswerRequest(
                        peer=self.entity, msg_id=raw.id, data=button.button.type.data,
                    ))
                    return
        raise ConfigurationError("Кнопка больше недоступна.")


async def authorize(
    client: TelegramClient,
    prompt: Callable[[str, bool], Awaitable[str]],
    log: Callable[[str], None],
) -> None:
    if await client.is_user_authorized():
        return
    phone = (await prompt("Номер телефона этого аккаунта в международном формате (+7…):", False)).strip()
    sent = await client.send_code_request(phone)
    log("Код отправлен. Проверьте служебный чат Telegram или SMS.")
    for _ in range(3):
        code = (await prompt("Код входа из Telegram / SMS:", True)).replace(" ", "").strip()
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
            return
        except errors.PhoneCodeInvalidError:
            log("Неверный код. Попробуйте ещё раз.")
        except errors.SessionPasswordNeededError:
            for _ in range(3):
                password = await prompt("Пароль двухэтапной аутентификации Telegram:", True)
                try:
                    await client.sign_in(password=password)
                    return
                except errors.PasswordHashInvalidError:
                    log("Неверный пароль. Попробуйте ещё раз.")
            raise ConfigurationError("Не удалось войти: неверный пароль.")
    raise ConfigurationError("Не удалось войти: неверный код.")


async def run_session(
    directory: Path,
    api_id: int,
    api_hash: str,
    chat: str,
    settings: Settings | None,
    emit: Callable[..., None],
    prompt: Callable[[str, bool], Awaitable[str]],
) -> None:
    client = TelegramClient(
        str(directory / "telegram"), api_id, api_hash,
        device_model="AyuClicker", app_version="1.0",
        request_retries=0, flood_sleep_threshold=0,
        connection_retries=3, retry_delay=1, timeout=10,
    )
    attempts = None
    clicker = None
    log = lambda text: emit("log", text)
    try:
        log("Подключение к Telegram…")
        await client.connect()
        session_file = directory / "telegram.session"
        if session_file.exists():
            session_file.chmod(0o600)
        await authorize(client, prompt, log)
        me = await client.get_me()
        if me is None or me.bot:
            raise ConfigurationError("Нужен вход в пользовательский аккаунт Telegram.")
        log("Вход выполнен.")

        if settings is None:
            dialogs = [(dialog.name, dialog.id) async for dialog in client.iter_dialogs()]
            emit("dialogs", dialogs)
            log(f"Загружено чатов: {len(dialogs)}.")
            return

        # Populate the entity cache, including private chats specified by ID.
        target = parse_chat(chat)
        if isinstance(target, int):
            async for dialog in client.iter_dialogs():
                if dialog.id == target:
                    break
        try:
            entity = await client.get_input_entity(target)
        except ValueError as error:
            raise ConfigurationError("Чат не найден. Загрузите список чатов и выберите нужный.") from error
        chat_id = utils.get_peer_id(entity)
        latest = await client.get_messages(entity, limit=1)
        baseline = latest[0].id if latest else 0
        attempts = SqliteAttempts(directory / "attempts.sqlite3", me.id)
        clicker = Clicker(settings, TelegramAdapter(client, entity, settings), attempts, log)

        async def observe(event):
            # Ignore history, including edits of messages predating this run.
            if event.message.id > baseline:
                clicker.observe(project_message(event.message))

        async def deleted(event):
            # Private-chat deletions sometimes have no chat ID. Refetch remains
            # the authoritative check; do not cancel another chat's message.
            if event.chat_id == chat_id:
                for message_id in event.deleted_ids:
                    clicker.remove((chat_id, message_id))

        client.add_event_handler(observe, events.NewMessage(chats=entity, incoming=True))
        client.add_event_handler(observe, events.MessageEdited(chats=entity, incoming=True))
        client.add_event_handler(deleted, events.MessageDeleted())
        emit("watching")
        log("Наблюдение включено. Обрабатываются только новые входящие сообщения выбранного чата.")
        await client.disconnected
        raise ConnectionError()
    finally:
        if clicker is not None:
            await clicker.close()
        await client.disconnect()
        if attempts is not None:
            attempts.close()
