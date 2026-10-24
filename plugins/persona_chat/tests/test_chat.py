"""纯逻辑；插件包只能在宿主加载后导入，所以经 module fixture 取子模块。"""

import base64
import io
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
    def __init__(self, path=None):
        self.data = None
        self.path = path

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


def test_dump_and_restore_keep_the_same_request(chat):
    conversation = chat.Conversation(summary="甲爱吃火锅")
    conversation.add("m1", "甲(1)", "[图片] 看", ("https://过期的图",), role="群主")
    conversation.add("b1", None, "好看")
    conversation.add(None, "Tenko 功能", "猜谜开始", counted=False)

    restored = chat.Conversation.restore(conversation.dump())

    assert restored.next_number == 4 and restored.summary == "甲爱吃火锅"
    assert restored.lines[0].images == () and restored.lines[0].role == "群主"
    assert chat.build_messages("人设", restored, "提示") == chat.build_messages(
        "人设", conversation, "提示"
    )


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


def test_animated_gif_becomes_a_contact_sheet(chat):
    from PIL import Image

    frames = [Image.new("RGB", (40, 30), color) for color in ("red", "green", "blue", "white") * 3]
    buffer = io.BytesIO()
    frames[0].save(buffer, "GIF", save_all=True, append_images=frames[1:])
    url = chat.picture_url(buffer.getvalue())
    assert url.startswith("frames:data:image/jpeg;base64,")
    sheet = Image.open(io.BytesIO(base64.b64decode(url.partition(",")[2])))
    assert sheet.size == (120, 60)  # 12 帧抽 6 帧，3×2

    still = io.BytesIO()
    frames[0].save(still, "GIF")
    assert chat.picture_url(still.getvalue()).startswith("data:image/gif;")
    assert chat.picture_url(b"GIF89a broken").startswith("data:image/gif;")

    conversation = chat.Conversation()
    conversation.add("1", "群友(1)", "[表情包]", ["u"]).pictures = [url]
    messages = chat.build_messages("人设", conversation, "提示", 1)
    (blocks,) = (m["content"] for m in messages if isinstance(m["content"], list))
    assert [b["type"] for b in blocks] == ["text", "image_url"]
    assert "动图" in blocks[0]["text"] and blocks[1]["image_url"]["url"] == url.removeprefix(
        "frames:"
    )


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


PNG = b"\x89PNG" + b"0" * 20


def test_stickers_dedupe_rename_evict_and_list(module, tmp_path):
    stickers = type(module.sticker_book)(MemoryStore(tmp_path / "stickers.json"), limit=2)
    assert stickers.catalog() == "你的表情包收藏还是空的。"
    assert stickers.add(" 猫猫[震惊] ", "吃惊、无语", PNG, "a.png") == ("猫猫震惊", [])
    assert stickers.add("别的名字", "x", PNG, "b.png") is None  # 同一张图
    assert stickers.add("狗头", "x", b"GIF8" + b"1" * 20, "a.png") is None  # 同一个 QQ 文件
    assert stickers.add("猫猫震惊", "嘲讽", b"GIF8" + b"2" * 20, "c.gif") == ("猫猫震惊2", [])
    assert stickers.add("坏图", "x", b"not an image", "d") is None
    assert stickers.name_of("a.png") == "猫猫震惊" and stickers.name_of("") is None
    assert stickers.catalog().endswith("猫猫震惊：吃惊、无语\n猫猫震惊2：嘲讽")

    assert stickers.catalog().startswith("收藏已经满了（2 张）")
    stickers.use("猫猫震惊")
    new = stickers.add("新来的", "开心", b"GIF8" + b"3" * 20, "e.gif", replace="没有这张")
    assert new == ("新来的", ["猫猫震惊2"])  # 满了没挑对：丢没用过的
    assert list(stickers.index()) == ["猫猫震惊", "新来的"]
    new = stickers.add("又来的", "开心", b"GIF8" + b"5" * 20, "g.gif", replace="猫猫震惊")
    assert new == ("又来的", ["猫猫震惊"])  # 挑了就丢它，哪怕用过
    assert list(stickers.index()) == ["新来的", "又来的"]
    assert stickers.remove("又来的") and stickers.add("猫猫震惊", "吃惊、无语", PNG, "a.png") == (
        "猫猫震惊",
        [],
    )
    stickers.use("猫猫震惊")
    assert len(list((tmp_path / "stickers").iterdir())) == 2
    assert stickers.remove("新来的") and not stickers.remove("新来的")
    assert stickers.path("新来的") is None and stickers.path("猫猫震惊").is_file()

    # 改备注；新名字被占用就只改备注，空备注和不存在的名字不改
    assert stickers.add("狗头", "嘲讽", b"GIF8" + b"4" * 20, "f.gif") == ("狗头", [])
    assert stickers.edit("猫猫震惊", "猫瞪眼「啊？」；吃惊时发", "狗头") == "猫猫震惊"
    assert stickers.edit("猫猫震惊", "猫瞪眼「啊？」；吃惊时发", "猫猫啊") == "猫猫啊"
    assert stickers.index()["猫猫啊"]["mood"] == "猫瞪眼「啊？」；吃惊时发"
    assert stickers.path("猫猫啊").is_file() and "猫猫震惊" not in stickers.index()
    assert stickers.edit("猫猫啊", " ") is None and stickers.edit("没有", "x") is None
