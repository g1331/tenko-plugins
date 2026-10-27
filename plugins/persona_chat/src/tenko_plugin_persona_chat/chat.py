"""会话窗口、请求组装、出站整形和用量记账；不依赖 Entari，方便单独测试。"""

from __future__ import annotations

import asyncio
import base64
import io
import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import zhconv
from PIL import Image, ImageOps
from pypinyin import lazy_pinyin

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
    shown: bool = False  # 图已经给模型看过：之后一直带着，不存盘
    sharp: bool = False  # 叫她来看的图用高清：低清读不出密密麻麻的小字，会照样编个数出来

    @property
    def image_tokens(self) -> int:
        """这条的图发给模型约占多少 token：实测低清一张约 190、高清约 1000，和原图大小关系不大。"""

        return len(self.images) * (1000 if self.sharp else 190)

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
    # 请求成功时记录里最后一条的编号；正在发的请求带到哪一条。被内容审核拒绝时，
    # 触发的只可能在这两者之间，撤掉这几条就行，前面的都审过了。
    passed: int = 0
    asked: int = 0
    last_message_at: float = 0.0
    last_unprompted_at: float = float("-inf")
    last_reply_at: float = float("-inf")
    spoken: deque[float] = field(default_factory=lambda: deque(maxlen=200))  # bot 每句话的时刻
    stickers: dict[str, str] | None = None  # 表情包目录快照，压缩时换新；None 是还没拍
    usage: dict[str, Any] = field(default_factory=dict)  # 上次请求的 usage，给“上下文”命令看
    compressed_at: float = 0.0
    scale: float = 1.4  # 实际 token ÷ 粗估，每次请求后按 usage 校准
    hint: str = ""  # 上次请求末尾的提示
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

    def her_words_before(self, line: Line) -> str:
        """``line`` 前面她连着说的几句。"""

        said: list[str] = []
        for other in reversed(self.lines):
            if other.number >= line.number:
                continue
            if other.speaker is not None:
                break
            said.insert(0, other.text)
        return " ".join(said)

    def drop_unchecked(self) -> list[Line]:
        """撤掉最近一次被拒的请求里新带上、还没审过的消息，返回撤掉的。"""

        dropped = [line for line in self.lines if self.passed < line.number <= self.asked]
        if dropped:
            kept = [line for line in self.lines if line not in dropped]
            self.lines.clear()
            self.lines.extend(kept)
        return dropped

    def by_number(self, number: object) -> Line | None:
        return next((line for line in self.lines if line.number == number), None)

    def called_in(self, text: str) -> tuple[Line | None, str]:
        """text 开头的“@昵称 ”对上窗口里说过话的人时，返回那人最近的一条和去掉前缀的 text。"""

        if text.startswith("@"):
            for line in reversed(self.lines):
                name = line.speaker.rpartition("(")[0] if line.speaker else ""
                rest = text.removeprefix(f"@{name}")
                if name and rest[:1].isspace() and rest.strip():
                    return line, rest.lstrip()
        return None, text

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
        """要给模型看图的消息：看过的一直带着，再加上最近还没看过的，新图总数不超过 ``budget``。

        看过的图要是因为来了新图又变回“[图片]”，从那条起的前缀缓存就全断了；
        它们随旧记录压进备忘时才离开。``budget`` 为 0 时一张都不带。
        """

        if budget <= 0:
            return []
        chosen: list[Line] = []
        for line in reversed(self.lines):
            if line.shown:
                chosen.append(line)
            elif line.images and len(line.images) <= budget:
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

    def retire_images(self, keep_tokens: int) -> int:
        """只留最近约 ``keep_tokens`` 的图，更早的退回“[图片]”，返回撤掉的看过的图占多少 token。

        只在压缩时调用：那时备忘刚改写，后面的缓存本来就要重算，撤图不多花钱；
        平时撤图会从那条起打断缓存。
        """

        retired = 0
        for line in reversed(self.lines):
            if keep_tokens > 0:
                keep_tokens -= line.image_tokens
            elif line.images:
                retired += line.image_tokens if line.shown else 0
                line.images, line.pictures, line.shown = (), None, False
        return retired

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
            "passed": self.passed,
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
        # 旧存盘没有 passed：之前都正常聊过来了，当作全审过。
        passed = data.get("passed", data["next_number"] - 1)
        conversation = cls(
            summary=data["summary"], next_number=data["next_number"], passed=passed, asked=passed
        )
        conversation.lines.extend(Line(images=(), **line) for line in data["lines"])
        return conversation

    def copy(self) -> Conversation:
        """沙盒用的副本：读得到当前窗口，写入不影响真实会话。"""

        clone = Conversation(
            self.size,
            next_number=self.next_number,
            summary=self.summary,
            place=self.place,
            passed=self.passed,
        )
        clone.lines.extend(self.lines)
        return clone


