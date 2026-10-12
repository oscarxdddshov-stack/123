"""Telegram user-account adapter; only inline callback buttons are supported."""

import asyncio
from pathlib import Path
import re
import time
from typing import Awaitable, Callable

from telethon import TelegramClient, errors, events, functions, types, utils

from .core import Clicker, ClickResponse, Message, RetryLater, Settings, message_label
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
    reply_to_id = getattr(raw, "reply_to_msg_id", None)
    reply_peer = getattr(getattr(raw, "reply_to", None), "reply_to_peer_id", None)
    if reply_peer is not None and utils.get_peer_id(reply_peer) != raw.chat_id:
        reply_to_id = None
    date = getattr(raw, "date", None)
    return Message(raw.chat_id, raw.id, raw.raw_text or "", buttons, getattr(raw, "sender_id", None),
                   reply_to_id, date.timestamp() if date is not None else None)


def parse_chat(value: str) -> int | str:
    value = value.strip()
    if not value:
        raise ConfigurationError("Выберите чат или введите его @username / числовой ID.")
    if re.fullmatch(r"-?[0-9]+", value):
        if int(value) == 0:
            raise ConfigurationError("ID чата не может быть равен нулю.")
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


def parse_chats(value: str) -> tuple[int | str, ...]:
    targets = []
    seen = set()
    for item in re.split(r"[,;\r\n]+", value):
        if not item.strip():
            continue
        target = parse_chat(item)
        identity = target.casefold() if isinstance(target, str) else target
        if identity not in seen:
            targets.append(target)
            seen.add(identity)
    if not targets:
        raise ConfigurationError("Укажите хотя бы один ID чата. Несколько ID — через запятую или с новой строки.")
    return tuple(targets)


