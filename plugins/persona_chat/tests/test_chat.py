"""纯逻辑；插件包只能在宿主加载后导入，所以经 module fixture 取子模块。"""

import re
from datetime import UTC, date, datetime

import pytest


@pytest.fixture
def chat(module):
    return module.chat


def untimed(text):
    """去掉开头的“—— 月-日 时:分 ——”时间标记。"""

    return re.sub(r"^—— .+? ——\n", "", text)


class MemoryStore:
    def __init__(self):
        self.data = None

    def load(self, default):
        return self.data if self.data is not None else default

    def save(self, data):
        self.data = data


def test_messages_keep_fixed_prefix_and_merge_consecutive_lines(chat):
    conversation = chat.Conversation(3)
    conversation.add("m1", "甲(1)", "早")
    conversation.add("m2", "乙(2)", "大肥鱼在吗")
    conversation.add("b1", None, "在，饭呢")
    conversation.add("m3", "甲(1)", "没饭")  # 窗口只留 3 条，挤掉 #1

    messages = chat.build_messages("人设", conversation, "提示")

    assert [m["role"] for m in messages] == [
        "system",
        "system",
        "user",
        "assistant",
        "user",
        "system",
    ]
    assert messages[0]["content"] == "人设"
    assert untimed(messages[2]["content"]) == "#2 [乙(2)] 大肥鱼在吗"
    assert messages[4]["content"] == "#4 [甲(1)] 没饭"
    assert messages[-1]["content"] == "提示"
    assert conversation.unseen == 3  # bot 自己的话不计数


def test_shape_flattens_and_cuts_at_sentence_end(chat):
    assert chat.shape(" 行，\n机票钱你出 @全体成员 ", 50) == "行， 机票钱你出"
    assert chat.shape("一二三四五。六七八九十", 8) == "一二三四五。"
    assert chat.shape("一二三四五六七八九十", 8) == "一二三四五六七八…"
    assert chat.shape("#4 阿明这波陪跑", 50) == "阿明这波陪跑"


def test_cost_doubles_in_weekday_peak_hours(chat):
    usage = {
        "prompt_cache_miss_tokens": 1_000_000,
        "prompt_cache_hit_tokens": 0,
        "completion_tokens": 0,
    }
    friday_peak = datetime(2026, 10, 23, 2, tzinfo=UTC)
    saturday = datetime(2026, 10, 24, 2, tzinfo=UTC)
    assert chat.estimate_cost(usage, friday_peak) == 0.3
    assert chat.estimate_cost(usage, saturday) == 0.15


def test_usage_book_counts_per_key_and_globally_and_keeps_a_week(chat):
    book = chat.UsageBook(MemoryStore())
    usage = {"prompt_cache_miss_tokens": 10, "prompt_cache_hit_tokens": 5, "completion_tokens": 3}
    book.add(date(2026, 10, 1), "group:1", usage, 0.01)
    book.add(date(2026, 10, 20), "group:1", usage, 0.01)
    book.add(date(2026, 10, 20), "private:9", usage, 0.01)

    # 命中缓存的 5 个不计入上限
    assert book.tokens(date(2026, 10, 20), "group:1") == 13
    assert book.tokens(date(2026, 10, 20), chat.GLOBAL) == 26
    assert list(book.store.data) == ["2026-10-20"]  # 10-01 已超出 7 天


def test_recent_pictures_follow_their_line_within_budget(chat):
    conversation = chat.Conversation(10)
    old = conversation.add("m1", "甲(1)", "[图片]", ("u1",))
    conversation.add("m2", "乙(2)", "看这个 [图片] [图片]", ("u2", "u3"))
    conversation.add("m3", "甲(1)", "哈哈")
    for line in conversation.lines:
        line.pictures = [f"data:{url}" for url in line.images]

    (user,) = chat.build_messages("人设", conversation, "提示", images=2)[2:3]

    # 预算 2 张：#2 的两张跟在它后面，更早的 #1 只剩文字
    assert [b["type"] for b in user["content"]] == ["text", "image_url", "image_url", "text"]
    assert (
        untimed(user["content"][0]["text"]) == "#1 [甲(1)] [图片]\n#2 [乙(2)] 看这个 [图片] [图片]"
    )
    assert user["content"][1]["image_url"] == {"url": "data:u2", "detail": "low"}
    assert old not in conversation.with_images(2)
    assert isinstance(chat.build_messages("人设", conversation, "提示")[2]["content"], str)


def test_data_url_sniffs_supported_formats(chat):
    assert chat.data_url(b"\x89PNG\r\n").startswith("data:image/png;base64,")
    assert chat.data_url(b"RIFF\0\0\0\0WEBPVP8").startswith("data:image/webp;")
    assert chat.data_url(b"<html>") is None


def test_compress_keeps_recent_lines_and_puts_memo_after_rules(chat):
    conversation = chat.Conversation()
    for i in range(30):
        conversation.add(f"m{i}", "甲(1)", "说" * 50)  # 每条约 42 token

    old = conversation.older_than(500)
    assert [line.number for line in old] == list(range(1, 20))  # 留最近 11 条，不超过 500 token
    assert chat.summary_messages("", old[:1])[1]["content"].endswith("#1 [甲(1)] " + "说" * 50)

    conversation.forget(old[-1].number, "甲很能说")
    messages = chat.build_messages("人设", conversation, "提示")
    assert conversation.lines[0].number == 20
    assert messages[2] == {"role": "system", "content": messages[2]["content"]}
    assert messages[2]["content"].endswith("甲很能说")
    assert untimed(messages[3]["content"]).startswith("#20 ")


def test_time_is_marked_only_after_a_pause(chat):
    conversation = chat.Conversation()
    for at in (0, 100, 1000):
        conversation.add(f"m{at}", "甲(1)", "嗯").at = datetime(2026, 10, 24, 1, 0).timestamp() + at

    text = chat.build_messages("人设", conversation, "提示")[2]["content"]
    assert text == (
        "—— 10-24 01:00 ——\n#1 [甲(1)] 嗯\n#2 [甲(1)] 嗯\n—— 10-24 01:16 ——\n#3 [甲(1)] 嗯"
    )