def build_messages(
    persona: str,
    conversation: Conversation,
    hint: str,
    images: int = 0,
    stickers: str = "",
    rules: str = RULES,
) -> list[dict[str, Any]]:
    """固定的两段 system 在前（命中前缀缓存），连续的群友消息合成一条 user。

    ``stickers`` 是表情包收藏目录，只在收藏增删时变，放在群名后面。

    看过的图和最近 ``images`` 张新图紧跟在所属消息后面，以低清晰度发给模型看。
    """

    shown = {line.number for line in conversation.with_images(images)}
    previous = 0.0
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": persona},
        {"role": "system", "content": rules},
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
            if url.startswith(_FRAMES):
                if blocks[-1]["type"] == "text":
                    blocks[-1]["text"] += _FRAMES_NOTE
                else:
                    blocks.append({"type": "text", "text": _FRAMES_NOTE})
                url = url.removeprefix(_FRAMES)
            detail = "high" if line.sharp else "low"
            blocks.append({"type": "image_url", "image_url": {"url": url, "detail": detail}})
        if blocks[-1]["type"] == "image_url":
            # 图后面紧接着下一个人的话，不标一句模型常把图算到下一个人头上。
            owner = f"（上面的图是 #{line.number} {line.speaker} 发的）"
            blocks.append({"type": "text", "text": owner})
    for message in messages:
        content = message["content"]
        if isinstance(content, list) and len(content) == 1 and content[0]["type"] == "text":
            message["content"] = content[0]["text"]
    messages.append({"role": "system", "content": hint})
    return messages


def context_parts(
    persona: str,
    tools: str,
    conversation: Conversation,
    stickers: str,
    hint: str,
    rules: str = RULES,
) -> dict[str, float]:
    """一次请求各部分的粗估 token（不含图），“上下文”命令按实际输入折算成准数。"""

    def rough(text: str) -> float:
        return estimate_tokens(text) - 12

    talk = sum(
        rough(m["content"])
        for m in build_messages(persona, conversation, hint, 0, stickers)
        if m["role"] != "system"
    )
    return {
        "人设、规则和工具": rough(persona) + rough(rules) + rough(tools),
        "群信息": rough(conversation.place),
        "表情包目录": rough(stickers),
        "备忘": rough(conversation.summary),
        "原文": talk,
        "末尾提示": rough(hint),
    }


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


# 动图抽帧拼成的图在 pictures 里带这个前缀，发请求时换成一句说明。
_FRAMES = "frames:"
_FRAMES_NOTE = "（下面这张是动图按时间顺序抽的几帧，从左到右、从上到下）"


def picture_url(data: bytes, most: int = 6) -> str | None:
    """给模型看的图：动图均匀抽至多 ``most`` 帧拼成一张 JPEG，其余原样转成 data URL。

    模型收到动图只看第一帧，动作和笑点常在后面；低清晰度下一张图的 token 基本固定，拼图不多花钱。
    """

    if data.startswith(b"GIF8"):
        try:
            with Image.open(io.BytesIO(data)) as gif:
                count = getattr(gif, "n_frames", 1)
                if count > 1:
                    return _FRAMES + data_url(_contact_sheet(gif, count, most))
        except (OSError, ValueError, Image.DecompressionBombError):
            pass  # 解不开就照原样发，模型至少看得到第一帧
    return data_url(_shrink(data))


