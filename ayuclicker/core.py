"""Matching and scheduling, independent of Telegram and the desktop UI."""

from __future__ import annotations

import asyncio
import math
import random
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol


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
    terms: tuple[str, ...] = field(init=False, repr=False)
    mine_markers: tuple[str, ...] = field(init=False, repr=False)
    other_markers: tuple[str, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        terms = tuple(term.strip() for term in self.word.split(",") if term.strip())
        if not terms or not self.button.strip():
            raise ValueError("Укажите хотя бы одно слово / фразу и название кнопки.")
        object.__setattr__(self, "terms", terms)
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
            if not self.mine_markers or not self.other_markers:
                raise ValueError("Для повторов укажите фразы результата: «Забрал я» и «Забрал другой».")
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
        if self.whole_word:
            alternatives = "|".join(re.escape(term) for term in self.terms)
            pattern = r"(?<!\w)(?:" + alternatives + r")(?!\w)"
            contains_word = re.search(pattern, message.text, re.IGNORECASE) is not None
        else:
            text = message.text.casefold()
            contains_word = any(term.casefold() in text for term in self.terms)
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

    async def click(self, message: Message, title: str) -> ClickResponse: ...


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
        self.closed = False

    def observe(self, message: Message) -> None:
        key = message.key
        if self.settings.repeat_until_result and key in self.pending:
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
        task.add_done_callback(lambda done: self._finished(key, done))

    def remove(self, key: tuple[int, int]) -> None:
        task = self.pending.pop(key, None)
        self.origins.pop(key, None)
        if task is not None:
            task.cancel()
            self.log(f"{message_label(key)}: ожидающее нажатие отменено.")

    def confirm(self, key: tuple[int, int], outcome: str) -> bool:
        if outcome not in ("mine", "other") or key not in self.pending:
            return False
        if not self.attempts.contains(key):
            if not self.attempts.claim(key):
                return False
        self.attempts.complete(key, outcome)
        label = "подтверждено: забрал я" if outcome == "mine" else "подтверждено: забрал другой"
        self.log(f"{message_label(key)}: {label}. Повторы остановлены.")
        task = self.pending.pop(key)
        self.origins.pop(key, None)
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

    def _finished(self, key: tuple[int, int], task: asyncio.Task[None]) -> None:
        if self.pending.get(key) is task:
            self.pending.pop(key, None)
            self.origins.pop(key, None)
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
                waiting_for_result = False
                if not owned:
                    if not self.attempts.claim(key):
                        return
                    owned = True
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
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
