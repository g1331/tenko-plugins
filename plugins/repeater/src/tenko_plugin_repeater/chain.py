"""复读判定：每个群记住最近一串相同的消息，决定跟一句、打断，还是不出声。

这里不碰 Entari：输入是已经拆好的 :class:`Said`，输出是要发出去的元素。
"""

from __future__ import annotations

import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

# 拉取图片并返回内容摘要；拉不到时返回 None，这条消息就不会被当成复读。
Digest = Callable[[str], Awaitable[str | None]]


@dataclass(slots=True)
class Said:
    """一条可以参与复读的消息。

    ``key`` 是文字、QQ 表情和商城表情拼成的比较键；纯图片消息的 ``key`` 为空，
    ``image`` 是图片地址，比较时按图片内容判断。``elements`` 是复读时原样发回的元素。
    """

    user: str
    key: str | None
    elements: Sequence[Any]
    text: str = ""
    image: str | None = None


@dataclass(slots=True)
class Run:
    first: Said
    users: set[str] = field(default_factory=set)
    acted: bool = False


@dataclass(slots=True)
class Settings:
    min_times: int = 2
    repeat_probability: float = 0.15
    interrupt_probability: float = 0.05
    cooldown_seconds: float = 600
    interrupt_lines: Sequence[str] = ("打断！",)


class Repeater:
    def __init__(
        self,
        settings: Settings,
        digest: Digest,
        *,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.digest = digest
        self.rng = rng or random.Random()
        self.clock = clock
        self.runs: dict[str, Run] = {}
        self.cooldown_until: dict[str, float] = {}
        self._digests: dict[str, str | None] = {}

    def forget(self, group: str) -> None:
        """不能参与复读的消息（命令、长文、转发等）把这一串断开。"""

        self.runs.pop(group, None)

    async def feed(self, group: str, said: Said) -> list[Any] | None:
        """记下一条消息；需要出声时返回要发送的元素。"""

        run = self.runs.get(group)
        if run is None or not await self._same(run.first, said):
            self.runs[group] = Run(said, {said.user})
            return None
        if said.user in run.users:
            return None  # 同一个人连发不算复读
        run.users.add(said.user)
        if run.acted or self.clock() < self.cooldown_until.get(group, 0):
            return None
        times = len(run.users)
        if times < self.settings.min_times:
            return None
        reply = self._decide(run.first, times)
        if reply is not None:
            run.acted = True
            self.cooldown_until[group] = self.clock() + self.settings.cooldown_seconds
        return reply

    def _decide(self, said: Said, times: int) -> list[Any] | None:
        s = self.settings
        if self.rng.random() < s.interrupt_probability:
            return self._interrupt(said)
        # 每多一条复读，“不跟”的概率乘以 (1 - p)：复读越久越容易跟，起步很克制。
        chance = 1 - (1 - s.repeat_probability) ** (times - s.min_times + 1)
        if self.rng.random() < chance:
            return list(said.elements)
        return None

    def _interrupt(self, said: Said) -> list[Any]:
        chars = list(said.text)
        if said.key is not None and len(set(chars)) >= 3:
            shuffled = chars[:]
            while shuffled == chars:
                self.rng.shuffle(shuffled)
            return ["".join(shuffled)]
        return [self.rng.choice(list(self.settings.interrupt_lines) or ["打断！"])]

    async def _same(self, a: Said, b: Said) -> bool:
        if a.image is None or b.image is None:
            return a.image is None and b.image is None and a.key == b.key
        first, second = await self._digest(a.image), await self._digest(b.image)
        return first is not None and first == second

    async def _digest(self, src: str) -> str | None:
        if src not in self._digests:
            if len(self._digests) >= 64:  # 只需要记住最近几张图
                self._digests.pop(next(iter(self._digests)))
            self._digests[src] = await self.digest(src)
        return self._digests[src]
