"""Conservative scheduling for two explicitly selected screen regions."""

from dataclasses import dataclass
import hashlib
import random

from .core import Message, Settings


def normalize(text: str) -> str:
    return " ".join(text.split())


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    def valid(self) -> bool:
        return self.width >= 8 and self.height >= 8

    def intersects(self, other: "Rect") -> bool:
        return (
            self.x < other.x + other.width and other.x < self.x + self.width
            and self.y < other.y + other.height and other.y < self.y + self.height
        )

    def inside(self, other: "Rect") -> bool:
        return (
            self.x >= other.x and self.y >= other.y
            and self.x + self.width <= other.x + other.width
            and self.y + self.height <= other.y + other.height
        )

    def offset(self, x: int, y: int) -> "Rect":
        return Rect(self.x + x, self.y + y, self.width, self.height)


@dataclass(frozen=True)
class Reading:
    text: str
    button: str

    @property
    def identity(self) -> str:
        content = normalize(self.text).casefold() + "\0" + normalize(self.button).casefold()
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def matches(self, settings: Settings) -> bool:
        return settings.matches(Message(0, 0, normalize(self.text), (normalize(self.button),)))


@dataclass(frozen=True)
class Decision:
    kind: str
    delay: float = 0


class ScreenSchedule:
    """Require two identical OCR reads and reserve one attempt per text.

    Screen OCR cannot identify Telegram message IDs. Identical text is therefore
    conservatively treated as the same message until this app is restarted.
    """

    def __init__(self, settings: Settings, seen: set[str], uniform=random.uniform):
        self.settings = settings
        self.seen = seen
        self.uniform = uniform
        self.candidate = None
        self.pending = None
        self.deadline = 0.0
        self.ready = False
        self.locked = False
        self.absent_reads = 0

    def cancel(self) -> Decision:
        had_pending = self.pending is not None
        self.candidate = self.pending = None
        self.ready = False
        return Decision("cancelled" if had_pending else "idle")

    def observe(self, reading: Reading | None, now: float) -> Decision:
        # Losing focus is not evidence that the message disappeared.
        if reading is None:
            return self.cancel()
        matches = reading.matches(self.settings)
        if self.locked:
            self.absent_reads = 0 if matches else self.absent_reads + 1
            if self.absent_reads >= 2:
                self.locked = False
                self.absent_reads = 0
            return self.cancel()
        if not matches or reading.identity in self.seen:
            return self.cancel()
        identity = reading.identity
        if self.pending is not None:
            if identity != self.pending:
                result = self.cancel()
                self.candidate = identity
                return result
            self.ready = now >= self.deadline
            return Decision("ready" if self.ready else "waiting")
        if identity != self.candidate:
            self.candidate = identity
            return Decision("stabilizing")
        delay = self.uniform(self.settings.minimum, self.settings.maximum)
        self.pending = identity
        self.deadline = now + delay
        self.ready = False
        return Decision("scheduled", delay)

    def claim(self, reading: Reading) -> bool:
        if (
            not self.ready or not reading.matches(self.settings)
            or reading.identity != self.pending
            or reading.identity in self.seen
        ):
            return False
        self.seen.add(reading.identity)
        self.locked = True
        self.absent_reads = 0
        self.candidate = self.pending = None
        self.ready = False
        return True
