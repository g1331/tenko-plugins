"""提醒的登记、校验和到点挑选；插件包只能在宿主加载后导入，所以经 module fixture 取子模块。"""

from datetime import datetime

import pytest


class MemoryStore:
    def __init__(self):
        self.data = None

    def load(self, default):
        return self.data if self.data is not None else default

    def save(self, data):
        self.data = data


@pytest.fixture
def r(module):
    return module.reminders


NOW = datetime(2026, 11, 2, 1, 0).timestamp()


def add(book, at="2026-11-02 09:00", user="13", key="group:1", now=NOW):
    return book.add(key, at=at, user=user, name="13", note="叫他起床", now=now)


def test_parse_at_reads_local_time(r):
    assert r.parse_at("2026-11-02 09:00") == datetime(2026, 11, 2, 9, 0).timestamp()
    assert r.parse_at("2026-11-02T09:00:30") == datetime(2026, 11, 2, 9, 0, 30).timestamp()
    assert r.parse_at("明天九点") is None
    assert r.parse_at(None) is None


def test_add_checks_time_and_limits(r):
    book = r.Reminders(MemoryStore())
    item, why = add(book)
    assert why == "" and item["id"] == 1 and item["note"] == "叫他起床"
    assert add(book, at="2026-11-02 00:30")[0] is None  # 已经过了
    assert add(book, at="2026-11-02 01:00")[0] is None  # 太近
    assert add(book, at="2026-11-10 09:00")[0] is None  # 超过七天
    assert add(book, at="明早")[0] is None
    assert add(book, at="2026-11-02 10:00")[0] is not None
    assert add(book, at="2026-11-02 11:00")[1] == "这个人挂着的提醒满了"
    for user in ("a", "b", "c"):
        assert add(book, user=user)[0] is not None
    assert add(book, user="d")[1] == "这个会话挂着的提醒满了"
    assert add(book, key="group:2")[0] is not None  # 别的会话另算


def test_daily_limit_counts_fired_ones_too(r):
    book = r.Reminders(MemoryStore())
    for i in range(r.DAILY):
        item, _ = add(book, user=str(i))
        book.drop(item["id"])  # 提醒过了也算今天登记过
    assert add(book, user="x")[1] == "今天登记得太多"
    assert add(book, user="x", now=NOW + 86400, at="2026-11-03 09:00")[0] is not None


def test_cancel_due_and_drop(r):
    book = r.Reminders(MemoryStore())
    first, _ = add(book, at="2026-11-02 09:00")
    second, _ = add(book, at="2026-11-02 08:00", user="2")
    assert [x["id"] for x in book.pending("group:1")] == [second["id"], first["id"]]
    assert book.cancel("group:2", first["id"]) is None  # 别的会话取消不了
    assert book.cancel("group:1", str(first["id"]))["note"] == "叫他起床"
    assert book.due(datetime(2026, 11, 2, 7, 59).timestamp()) == []
    assert [x["id"] for x in book.due(datetime(2026, 11, 2, 8, 0).timestamp())] == [second["id"]]
    book.drop(second["id"])
    assert book.pending("group:1") == []


def test_when_says_today_tomorrow_or_date(r):
    now = datetime(2026, 11, 2, 1, 0).timestamp()
    assert r.when(datetime(2026, 11, 2, 9, 0).timestamp(), now) == "今天 09:00"
    assert r.when(datetime(2026, 11, 3, 9, 0).timestamp(), now) == "明天 09:00"
    assert r.when(datetime(2026, 11, 6, 9, 0).timestamp(), now) == "11-06 09:00"


def test_same_person_same_time_counts_once(r):
    book = r.Reminders(MemoryStore())
    first, _ = book.add("group:1", at="2026-11-02 09:00", user="13", name="13", note="", now=NOW)
    again, why = add(book, at="2026-11-02 09:00")  # 第二遍补上了备注
    assert why == "已经登记过" and again["id"] == first["id"]
    (only,) = book.pending("group:1")
    assert only["note"] == "叫他起床"
    assert add(book, at="2026-11-02 09:30")[0]["id"] != first["id"]  # 不同时刻另算
