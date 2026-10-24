"""会话窗口、请求组装、出站整形和用量记账；不依赖 Entari，方便单独测试。"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .persona import RULES, SUMMARY_PROMPT

GLOBAL = "*"
_TIME_GAP = 300
_SENTENCE_ENDS = "。！？!?…~～"


@dataclass(slots=True)
class Line:
    number: int
    message_id: str | None
    speaker: str | None  # “昵称(QQ号)”；None 是 bot 自己
    text: str
    images: tuple[str, ...] = ()  # 图片原地址
    pictures: list[str] | None = None  # 下载好的 data URL；None 表示还没下载
    at: float = 0.0  # 收到的时刻（Unix 时间）
    role: str = ""  # 群身份，例如“群主”；普通成员为空
    sticker: tuple[str, str] | None = None  # 还没收藏的表情包：(QQ 文件名, 原地址)

    @property
    def who(self) -> str:
        """给模型看的发言人：昵称(QQ号)，有身份时跟在后面。"""

        return f"{self.speaker} {self.role}" if self.role else f"{self.speaker}"


@dataclass
class Conversation:
    """一个群或一个私聊的聊天记录；用 :meth:`dump` / :meth:`restore` 存盘，重启后接着聊。

    记录一直往后追加，请求的前缀因此保持不变、能命中缓存；超过 token 阈值后
    较早的部分由 ``compress`` 压成 ``summary``。``size`` 只是防止一直没有请求时无限增长的保险。
    """

    size: int = 1000
    summary: str = ""
    place: str = ""  # 在哪儿聊，例如“你现在在 QQ 群「车万」里。”
    lines: deque[Line] = field(init=False)
    next_number: int = 1
    unseen: int = 0  # 上次请求之后新进来的群友消息数
    last_message_at: float = 0.0
    last_unprompted_at: float = float("-inf")
    last_reply_at: float = float("-inf")
    spoken: deque[float] = field(default_factory=lambda: deque(maxlen=200))  # bot 每句话的时刻
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self.lines = deque(maxlen=self.size)

    def add(
        self,
        message_id: str | None,
        speaker: str | None,
        text: str,
        images: tuple[str, ...] = (),
        role: str = "",
        sticker: tuple[str, str] | None = None,
        counted: bool = True,
    ) -> Line:
        """``counted`` 为假的不算新消息，例如 Tenko 功能的回复：不催她插话。"""

        line = Line(
            self.next_number,
            message_id,
            speaker,
            text,
            images,
            at=time.time(),
            role=role,
            sticker=sticker,
        )
        self.next_number += 1
        self.lines.append(line)
        if speaker is not None and counted:
            self.unseen += 1
        return line

    def by_number(self, number: object) -> Line | None:
        return next((line for line in self.lines if line.number == number), None)

    def by_message_id(self, message_id: str | None) -> Line | None:
        if not message_id:
            return None
        return next((line for line in self.lines if line.message_id == message_id), None)

    def name_of(self, user_id: str) -> str | None:
        """窗口里这个 QQ 号最近用的昵称。"""

        suffix = f"({user_id})"
        for line in reversed(self.lines):
            if line.speaker and line.speaker.endswith(suffix):
                return line.speaker.removesuffix(suffix)
        return None

    def with_images(self, budget: int) -> list[Line]:
        """最近几条带图的消息，图片总数不超过 ``budget``；更早的图只留“[图片]”字样。"""

        chosen: list[Line] = []
        for line in reversed(self.lines):
            if line.images and len(line.images) <= budget:
                chosen.append(line)
                budget -= len(line.images)
        return chosen

    def older_than(self, keep_tokens: int) -> list[Line]:
        """压缩时要并进摘要的较早记录：从最新往前留够约 ``keep_tokens``，至少留 10 条。"""

        kept, total = 0, 0.0
        for line in reversed(self.lines):
            total += estimate_tokens(line.text)
            if kept >= 10 and total > keep_tokens:
                break
            kept += 1
        return list(self.lines)[: len(self.lines) - kept]

    def forget(self, upto: int, summary: str) -> None:
        """删掉编号不超过 ``upto`` 的记录，换成新摘要。"""

        while self.lines and self.lines[0].number <= upto:
            self.lines.popleft()
        self.summary = summary

    def fatigue(self, now: float, window: float = 600) -> int:
        """最近 ``window`` 秒里 bot 说了几句：说得越多越累，不说话就慢慢缓过来。"""

        return sum(1 for at in self.spoken if now - at < window)

    def sent_recently(self, text: str, window: float = 1800) -> bool:
        """bot 最近 ``window`` 秒里说过的话里有没有 ``text``，例如同一张表情包。"""

        now = time.time()
        return any(
            line.speaker is None and text in line.text and now - line.at < window
            for line in self.lines
        )

    def buried(self, line: Line) -> bool:
        """``line`` 之后有没有别人说过话；没有的话回它不用引用，一看就知道在接谁。"""

        return any(other.number > line.number and other.speaker for other in self.lines)

    def dump(self) -> dict[str, Any]:
        """存盘的内容：图片地址会过期、计时用的是进程内的时钟，都不存。"""

        return {
            "summary": self.summary,
            "next_number": self.next_number,
            "lines": [
                {
                    "number": line.number,
                    "message_id": line.message_id,
                    "speaker": line.speaker,
                    "text": line.text,
                    "at": line.at,
                    "role": line.role,
                }
                for line in self.lines
            ],
        }

    @classmethod
    def restore(cls, data: dict[str, Any]) -> Conversation:
        conversation = cls(summary=data["summary"], next_number=data["next_number"])
        conversation.lines.extend(Line(images=(), **line) for line in data["lines"])
        return conversation

    def copy(self) -> Conversation:
        """沙盒用的副本：读得到当前窗口，写入不影响真实会话。"""

        clone = Conversation(
            self.size, next_number=self.next_number, summary=self.summary, place=self.place
        )
        clone.lines.extend(self.lines)
        return clone


def build_messages(
    persona: str, conversation: Conversation, hint: str, images: int = 0, stickers: str = ""
) -> list[dict[str, Any]]:
    """固定的两段 system 在前（命中前缀缓存），连续的群友消息合成一条 user。

    ``stickers`` 是表情包收藏目录，只在收藏增删时变，放在群名后面。

    最近 ``images`` 张已下载的图片紧跟在所属消息后面，以低清晰度发给模型看。
    """

    shown = {line.number for line in conversation.with_images(images)}
    previous = 0.0
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": persona},
        {"role": "system", "content": RULES},
    ]
    if conversation.place:
        messages.append({"role": "system", "content": conversation.place})
    if stickers:
        messages.append({"role": "system", "content": stickers})
    if conversation.summary:
        memo = f"更早的聊天备忘（这些消息已经没有编号，不能 reply_to）：\n{conversation.summary}"
        messages.append({"role": "system", "content": memo})
    for line in conversation.lines:
        # 像 QQ 一样，隔了一阵才标一次时间：省 token，标记由记录本身决定，不影响前缀缓存。
        gap, previous = line.at - previous, line.at
        if line.speaker is None:
            # 分几条连着发的话合成一条 assistant，保持一问一答交替。
            if messages[-1]["role"] == "assistant":
                messages[-1]["content"] += "\n" + line.text
            else:
                messages.append({"role": "assistant", "content": line.text})
            continue
        if messages[-1]["role"] != "user":
            messages.append({"role": "user", "content": []})
        blocks = messages[-1]["content"]
        text = f"#{line.number} [{line.who}] {line.text}"
        if gap >= _TIME_GAP:
            text = f"—— {datetime.fromtimestamp(line.at):%m-%d %H:%M} ——\n{text}"
        if blocks and blocks[-1]["type"] == "text":
            blocks[-1]["text"] += "\n" + text
        else:
            blocks.append({"type": "text", "text": text})
        for url in (line.pictures or []) if line.number in shown else []:
            blocks.append({"type": "image_url", "image_url": {"url": url, "detail": "low"}})
    for message in messages:
        content = message["content"]
        if isinstance(content, list) and len(content) == 1 and content[0]["type"] == "text":
            message["content"] = content[0]["text"]
    messages.append({"role": "system", "content": hint})
    return messages


def estimate_tokens(text: str) -> float:
    """官方给的粗略换算：汉字约 0.6 token、其他字符约 0.3，每行另加编号和昵称的开销。"""

    wide = sum(1 for char in text if ord(char) > 0x2E7F)
    return wide * 0.6 + (len(text) - wide) * 0.3 + 12


def summary_messages(summary: str, lines: list[Line]) -> list[dict[str, str]]:
    """把旧摘要和要并进去的记录交给模型，写一份新的备忘。"""

    log = "\n".join(
        f"#{line.number} [{line.who if line.speaker else '你'}] {line.text}" for line in lines
    )
    old = f"之前的备忘：\n{summary}\n\n" if summary else ""
    return [
        {"role": "system", "content": SUMMARY_PROMPT},
        {"role": "user", "content": f"{old}要并进去的聊天记录：\n{log}"},
    ]


_IMAGE_MAGIC = ((b"\xff\xd8\xff", "jpeg"), (b"\x89PNG", "png"), (b"GIF8", "gif"))


def data_url(data: bytes) -> str | None:
    """按文件头认出 JPEG/PNG/GIF/WebP（DeepSeek 只收这几种），转成 data URL。"""

    kind = next((k for magic, k in _IMAGE_MAGIC if data.startswith(magic)), None)
    if kind is None and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind = "webp"
    return kind and f"data:image/{kind};base64,{base64.b64encode(data).decode()}"


def split(text: str, limit: int, most: int = 3) -> list[str]:
    """按换行拆成至多 ``most`` 条分开发，多出来的并进最后一条；每条各自整形。"""

    pieces = [piece for part in text.splitlines() if (piece := shape(part, limit))]
    if len(pieces) > most:
        pieces[most - 1 :] = [shape(" ".join(pieces[most - 1 :]), limit)]
    return pieces


def shape(text: str, limit: int) -> str:
    """压成一段、去掉 @全体和模型偶尔照抄的“#编号”前缀，超长时尽量在句末截断。"""

    text = " ".join(text.replace("@全体成员", "").split())
    text = re.sub(r"^#\d+\s*", "", text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(mark) for mark in _SENTENCE_ENDS)
    return cut[: end + 1] if end >= limit // 2 else cut.rstrip() + "…"