class TelegramAdapter:
    def __init__(self, client: TelegramClient, entities: dict[int, object], settings: Settings):
        self.client = client
        self.entities = dict(entities)
        self.settings = settings
        self.pause_until = 0.0

    async def request(self, operation):
        remaining = self.pause_until - time.monotonic()
        if remaining > 0:
            raise RetryLater("Telegram ограничил частоту запросов", remaining)
        try:
            return await operation()
        except (errors.FloodWaitError, errors.SlowModeWaitError, errors.FloodPremiumWaitError) as error:
            seconds = max(1, error.seconds)
            self.pause_until = max(self.pause_until, time.monotonic() + seconds)
            raise RetryLater(type(error).__name__, seconds) from error
        except (TimeoutError, OSError, errors.BotResponseTimeoutError, errors.ServerError,
                errors.RpcCallFailError, errors.TimedOutError) as error:
            raise RetryLater(type(error).__name__) from error

    def entity_for(self, chat_id):
        try:
            return self.entities[chat_id]
        except KeyError as error:
            raise ConfigurationError("Этот чат не включён в список наблюдения.") from error

    async def fetch(self, key: tuple[int, int]) -> Message | None:
        entity = self.entity_for(key[0])
        raw = await self.request(lambda: self.client.get_messages(entity, ids=key[1]))
        if raw is None or isinstance(raw, types.MessageEmpty):
            return None
        message = project_message(raw)
        if message.key != key:
            raise ConfigurationError("Получено сообщение из другого чата. Нажатие отменено.")
        return message

    async def click(self, message: Message, title: str, *, before_dispatch=None) -> ClickResponse:
        # Fetch again before sending the exact callback data, not coordinates.
        entity = self.entity_for(message.chat_id)
        raw = await self.request(lambda: self.client.get_messages(entity, ids=message.message_id))
        if raw is None or isinstance(raw, types.MessageEmpty):
            if self.settings.repeat_until_result:
                return ClickResponse(dispatched=False)
            raise ConfigurationError("Сообщение изменилось перед нажатием.")
        latest = project_message(raw)
        if latest.key != message.key:
            raise ConfigurationError("Получено сообщение из другого чата. Нажатие отменено.")
        if self.settings.repeat_until_result and self.settings.outcome(latest.text):
            return ClickResponse(latest.text, dispatched=False)
        if not self.settings.matches(latest):
            if self.settings.repeat_until_result:
                return ClickResponse(dispatched=False)
            raise ConfigurationError("Сообщение изменилось перед нажатием.")
        for row in raw.buttons or []:
            for button in row:
                if (
                    is_callback(button)
                    and button.text.strip().casefold() == title.casefold()
                ):
                    def dispatch():
                        if before_dispatch is not None:
                            before_dispatch(latest)
                        return self.client(functions.messages.GetBotCallbackAnswerRequest(
                            peer=entity, msg_id=raw.id, data=button.button.type.data,
                        ))
                    response = await self.request(dispatch)
                    return ClickResponse(response.message or "")
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
    targets = parse_chats(chat) if settings is not None else ()
    receipt_target = (parse_chat(settings.success_chat) if settings is not None
                      and settings.repeat_until_result and settings.success_chat.strip() else None)
    client = TelegramClient(
        str(directory / "telegram"), api_id, api_hash,
        device_model="AyuClicker", app_version="1.0",
        request_retries=0, flood_sleep_threshold=0,
        raise_last_call_error=True,
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

        # Load the dialog cache once for all numeric IDs, not once per chat.
        remaining_ids = {target for target in targets if isinstance(target, int)}
        if isinstance(receipt_target, int):
            remaining_ids.add(receipt_target)
        if remaining_ids:
            async for dialog in client.iter_dialogs():
                remaining_ids.discard(dialog.id)
                if not remaining_ids:
                    break
        entities = {}
        baselines = {}
        for target in targets:
            try:
                entity = await client.get_input_entity(target)
                chat_id = utils.get_peer_id(entity)
                if chat_id in entities:
                    continue  # ID and username may name the same chat.
                latest = await client.get_messages(entity, limit=1)
            except (ValueError, errors.UsernameInvalidError, errors.UsernameNotOccupiedError,
                    errors.ChannelPrivateError, errors.PeerIdInvalidError) as error:
                raise ConfigurationError(
                    f"Чат {target} не найден или недоступен этому аккаунту. "
                    "Проверьте ID и доступ; можно загрузить список чатов. Наблюдение не включено."
                ) from error
            entities[chat_id] = entity
            baselines[chat_id] = latest[0].id if latest else 0
            log(f"Подготовлен чат {chat_id}.")
        attempts = SqliteAttempts(directory / "attempts.sqlite3", me.id)
        clicker = Clicker(settings, TelegramAdapter(client, entities, settings), attempts, log)
        receipt_entity = None
        if receipt_target is not None:
            try:
                receipt_entity = await client.get_input_entity(receipt_target)
                bot = await client.get_entity(receipt_entity)
                if not isinstance(bot, types.User) or not bot.bot:
                    raise ConfigurationError("Диалог подтверждения должен быть личным диалогом с ботом.")
                receipt_id = utils.get_peer_id(receipt_entity)
                if receipt_id in entities:
                    raise ConfigurationError("Диалог подтверждения укажите отдельно от чатов с кнопками.")
                latest = await client.get_messages(receipt_entity, limit=1)
            except (ValueError, errors.UsernameInvalidError, errors.UsernameNotOccupiedError,
                    errors.ChannelPrivateError, errors.PeerIdInvalidError) as error:
                raise ConfigurationError("Диалог подтверждения не найден. Проверьте ID / @username бота и доступ.") from error
            clicker.receipt_chat_id = receipt_id
            clicker.receipts_after = latest[0].id if latest else 0
            log(f"Подтверждение получения: диалог {receipt_id}, совпадение обоих тегов #AD… [#…].")

        async def observe(event):
            # Ignore history, including edits of messages predating this run.
            chat_id = event.message.chat_id
            if chat_id in baselines and event.message.id > baselines[chat_id]:
                message = project_message(event.message)
                clicker.observe_reply(message)
                clicker.observe(message)

        async def deleted(event):
            # Private-chat deletions sometimes have no chat ID. Refetch remains
            # the authoritative check; do not cancel another chat's message.
            if event.chat_id in entities:
                for message_id in event.deleted_ids:
                    key = (event.chat_id, message_id)
                    if settings.repeat_until_result and key in clicker.pending and attempts.contains(key):
                        log(f"{message_label(key)}: удалено; результат не подтверждён, ожидаем ответ бота.")
                    else:
                        clicker.remove(key)

        async def receipt(event):
            clicker.observe_receipt(project_message(event.message))

        selected = list(entities.values())
        client.add_event_handler(observe, events.NewMessage(chats=selected, incoming=True))
        client.add_event_handler(observe, events.MessageEdited(chats=selected, incoming=True))
        client.add_event_handler(deleted, events.MessageDeleted())
        if receipt_entity is not None:
            client.add_event_handler(receipt, events.NewMessage(chats=[receipt_entity], incoming=True))
        emit("watching", len(entities))
        log(f"Наблюдение включено: чатов {len(entities)}. Обрабатываются только новые входящие сообщения выбранных чатов.")
        await client.disconnected
        raise ConnectionError()
    finally:
        if clicker is not None:
            await clicker.close()
        await client.disconnect()
        if attempts is not None:
            attempts.close()
