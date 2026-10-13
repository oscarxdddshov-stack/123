"""Matching and scheduling, independent of Telegram and the desktop UI."""

from __future__ import annotations

import asyncio
import math
import random
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol


REQUEST_TAGS = re.compile(r"(?<![\w#])#(AD[0-9]+)\s*\[\s*#([A-Za-z][A-Za-z0-9_]*)\s*\]\s*\Z")


def request_tag_label(text: str) -> str:
    match = REQUEST_TAGS.search(text)
    return f"#{match.group(1)} [#{match.group(2)}]" if match else ""


def request_tags(text: str) -> tuple[str, str] | None:
    """The complete trailing request identifier, never a BIN or partial tag."""
    match = REQUEST_TAGS.search(text)
    return (match.group(1).casefold(), match.group(2).casefold()) if match else None


@dataclass(frozen=True)
class TextRule:
    name: str
    bin_numbers: tuple[str, ...] = ()

    @classmethod
    def parse(cls, term: str) -> TextRule:
        if "(" not in term and ")" not in term:
            return cls(term)
        match = re.fullmatch(r"([^()]+?)\s*\(\s*([0-9]+(?:\s*,\s*[0-9]+)*)\s*\)", term)
        if match is None or not match.group(1).strip():
            raise ValueError(
                "Условие с BIN должно иметь формат Название(цифры), например Bancorp(497213,518913). "
                "Для названия без BIN не используйте скобки."
            )
        numbers = tuple(dict.fromkeys(number.strip() for number in match.group(2).split(",")))
        return cls(match.group(1).strip(), numbers)

    def matches(self, text: str, whole_word: bool) -> bool:
        if whole_word:
            name_matches = re.search(r"(?<!\w)" + re.escape(self.name) + r"(?!\w)", text, re.IGNORECASE) is not None
        else:
            name_matches = self.name.casefold() in text.casefold()
        if not name_matches:
            return False
        if not self.bin_numbers:
            return True
        # Match the complete digit sequence even when substring mode is enabled.
        # /bin is an optional label; title and BIN must belong to this message.
        return any(re.search(r"(?<!\d)" + re.escape(number) + r"(?!\d)", text) is not None
                   for number in self.bin_numbers)


def split_terms(source: str) -> tuple[str, ...]:
    """Separate names at top-level commas, keeping each bank's BIN list intact."""
    terms = []
    start = 0
    inside = False
    for position, character in enumerate(source):
        if character == "(":
            if inside:
                raise ValueError("В условиях BIN нельзя вкладывать скобки друг в друга.")
            inside = True
        elif character == ")":
            if not inside:
                raise ValueError("В условии BIN лишняя закрывающая скобка.")
            inside = False
        elif character == "," and not inside:
            if term := source[start:position].strip():
                terms.append(term)
            start = position + 1
    if inside:
        raise ValueError("Закройте скобку в условии BIN, например Bancorp(497213,518913).")
    if term := source[start:].strip():
        terms.append(term)
    return tuple(terms)


