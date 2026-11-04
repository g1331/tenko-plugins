"""她答应到点做的事：登记、到点交给插件叫她开口、列给她看。

11-02 凌晨 bot主人说“明早叫我起床”，她答应“九点肯定叫你”，可插件没有定时的本事，
十点半人家来问“为啥你没叫我”。现在答应时调用 remind 登记，到点插件叫她 @ 那个人说一句。
只提醒一次：Muse、AI Studio 的跟进也是发一次、没人回就停。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

MIN_AHEAD = 60  # 至少一分钟以后：再近的直接说就行
HORIZON = 7 * 86400  # 最远七天
PER_CONVERSATION = 5  # 一个会话同时挂着的
PER_PERSON = 2  # 同一个人同时挂着的
DAILY = 10  # 一个会话一天里新登记的
LATE = 1800  # 重启、掉线错过了时间：晚这么久以内照发，再晚就作废
NOTE_CHARS = 30
SAID_CHARS = 80
_DAY = 86400
_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S")


def parse_at(text: object) -> float | None:
    """模型填的本地时间“2026-11-03 09:00”换成时间戳；格式不对返回 ``None``。"""

    text = str(text or "").strip()
    for fmt in _FORMATS:
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return None


class Reminders:
    """存盘：{"next_id", "items": [提醒], "made": {会话: [登记时刻]}}。

    一条提醒：{"id", "key", "due", "user", "name", "note", "said", "number",
    "self_id", "platform", "origin", "created"}；``origin`` 是登记时那条消息的原始事件，
    到点没有群友消息可接时，靠它重建会话把话发出去。
    """

    def __init__(self, store: Any) -> None:
        self.store = store

    def _data(self) -> dict[str, Any]:
        data = self.store.load(default={})
        data.setdefault("next_id", 1)
        data.setdefault("items", [])
        data.setdefault("made", {})
        return data

    def pending(self, key: str) -> list[dict[str, Any]]:
        return sorted(
            (item for item in self._data()["items"] if item["key"] == key),
            key=lambda item: item["due"],
        )

    def add(
        self,
        key: str,
        *,
        at: object,
        user: str,
        name: str,
        note: str,
        said: str = "",
        number: int | None = None,
        self_id: str = "",
        platform: str = "",
        origin: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """登记一条，返回 (提醒, 不收的原因)；收下时原因为空。"""

        now = time.time() if now is None else now
        due = parse_at(at)
        if due is None:
            return None, f"时间看不懂：{at!r}"
        if due - now < MIN_AHEAD:
            return None, "时间已经过了或者太近"
        if due - now > HORIZON:
            return None, "超过七天"
        data = self._data()
        mine = [item for item in data["items"] if item["key"] == key]
        note = " ".join(str(note).split())[:NOTE_CHARS]
        # 同一轮里她常把同一个提醒登记两遍（第二遍不写备注）：同一个人、同一时刻的算一条。
        same = next(
            (x for x in mine if x["user"] == user and abs(x["due"] - due) < MIN_AHEAD), None
        )
        if same is not None:
            if note and not same["note"]:
                same["note"] = note
                self.store.save(data)
            return same, ""
        if len(mine) >= PER_CONVERSATION:
            return None, "这个会话挂着的提醒满了"
        if sum(item["user"] == user for item in mine) >= PER_PERSON:
            return None, "这个人挂着的提醒满了"
        made = [t for t in data["made"].get(key, []) if now - t < _DAY]
        if len(made) >= DAILY:
            return None, "今天登记得太多"
        item = {
            "id": data["next_id"],
            "key": key,
            "due": due,
            "user": user,
            "name": name,
            "note": note,
            "said": " ".join(str(said).split())[:SAID_CHARS],
            "number": number,
            "self_id": self_id,
            "platform": platform,
            "origin": origin,
            "created": now,
        }
        data["next_id"] += 1
        data["items"].append(item)
        data["made"][key] = [*made, now]
        self.store.save(data)
        return item, ""

    def cancel(self, key: str, rid: object) -> dict[str, Any] | None:
        """取消本会话的一条，返回被取消的；编号不对返回 ``None``。"""

        data = self._data()
        found = next(
            (item for item in data["items"] if item["key"] == key and str(item["id"]) == str(rid)),
            None,
        )
        if found is not None:
            data["items"].remove(found)
            self.store.save(data)
        return found

    def due(self, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        return sorted(
            (item for item in self._data()["items"] if item["due"] <= now),
            key=lambda item: item["due"],
        )

    def drop(self, rid: object) -> None:
        data = self._data()
        data["items"] = [item for item in data["items"] if str(item["id"]) != str(rid)]
        self.store.save(data)


def when(due: float, now: float) -> str:
    """给她看的时间：今天、明天、后天说“明天 09:00”，再远写日期。"""

    moment = datetime.fromtimestamp(due)
    days = (moment.date() - datetime.fromtimestamp(now).date()).days
    day = {0: "今天", 1: "明天", 2: "后天"}.get(days, f"{moment:%m-%d}")
    return f"{day} {moment:%H:%M}"