def _shrink(data: bytes, side: int = 2048) -> bytes:
    """长边超过 ``side`` 或超过 2 MB 的照片缩小重存成 JPEG：手机原图动辄几千像素、七八兆，
    原样发过去又慢又可能被拒，高清晰度也只看这么大。解不开就原样返回。"""

    try:
        with Image.open(io.BytesIO(data)) as image:
            if len(data) <= 2 * 1024 * 1024 and max(image.size) <= side:
                return data
            image = ImageOps.exif_transpose(image).convert("RGB")  # 手机照片常靠 EXIF 标方向
            image.thumbnail((side, side))
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=85)
            return buffer.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError):
        return data


def _contact_sheet(gif: Image.Image, count: int, most: int) -> bytes:
    picks = (
        sorted({round(i * (count - 1) / (most - 1)) for i in range(most)})
        if count > most
        else range(count)
    )
    frames = []
    for index in picks:
        gif.seek(index)
        frame = gif.convert("RGBA")
        frame.thumbnail((256, 256))
        flat = Image.new("RGB", frame.size, "white")  # 透明处垫白，免得变成一片黑
        flat.paste(frame, mask=frame)
        frames.append(flat)
    width = max(f.width for f in frames)
    height = max(f.height for f in frames)
    columns = min(3, len(frames))
    rows = -(-len(frames) // columns)
    sheet = Image.new("RGB", (width * columns, height * rows), "white")
    for k, frame in enumerate(frames):
        sheet.paste(frame, ((k % columns) * width, (k // columns) * height))
    buffer = io.BytesIO()
    sheet.save(buffer, "JPEG", quality=85)
    return buffer.getvalue()


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


def banned(text: str, words: list[str]) -> str | None:
    """``text`` 里含的第一个违禁词。

    先统一成简体、半角、小写，去掉空格、标点和符号，“Ｌan Yin-Zhen.Shen”“藍 銀·真 神”都认得出来。
    词条读出来够长（拼音不少于 ``_SOUND_LETTERS`` 个字母）的再按拼音比一次，
    写成“lanyinzhenshen”就能拦下“蓝银真神”“懒音真神”“蓝yin真shen”；
    短的读音撞车太多（“西安”和“先”都是 xian），只按字面比。
    """

    plain = _bare(text)
    sound = None
    for word in words:
        if not (bare := _bare(word)):
            continue
        if bare in plain:
            return word
        if len(spelled := _spell(bare)) >= _SOUND_LETTERS:
            sound = sound if sound is not None else _spell(plain)
            if spelled in sound:
                return word
    return None


_SOUND_LETTERS = 8


def _bare(text: str) -> str:
    text = zhconv.convert(unicodedata.normalize("NFKC", text), "zh-cn").casefold()
    return "".join(ch for ch in text if ch.isalnum())


def _spell(text: str) -> str:
    """不带声调的拼音连写，汉字以外的照原样。"""

    return "".join(lazy_pinyin(text))


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


_RESET = "reset:"


class UsageBook:
    """按天记 token 和估算费用：{日期: {会话键 或 "*": [未命中, 命中, 输出, 美元]}}，保留 7 天。

    重置额度时当天另记一笔 ``"reset:会话键": 当时已计的 token``，额度只算之后的，用量照旧统计。
    """

    def __init__(self, store: Any) -> None:
        self.store = store

    def tokens(self, day: date, key: str) -> int:
        """计入每日上限的 token：未命中缓存的输入加输出；命中缓存的只按 2% 计价，不算。"""

        today = self.store.load(default={}).get(day.isoformat(), {})
        row = today.get(key)
        return int(row[0] + row[2]) - today.get(_RESET + key, 0) if row else 0

    def reset(self, day: date, key: str) -> int:
        """从现在起重新计 ``key`` 当天的额度，返回清掉了多少。"""

        used = self.tokens(day, key)
        if used:
            data = self.store.load(default={})
            today = data[day.isoformat()]
            today[_RESET + key] = today.get(_RESET + key, 0) + used
            self.store.save(data)
        return used

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
                if key.startswith(_RESET):
                    continue
                week[key] = [a + b for a, b in zip(week.get(key, [0, 0, 0, 0.0]), row, strict=True)]
        today = {k: v for k, v in data.get(day.isoformat(), {}).items() if not k.startswith(_RESET)}
        return [("今日", today), ("近 7 天", week)]
