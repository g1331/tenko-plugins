import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from arclet.alconna import command_manager
from arclet.entari.command import _commands
from satori import Author, Image, Quote, Text

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16
GIF = b"GIF89a" + b"0" * 16


@pytest.fixture
def marriage(module):
    from tenko_plugin_random_wife import marriage

    return marriage


@pytest.fixture
def library(module):
    from tenko_plugin_random_wife import library

    return library


@pytest.fixture
def clean(module, library):
    """每个用例从空的老婆库、空的登记和投稿开始。"""

    root = library.folder().parent
    shutil.rmtree(root, ignore_errors=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def sent(module, monkeypatch):
    """记下发往各个群的消息，不真的发。"""

    messages = []

    async def send(group, platform, content):
        messages.append((group, content))
        return True

    monkeypatch.setattr(module.manage, "_send", send)
    return messages


def _text(reply) -> str:
    if isinstance(reply, str):
        return reply
    return "".join(e.text for e in reply if isinstance(e, Text))


# ---- 规则 ----


def test_first_draw_keeps_the_wife_and_later_draws_return_her(marriage):
    registry = marriage.Registry(None, "2026-11-01")
    library = {"a", "b"}
    assert registry.draw("g", "1", "甲", "a", library) == marriage.Outcome("new", "a")
    assert registry.draw("g", "1", "甲", "b", library) == marriage.Outcome("mine", "a")
    # 别的群互不影响
    assert registry.draw("h", "2", "乙", "a", library) == marriage.Outcome("new", "a")


def test_drawing_someone_elses_wife_twice_steals_her(marriage):
    registry = marriage.Registry(None, "2026-11-01")
    library = {"a", "b", "c"}
    registry.draw("g", "1", "甲", "a", library)
    registry.draw("g", "3", "丙", "c", library)
    assert registry.draw("g", "2", "乙", "a", library) == marriage.Outcome("seen", "a", "甲")
    assert registry.draw("g", "2", "乙", "c", library) == marriage.Outcome("seen", "c", "丙")
    assert registry.draw("g", "2", "乙", "a", library) == marriage.Outcome("stolen", "a", "甲")
    assert registry.wife_of("g", "1") is None  # 甲没了老婆，可以重新抽
    assert registry.draw("g", "1", "甲", "b", library) == marriage.Outcome("new", "b")


def test_only_recent_draws_count_towards_stealing(marriage):
    registry = marriage.Registry(None, "2026-11-01")
    library = {f"w{i}" for i in range(20)}
    for i in range(20):
        registry.draw("g", "owner", "主", f"w{i}", library)
        registry.divorce("g", "owner")
        registry.state["groups"]["g"]["owners"][f"w{i}"] = {"id": f"o{i}", "name": f"主{i}"}
    for i in range(marriage.RECENT + 1):
        assert registry.draw("g", "2", "乙", f"w{i}", library).kind == "seen"
    assert registry.draw("g", "2", "乙", "w0", library).kind == "seen"  # 已经被挤出去了
    assert registry.draw("g", "2", "乙", "w5", library).kind == "stolen"


def test_divorce_and_daily_reset(marriage):
    registry = marriage.Registry(None, "2026-11-01")
    registry.draw("g", "1", "甲", "a", {"a"})
    assert registry.divorce("g", "1") == "a"
    assert registry.divorce("g", "1") is None
    registry.draw("g", "1", "甲", "a", {"a"})
    tomorrow = marriage.Registry(registry.state, "2026-11-02")
    assert tomorrow.wife_of("g", "1") is None


def test_deleted_wife_no_longer_counts(marriage):
    registry = marriage.Registry(None, "2026-11-01")
    registry.draw("g", "1", "甲", "a", {"a", "b"})
    assert registry.draw("g", "1", "甲", "b", {"b"}) == marriage.Outcome("new", "b")


# ---- 老婆库 ----


def test_names_and_formats_are_checked(library):
    assert library.check_name(" 芙莉莲 ") == "芙莉莲"
    for bad in ["", "../x", "a/b", ".hidden", "x" * 31, "a\nb"]:
        with pytest.raises(library.LibraryError):
            library.check_name(bad)
    assert library.suffix(PNG) == ".png"
    assert library.suffix(GIF) == ".gif"
    assert library.suffix(b"\xff\xd8\xff\xe0") == ".jpg"
    assert library.suffix(b"RIFF\0\0\0\0WEBPVP8 ") == ".webp"
    with pytest.raises(library.LibraryError):
        library.suffix(b"<html>")


def test_old_bot_images_dropped_into_the_folder_just_work(library, clean):
    (library.folder() / "旧老婆.jpg").write_bytes(b"\xff\xd8\xff")
    (library.folder() / "__init__.py").write_text("")
    assert list(library.wives()) == ["旧老婆"]
    # 旧库里不少图的后缀和内容对不上，发图时按内容判断
    (library.folder() / "其实是动图.png").write_bytes(GIF)
    path = library.wives()["其实是动图"]
    assert library.mime(path.read_bytes(), path) == "image/gif"


# ---- 命令 ----


def _alconna(name):
    return next(c for c in command_manager.get_commands() if c.command == name)


def test_old_command_names_reach_the_same_handler(module):
    for names, handler in [
        (["抽老婆", "随机老婆", "随机wife"], module.draw_wife),
        (["添加老婆", "添加wife"], module.add_wife_command),
        (["删除老婆", "删除wife"], module.remove_wife_command),
    ]:
        for name in names:
            targets = [_commands.subscribers[i] for i in _commands.trie[f"/{name}"]]
            assert any(t.callable_target is handler for t in targets), name
    result = _alconna("添加老婆").parse([Text("/添加老婆 芙莉莲"), Image(src="http://x/a.png")])
    assert result.matched and result.query("name") == "芙莉莲"


async def test_draw_from_an_empty_and_a_filled_library(module, library, clean):
    assert "老婆库还是空的" in await module.daily.draw("g", "1", "甲")
    library.add("芙莉莲", PNG)
    reply = await module.daily.draw("g", "1", "甲")
    assert _text(reply).startswith("你今天的老婆是芙莉莲")
    assert any(isinstance(e, Image) for e in reply)
    assert "你抽到了芙莉莲，是【甲】的老婆" in _text(await module.daily.draw("g", "2", "乙"))
    assert "你抢走了【甲】的老婆芙莉莲" in _text(await module.daily.draw("g", "2", "乙"))
    assert await module.daily.divorce("g", "1") == "你还没有老婆，先 /抽老婆 抽一个吧~"
    assert await module.daily.divorce("g", "2") == "你已经和芙莉莲离婚了~"


async def test_member_submission_is_reviewed_in_the_review_group(module, library, clean, sent):
    assert await module.manage.submit("g", "qq-client", "10", "群友", "芙莉莲", PNG) == (
        "投稿已提交给管理员，审核通过后会加进老婆库"
    )
    assert "已经有人投稿了芙莉莲" in await module.manage.submit(
        "g", "qq-client", "11", "b", "芙莉莲", GIF
    )
    group, content = sent[0]
    assert group == "900" and any(isinstance(e, Image) for e in content)
    said = _text(content)
    assert "群友(10) 添加老婆【芙莉莲】的申请（编号 1）" in said

    quote = Quote("1")(Author("bot"), Text(said))
    assert module.manage._quoted_request([quote, Text("y")], {"bot"}) == "1"
    assert module.manage._quoted_request([quote, Text("y")], {"other"}) is None
    assert module.manage._quoted_request([Text("y")], {"bot"}) is None

    assert await module.manage.decide("1", "Y") == "已同意 群友(10) 投稿的芙莉莲"
    assert "芙莉莲" in library.wives()
    assert sent[-1][0] == "g" and "审核通过" in _text(sent[-1][1])
    assert "已经处理过或过期" in await module.manage.decide("1", "y")
    assert not any(library.folder("pending").iterdir())


async def test_rejection_carries_the_reason(module, library, clean, sent):
    await module.manage.submit("g", "qq-client", "10", "群友", "芙莉莲", PNG)
    assert await module.manage.decide("1", "图太糊了") == "已拒绝 群友(10) 投稿的芙莉莲"
    assert "芙莉莲" not in library.wives()
    assert "没有通过，理由：图太糊了" in _text(sent[-1][1])


async def test_submission_that_cannot_reach_the_review_group_is_dropped(
    module, library, clean, monkeypatch
):
    async def fail(group, platform, content):
        return False

    monkeypatch.setattr(module.manage, "_send", fail)
    assert "没能发到审核群" in await module.manage.submit(
        "g", "qq-client", "10", "群友", "芙莉莲", PNG
    )
    assert module.manage._pending()["items"] == {}


@pytest.fixture
def master(module, monkeypatch):
    """没配审核群：投稿经 notify_master 发给 Master。"""

    monkeypatch.setattr(module.config.config, "review_group", "")
    state = SimpleNamespace(notices=[], result=SimpleNamespace(delivered=True, reason=None))

    async def notify(content, *, key=None):
        state.notices.append((content, key))
        return state.result

    monkeypatch.setattr(module.manage, "notify_master", notify)
    return state


def _listener(handler):
    return getattr(handler, "callable_target", handler)


async def test_without_a_review_group_master_reviews(
    module, library, clean, sent, master, monkeypatch
):
    assert await module.manage.submit("g", "qq-client", "10", "群友", "芙莉莲", PNG) == (
        "投稿已提交给管理员，审核通过后会加进老婆库"
    )
    content, key = master.notices[0]
    assert key == "submission:1" and any(isinstance(e, Image) for e in content)
    assert "群友(10) 添加老婆【芙莉莲】的申请（编号 1）" in _text(content)
    assert sent == []  # 没往审核群发

    replies = iter(
        [
            None,  # 不是回复通知的消息
            SimpleNamespace(key="other:1", text="y"),  # 本插件别的通知
            SimpleNamespace(key="submission:1", text=""),  # 只回了图
            SimpleNamespace(key="submission:1", text="y"),
        ]
    )

    async def reply(session):
        return next(replies)

    monkeypatch.setattr(module.manage, "master_reply", reply)
    session = SimpleNamespace(send=AsyncMock())
    for _ in range(4):
        await _listener(module.on_master_reply)(session)
    session.send.assert_awaited_once_with("已同意 群友(10) 投稿的芙莉莲")
    assert "芙莉莲" in library.wives()
    assert sent[-1][0] == "g" and "审核通过" in _text(sent[-1][1])


async def test_master_rate_limit_drops_the_submission(module, library, clean, master):
    master.result = SimpleNamespace(delivered=False, reason="rate_limited")
    assert await module.manage.submit("g", "qq-client", "10", "群友", "芙莉莲", PNG) == (
        "最近投稿的人太多了，过一会儿再来"
    )
    master.result = SimpleNamespace(delivered=False, reason="no_account")
    assert "没能发给管理员" in await module.manage.submit(
        "g", "qq-client", "10", "群友", "芙莉莲", PNG
    )
    assert module.manage._pending()["items"] == {}
    assert not any(library.folder("pending").iterdir())


async def test_admin_adds_directly_and_members_need_an_image(module, library, clean, monkeypatch):
    async def perm(session, level):
        return session.admin

    async def download(src):
        assert src == "http://x/a.png"
        return PNG

    monkeypatch.setattr(module.manage, "require_perm", perm)
    monkeypatch.setattr(library, "download", download)

    def session(elements, admin=True):
        event = SimpleNamespace(message=SimpleNamespace(message=elements), guild=None)
        return SimpleNamespace(event=event, admin=admin)

    assert "要带上老婆的图片" in await module.manage.add_wife(session([Text("x")]), "芙莉莲")
    quoted = Quote("1")(Author("someone"), Image(src="http://x/a.png"))
    assert (
        await module.manage.add_wife(session([quoted, Text("x")]), "芙莉莲")
        == "已把芙莉莲加进老婆库"
    )
    assert library.wives()["芙莉莲"].suffix == ".png"
    with pytest.raises(library.LibraryError):
        await module.manage.add_wife(session([Image(src="http://x/a.png")]), "芙莉莲")
    member = session([Image(src="http://x/a.png")], admin=False)
    assert await module.manage.add_wife(member, "别的") == "投稿要在群里发"


def _real_png(color="pink", size=(60, 90)) -> bytes:
    from io import BytesIO

    from PIL import Image as Picture

    buffer = BytesIO()
    Picture.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


def test_list_arguments_never_mistake_a_name_for_a_page(module):
    command = _alconna("老婆列表")
    result = command.parse("/老婆列表 02")  # 旧库里真有叫 02、168 的老婆
    assert result.query("keyword") == "02" and result.query("page.page") is None
    result = command.parse("/老婆列表 中野 -p 2")
    assert result.query("keyword") == "中野" and result.query("page.page") == 2
    result = command.parse("/老婆列表 -p 3")
    assert result.query("keyword") is None and result.query("page.page") == 3
    assert command.parse("/老婆列表 02 -页 2").query("page.page") == 2


def test_exact_name_comes_first(module, library, clean):
    for name in ["002", "02", "1024"]:
        library.add(name, PNG)
    assert list(library.wives()) == ["002", "02", "1024"]
    found = [name for name, _ in module.pages.search(library.wives(), "02")]
    assert found == ["02", "002", "1024"]


def test_list_pages_search_and_sizes(module, library, clean):
    pages = module.pages
    assert pages.format_list(pages.list_page({}, None, 1)) == "老婆库还是空的"
    for name in ["中野三玖", "中野五月", "芙莉莲"]:
        library.add(name, PNG)
    context = pages.list_page(library.wives(), "中野", 1, thumbs=False)
    assert [c["name"] for c in context["cards"]] == ["中野三玖", "中野五月"]
    assert context["found"] == 2 and context["total"] == 3
    assert context["cards"][0]["size"] == "1 KB" and context["cards"][0]["format"] == "PNG"
    assert pages.format_list(context).splitlines() == [
        "名字带“中野”的有 2 位（第 1/1 页）",
        "1. 中野三玖  1 KB",
        "2. 中野五月  1 KB",
    ]
    assert "没有名字带“阿库娅”" in pages.format_list(pages.list_page(library.wives(), "阿库娅", 1))

    for i in range(pages.PER_PAGE * 2):
        library.add(f"w{i:02d}", PNG)
    context = pages.list_page(library.wives(), None, 9, thumbs=False)  # 超出范围取最后一页
    assert (context["page"], context["pages"]) == (3, 3)
    assert context["cards"][0]["no"] == pages.PER_PAGE * 2 + 1
    assert pages.size_text(3 * 1024 * 1024 + 200 * 1024) == "3.2 MB"


def test_thumbnails_are_cached_and_follow_changes(module, library, clean):
    pages = module.pages
    path = library.add("芙莉莲", _real_png())
    first = pages._thumbnail(path)
    assert first.startswith(b"\xff\xd8\xff")
    assert len(list(library.folder("thumbs").iterdir())) == 1
    assert pages._thumbnail(path) == first
    path.write_bytes(_real_png("blue", (80, 80)))  # 换了图：重新生成，旧缓存删掉
    assert pages._thumbnail(path) != first
    assert len(list(library.folder("thumbs").iterdir())) == 1
    (library.folder() / "坏图.png").write_bytes(PNG)
    assert pages._thumb_uri(library.wives()["坏图"]) is None


async def test_list_falls_back_to_text_without_a_browser(module, library, clean):
    library.add("芙莉莲", _real_png())
    assert (
        await module.manage.wife_list(None, 1) == "老婆库里一共 1 位（第 1/1 页）\n1. 芙莉莲  1 KB"
    )


async def test_real_notify_master_without_accounts(module, library, clean, monkeypatch):
    # 不替换宿主接口：测试宿主没有在线账号，送不到 Master，投稿撤回
    monkeypatch.setattr(module.config.config, "review_group", "")
    reply = await module.manage.submit("g", "qq-client", "10", "群友", "芙莉莲", PNG)
    assert reply == "投稿没能发给管理员，稍后再试"
    assert module.manage._pending()["items"] == {}