# deepseek-flash 每 1M token 的闲时美元价：缓存未命中输入、命中输入、输出。
_PRICES = (0.15, 0.003, 0.6)


def estimate_cost(usage: dict[str, Any], now: datetime) -> float:
    """按官方价目估算；工作日 UTC 01-04、06-10 点为高峰，价格翻倍。

    shortcut: 不认中国法定节假日（官方按闲时计价），节假日的估算会偏高。
    """

    now = now.astimezone(UTC)
    peak = now.weekday() < 5 and (1 <= now.hour < 4 or 6 <= now.hour < 10)
    tokens = (
        usage.get("prompt_cache_miss_tokens", 0),
        usage.get("prompt_cache_hit_tokens", 0),
        usage.get("completion_tokens", 0),
    )
    cost = sum(count * price for count, price in zip(tokens, _PRICES, strict=True)) / 1e6
    return cost * 2 if peak else cost


class UsageBook:
    """按天记 token 和估算费用：{日期: {会话键 或 "*": [未命中, 命中, 输出, 美元]}}，保留 7 天。"""

    def __init__(self, store: Any) -> None:
        self.store = store

    def tokens(self, day: date, key: str) -> int:
        """计入每日上限的 token：未命中缓存的输入加输出；命中缓存的只按 2% 计价，不算。"""

        row = self.store.load(default={}).get(day.isoformat(), {}).get(key)
        return int(row[0] + row[2]) if row else 0

    def add(self, day: date, key: str, usage: dict[str, Any], cost: float) -> None:
        data = self.store.load(default={})
        today = data.setdefault(day.isoformat(), {})
        delta = (
            usage.get("prompt_cache_miss_tokens", 0),
            usage.get("prompt_cache_hit_tokens", 0),
            usage.get("completion_tokens", 0),
            cost,
        )
        for name in (GLOBAL, key):
            row = today.setdefault(name, [0, 0, 0, 0.0])
            today[name] = [a + b for a, b in zip(row, delta, strict=True)]
        oldest = (day - timedelta(days=6)).isoformat()
        self.store.save({k: v for k, v in data.items() if k >= oldest})

    def summary(self, day: date) -> list[tuple[str, dict[str, list[float]]]]:
        """今天和近 7 天各会话的合计，键按用量从大到小。"""

        data = self.store.load(default={})
        week: dict[str, list[float]] = {}
        for rows in data.values():
            for key, row in rows.items():
                week[key] = [a + b for a, b in zip(week.get(key, [0, 0, 0, 0.0]), row, strict=True)]
        return [("今日", data.get(day.isoformat(), {})), ("近 7 天", week)]
