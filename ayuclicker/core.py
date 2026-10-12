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
    terms: tuple[str, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        terms = tuple(term.strip() for term in self.word.split(",") if term.strip())
        if not terms or not self.button.strip():
            raise ValueError("Укажите хотя бы одно слово / фразу и название кнопки.")
        object.__setattr__(self, "terms", terms)
        if not all(math.isfinite(value) for value in (self.minimum, self.maximum)):
            raise ValueError("Задержки должны быть конечными числами.")
        if not 0 <= self.minimum <= self.maximum:
            raise ValueError("Нужен диапазон: 0 ≤ минимум ≤ максимум секунд.")

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

    @property
    def key(self) -> tuple[int, int]:
        return self.chat_id, self.message_id


class Adapter(Protocol):
    async def fetch(self, key: tuple[int, int]) -> Message | None: ...

    async def click(self, message: Message, title: str) -> None: ...


class AttemptStore(Protocol):
    def contains(self, key: tuple[int, int]) -> bool: ...

    def claim(self, key: tuple[int, int]) -> bool: ...


def message_label(key: tuple[int, int]) -> str:
    return f"Чат {key[0]}, сообщение {key[1]}"


class Clicker:
    """One pending timer and at most one click attempt per message.

    Reserve the attempt before the remote request: a network timeout may happen
    after Telegram accepted a click, so automatically retrying is ambiguous.
    """

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
        self.closed = False

    def observe(self, message: Message) -> None:
        key = message.key
        if self.closed or self.attempts.contains(key):
            return
        if not self.settings.matches(message):
            self.remove(key)
            return
        if key in self.pending:
            return
        delay = self.uniform(self.settings.minimum, self.settings.maximum)
        self.log(f"{message_label(key)}: нажатие через {delay:.2f} с.")
        task = asyncio.create_task(self._run(key, delay))
        self.pending[key] = task
        task.add_done_callback(lambda done: self._finished(key, done))

    def remove(self, key: tuple[int, int]) -> None:
        task = self.pending.pop(key, None)
        if task is not None:
            task.cancel()
            self.log(f"{message_label(key)}: ожидающее нажатие отменено.")

    def _finished(self, key: tuple[int, int], task: asyncio.Task[None]) -> None:
        if self.pending.get(key) is task:
            self.pending.pop(key, None)
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
            self.log(f"{message_label(key)}: кнопка нажата.")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Do not log exception text: library errors can contain credentials.
            self.log(
                f"{message_label(key)}: ошибка {type(error).__name__}. "
                "Повторное нажатие автоматически не выполняется."
            )

    async def close(self) -> None:
        self.closed = True
        tasks = list(self.pending.values())
        self.pending.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