@dataclass(frozen=True)
class Settings:
    word: str
    button: str
    minimum: float
    maximum: float
    whole_word: bool = True
    repeat_until_result: bool = False
    mine_phrases: str = ""
    other_phrases: str = ""
    success_chat: str = ""
    terms: tuple[str, ...] = field(init=False, repr=False)
    rules: tuple[TextRule, ...] = field(init=False, repr=False)
    mine_markers: tuple[str, ...] = field(init=False, repr=False)
    other_markers: tuple[str, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        terms = split_terms(self.word)
        if not terms or not self.button.strip():
            raise ValueError("Укажите хотя бы одно слово / фразу и название кнопки.")
        object.__setattr__(self, "terms", terms)
        object.__setattr__(self, "rules", tuple(TextRule.parse(term) for term in terms))
        for name, source in (("mine_markers", self.mine_phrases), ("other_markers", self.other_phrases)):
            markers = tuple(" ".join(item.split()).casefold() for item in re.split(r"[;\r\n]+", source) if item.strip())
            object.__setattr__(self, name, markers)
        if not all(math.isfinite(value) for value in (self.minimum, self.maximum)):
            raise ValueError("Задержки должны быть конечными числами.")
        if not 0 <= self.minimum <= self.maximum:
            raise ValueError("Нужен диапазон: 0 ≤ минимум ≤ максимум секунд.")
        if self.repeat_until_result:
            if self.minimum <= 0:
                raise ValueError("Для повторных нажатий минимальная задержка должна быть больше нуля.")
            if (not self.mine_markers and not self.success_chat.strip()) or not self.other_markers:
                raise ValueError("Для повторов укажите «Забрал я» или диалог подтверждения по тегам, а также «Забрал другой».")
            if set(self.mine_markers) & set(self.other_markers):
                raise ValueError("Фразы «Забрал я» и «Забрал другой» должны отличаться.")

    def outcome(self, text: str) -> str | None:
        normalized = " ".join(text.split()).casefold()
        if any(marker in normalized for marker in self.other_markers):
            return "other"
        if any(marker in normalized for marker in self.mine_markers):
            return "mine"
        return None

    def matches(self, message: Message) -> bool:
        contains_word = any(rule.matches(message.text, self.whole_word) for rule in self.rules)
        return contains_word and self.button.strip().casefold() in (
            label.strip().casefold() for label in message.buttons
        )


@dataclass(frozen=True)
class Message:
    chat_id: int
    message_id: int
    text: str
    buttons: tuple[str, ...]
    sender_id: int | None = None
    reply_to_id: int | None = None
    timestamp: float | None = None

    @property
    def key(self) -> tuple[int, int]:
        return self.chat_id, self.message_id


@dataclass(frozen=True)
class ClickResponse:
    text: str = ""
    dispatched: bool = True


class RetryLater(Exception):
    def __init__(self, reason: str, seconds: float = 0):
        super().__init__(reason)
        self.reason = reason
        self.seconds = max(0, seconds)


class Adapter(Protocol):
    async def fetch(self, key: tuple[int, int]) -> Message | None: ...

    async def click(self, message: Message, title: str, *, before_dispatch=None) -> ClickResponse: ...


class AttemptStore(Protocol):
    def contains(self, key: tuple[int, int]) -> bool: ...

    def claim(self, key: tuple[int, int]) -> bool: ...

    def complete(self, key: tuple[int, int], outcome: str) -> None: ...


def message_label(key: tuple[int, int]) -> str:
    return f"Чат {key[0]}, сообщение {key[1]}"


class Clicker:
    """One worker per message; optional retries stop only on configured evidence."""

    def __init__(
        self,
        settings: Settings,
        adapter: Adapter,
        attempts: AttemptStore,
        log: Callable[[str], None],
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.settings = settings
        self.adapter = adapter
        self.attempts = attempts
        self.log = log
        self.sleep = sleep
        self.uniform = uniform
        self.pending: dict[tuple[int, int], asyncio.Task[None]] = {}
        self.origins: dict[tuple[int, int], int | None] = {}
        self.request_labels: dict[tuple[int, int], str] = {}
        self.receipt_chat_id: int | None = None
        self.receipts_after: int = 0
        self.receipt_origins: dict[tuple[int, int], tuple[tuple[str, str], float]] = {}
        self.closed = False

    def observe(self, message: Message) -> None:
        key = message.key
        if self.settings.repeat_until_result and key in self.pending:
            if self.settings.matches(message):
                self.request_labels[key] = request_tag_label(message.text)
            outcome = self.settings.outcome(message.text)
            if outcome is not None:
                self.confirm(key, outcome)
            elif not self.settings.matches(message) and not self.attempts.contains(key):
                self.remove(key)
                self.log(f"{message_label(key)}: сообщение изменилось; результат не подтверждён.")
            return
        if self.closed or self.attempts.contains(key):
            return
        if not self.settings.matches(message):
            self.remove(key)
            return
        if key in self.pending:
            return
        delay = self.uniform(self.settings.minimum, self.settings.maximum)
        self.log(f"{message_label(key)}: нажатие через {delay:.2f} с.")
        task = asyncio.create_task(self._repeat(key, delay) if self.settings.repeat_until_result else self._run(key, delay))
        self.pending[key] = task
        self.origins[key] = message.sender_id
        self.request_labels[key] = request_tag_label(message.text)
        task.add_done_callback(lambda done: self._finished(key, done))

    def remove(self, key: tuple[int, int]) -> None:
        task = self.pending.pop(key, None)
        self.origins.pop(key, None)
        self.request_labels.pop(key, None)
        self.receipt_origins.pop(key, None)
        if task is not None:
            task.cancel()
            self.log(f"{message_label(key)}: ожидающее нажатие отменено.")

    def confirm(self, key: tuple[int, int], outcome: str, tag_label: str = "") -> bool:
        if outcome not in ("mine", "other") or key not in self.pending:
            return False
        if not self.attempts.contains(key):
            if not self.attempts.claim(key):
                return False
        self.attempts.complete(key, outcome)
        label = "подтверждено: забрал я" if outcome == "mine" else "подтверждено: забрал другой"
        tags = tag_label or self.request_labels.get(key, "")
        detail = f" — {tags}" if tags else ""
        self.log(f"{message_label(key)}: {label}{detail}. Повторы остановлены.")
        task = self.pending.pop(key)
        self.origins.pop(key, None)
        self.request_labels.pop(key, None)
        self.receipt_origins.pop(key, None)
        if task is not asyncio.current_task():
            task.cancel()
        return True

    def observe_reply(self, message: Message) -> bool:
        if not self.settings.repeat_until_result or message.reply_to_id is None:
            return False
        key = (message.chat_id, message.reply_to_id)
        if message.sender_id is None or message.sender_id != self.origins.get(key):
            return False
        outcome = self.settings.outcome(message.text)
        return self.confirm(key, outcome) if outcome is not None else False

    def arm_receipt(self, message: Message) -> None:
        # Called with the last re-read immediately before a real callback request.
        # Keep the first request time for timeouts and update the actual tags.
        tags = request_tags(message.text)
        self.request_labels[message.key] = request_tag_label(message.text)
        previous = self.receipt_origins.get(message.key)
        if tags is not None:
            started = previous[1] if previous and previous[0] == tags else time.time()
            self.receipt_origins[message.key] = (tags, started)
        else:
            self.receipt_origins.pop(message.key, None)

    def observe_receipt(self, message: Message) -> bool:
        if self.closed or not self.settings.repeat_until_result or message.chat_id != self.receipt_chat_id:
            return False
        # One new incoming message can confirm once; old messages and edits cannot.
        if message.message_id <= self.receipts_after:
            return False
        self.receipts_after = message.message_id
        if message.sender_id != self.receipt_chat_id:
            return False
        tags = request_tags(message.text)
        if tags is None or self.settings.outcome(message.text) == "other":
            return False
        matches = [key for key, (source, started) in self.receipt_origins.items()
                   if key in self.pending and source == tags
                   and (message.timestamp is None or message.timestamp >= int(started))]
        if len(matches) > 1:
            self.log("Подтверждение по тегам неоднозначно: одинаковая пара у нескольких заявок. Повторы не остановлены.")
            return False
        return self.confirm(matches[0], "mine", request_tag_label(message.text)) if matches else False

    def _finished(self, key: tuple[int, int], task: asyncio.Task[None]) -> None:
        if self.pending.get(key) is task:
            self.pending.pop(key, None)
            self.origins.pop(key, None)
            self.request_labels.pop(key, None)
            self.receipt_origins.pop(key, None)
        if not task.cancelled() and task.exception() is not None:
            self.log(f"{message_label(key)}: внутренняя ошибка {type(task.exception()).__name__}.")

    async def _run(self, key: tuple[int, int], delay: float) -> None:
        try:
            await self.sleep(delay)
            latest = await self.adapter.fetch(key)
            if latest is None or not self.settings.matches(latest):
                self.log(f"{message_label(key)}: условие больше не выполнено.")
                return
            if self.closed or not self.attempts.claim(key):
                return
            await self.adapter.click(latest, self.settings.button.strip())
            self.attempts.complete(key, "single_attempt")
            self.log(f"{message_label(key)}: кнопка нажата.")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Do not log exception text: library errors can contain credentials.
            self.log(
                f"{message_label(key)}: ошибка {type(error).__name__}. "
                "Повторное нажатие автоматически не выполняется."
            )

    async def _repeat(self, key: tuple[int, int], delay: float) -> None:
        owned = False
        number = 0
        waiting_for_result = False
        while not self.closed:
            try:
                await self.sleep(delay)
                if self.closed:
                    return
                latest = await self.adapter.fetch(key)
                if self.closed:
                    return
                if latest is None:
                    if not owned:
                        self.log(f"{message_label(key)}: сообщение удалено; результат не подтверждён.")
                        return
                    if not waiting_for_result:
                        self.log(f"{message_label(key)}: сообщение недоступно; ожидаем подтверждение бота, нажатий нет.")
                    waiting_for_result = True
                    delay = self.uniform(self.settings.minimum, self.settings.maximum)
                    continue
                outcome = self.settings.outcome(latest.text)
                if outcome is not None:
                    self.confirm(key, outcome)
                    return
                if not self.settings.matches(latest):
                    if not owned:
                        self.log(f"{message_label(key)}: слово или кнопка исчезли; результат не подтверждён.")
                        return
                    if not waiting_for_result:
                        self.log(f"{message_label(key)}: слово или кнопка исчезли; ожидаем подтверждение бота, нажатий нет.")
                    waiting_for_result = True
                    delay = self.uniform(self.settings.minimum, self.settings.maximum)
                    continue
                self.request_labels[key] = request_tag_label(latest.text)
                waiting_for_result = False
                if not owned:
                    if not self.attempts.claim(key):
                        return
                    owned = True
                if self.settings.success_chat.strip():
                    response = await self.adapter.click(latest, self.settings.button.strip(), before_dispatch=self.arm_receipt)
                else:
                    response = await self.adapter.click(latest, self.settings.button.strip())
                if response is None or response.dispatched:
                    number += 1
                outcome = self.settings.outcome(response.text if response is not None else "")
                if outcome is not None:
                    self.confirm(key, outcome)
                    return
                if response is not None and not response.dispatched:
                    self.log(f"{message_label(key)}: состояние изменилось; ожидаем результат без нажатия.")
                else:
                    self.log(f"{message_label(key)}: попытка {number}, подтверждения пока нет.")
                delay = self.uniform(self.settings.minimum, self.settings.maximum)
            except asyncio.CancelledError:
                raise
            except RetryLater as error:
                delay = max(error.seconds, self.uniform(self.settings.minimum, self.settings.maximum))
                self.log(f"{message_label(key)}: {error.reason}; следующая проверка через {delay:.2f} с.")
            except Exception as error:
                self.log(f"{message_label(key)}: ошибка {type(error).__name__}; повторы остановлены, результат не подтверждён.")
                return

    async def close(self) -> None:
        self.closed = True
        tasks = list(self.pending.values())
        self.pending.clear()
        self.origins.clear()
        self.request_labels.clear()
        self.receipt_origins.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
