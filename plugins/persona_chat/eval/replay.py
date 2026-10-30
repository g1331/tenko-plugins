"""在临时目录起一个真实的 Tenko 宿主加载插件，把快照里的场景交给真实的 ``respond`` 回放。

和单测的区别只在 ``llm.chat`` 是真的：提示怎么拼、话怎么改写、表情包怎么挑都走插件本身的代码，
回放出来的就是线上这一刻会说的话。插件写盘全落在临时目录里，碰不到生产数据。
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import shutil
import tempfile
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

PROD = Path(os.environ.get("TENKO_ROOT", "/srv/tenko"))
DATA = Path(os.environ.get("PERSONA_EVAL_DIR", Path.home() / "persona-eval"))
# 生产会话里请求的输入约 1.6 万到 3.2 万 token（超过就压缩），扣掉人设、规则和目录后，
# 聊天记录大致在这个范围。
_KEEP_TOKENS = 12000
_RETRIES = 4


class ReplayError(RuntimeError):
    """这次回放作废（接口一直失败、被内容审核拒绝），不能当成她选择了不说话。"""


@dataclass
class Scene:
    id: str
    key: str  # "group:群号"
    cut: int  # 回放到这条（含）为止，这条就是触发的那条
    kind: str  # call / named / answer / follow / cold
    tags: list[str] = field(default_factory=list)


@dataclass
class Reply:
    bubbles: list[str]  # 她发出的每条：“[表情:名字]”是表情包，“↪#编号 ”开头的引用了那条
    calls: int = 0  # 这次回复一共请求了几次接口（含挑表情包）
    cost: float = 0.0  # 美元，按插件自己的价目估


_now: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "persona_eval_now", default=None
)
_ledger: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "persona_eval_ledger", default=None
)


class _Clock(datetime):
    """插件里的 ``datetime.now()`` 换成场景发生的时刻：不然提示里写着“现在”是几天后，
    她会以为群里冷了好几天。"""

    @classmethod
    def now(cls, tz=None):
        at = _now.get()
        return datetime.fromtimestamp(at, tz) if at is not None else datetime.now(tz)


def prod_config() -> dict[str, Any]:
    with open(PROD / "config/tenko.toml", "rb") as f:
        return dict(tomllib.load(f)["plugin_config"]["persona_chat"])


def accounts() -> dict[str, str]:
    """{QQ号: 昵称}：她在群里用的号，写进“你用的 QQ 号是……”。"""

    path = DATA / "accounts.json"
    return json.loads(path.read_text()) if path.exists() else {}


def latest_snapshot() -> Path:
    snapshots = sorted((DATA / "snapshots").iterdir())
    if not snapshots:
        raise SystemExit("还没有快照：先运行 snapshot")
    return snapshots[-1]


def take_snapshot() -> Path:
    """生产会话会在线压缩，先拷一份定住，场景和回放都只读这一份。"""

    target = DATA / "snapshots" / datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copytree(PROD / ".tenko/plugin-data/persona_chat", target / "persona_chat")
    return target


def merge_history(snapshot: Path, older: list[Path]) -> Path:
    """把更早的会话存盘并进快照，写成 ``history.json``：线上记录满了会压缩，旧的那段只剩备忘，
    并进来才有足够多的场景可挑。备忘只对快照里最早那条之后的回放有效（``summary_from``）。"""

    current = json.loads((snapshot / "persona_chat/conversations.json").read_text())["data"]
    merged: dict[str, Any] = {}
    for path in older:
        saved = json.loads(path.read_text())
        for key, conversation in saved.get("data", saved).items():
            if key.startswith("group:") and "lines" in conversation:
                lines = merged.setdefault(key, {"summary": "", "summary_from": 10**9, "lines": {}})
                lines["lines"].update({x["number"]: x for x in conversation["lines"]})
    for key, conversation in current.items():
        entry = merged.setdefault(key, {"lines": {}})
        entry["lines"].update({x["number"]: x for x in conversation["lines"]})
        entry["summary"] = conversation.get("summary", "")
        entry["summary_from"] = min((x["number"] for x in conversation["lines"]), default=0)
    history = {
        key: {**entry, "lines": [entry["lines"][n] for n in sorted(entry["lines"])]}
        for key, entry in merged.items()
    }
    path = snapshot / "history.json"
    path.write_text(json.dumps(history, ensure_ascii=False))
    return path


def load_conversations(snapshot: Path) -> dict[str, Any]:
    """快照里的会话（并过旧存盘的话用并好的），加上从以前的盲评题还原出来的场景。"""

    history = snapshot / "history.json"
    if history.exists():
        data = json.loads(history.read_text())
    else:
        data = json.loads((snapshot / "persona_chat/conversations.json").read_text())["data"]
    labeled = DATA / "labeled.json"
    if labeled.exists():
        data |= json.loads(labeled.read_text())
    return data


class Host:
    """加载好插件的宿主；``module`` 就是插件的 ``__init__``。"""

    def __init__(self, runtime, module, persona) -> None:
        self.runtime = runtime
        self.module = module
        self.persona = persona

    async def stop(self) -> None:
        """在事件循环关掉之前卸载插件，让它关掉 HTTP 客户端、收拾后台任务。"""

        self.runtime.unload("persona_chat")
        await asyncio.sleep(0.1)

    @classmethod
    async def start(cls, snapshot: Path, **overrides: Any) -> Host:
        from arclet.entari import Entari
        from arclet.entari.config import EntariConfig
        from tenko.host.plugins import PluginRuntime

        root = Path(tempfile.mkdtemp(prefix="persona-eval-"))
        shutil.copytree(snapshot / "persona_chat", root / ".tenko/plugin-data/persona_chat")
        os.chdir(root)
        EntariConfig(root / "entari.yml")
        Entari(ignore_self_message=True)
        (root / "state.json").write_text(json.dumps({"enabled": ["persona_chat"]}))
        config = {
            **prod_config(),
            # 不压缩、不限额、不搜索、不看图：快照里没有图，也不想让回放顺手花这些钱。
            "context_tokens": 10**9,
            "group_daily_tokens": 0,
            "global_daily_tokens": 0,
            "search_daily": 0,
            "vision_images": 0,
            **overrides,
        }
        runtime = PluginRuntime(root / "no-builtin", state_file=root / "state.json")
        await runtime.load_all(external_configs={"persona_chat": config})
        module = runtime.loaded_plugins["persona_chat"].module
        import sys

        host = cls(runtime, module, sys.modules[f"{module.__name__}.persona"])
        host._patch()
        return host

    def _patch(self) -> None:
        m = self.module
        m.datetime = _Clock
        m._typing_delay = lambda text: 0
        m._reply_delay = lambda text: 0
        m._commands = dict  # 不替人跑别的插件的命令
        m._rounds = lambda key, speaker: 1  # 和谁聊了几轮靠进程里的计时，回放时不提“聊太久了”
        names = accounts()
        m.accounts_for_group = lambda group, platform=None: [
            SimpleNamespace(self_id=qq, self_info=SimpleNamespace(user=SimpleNamespace(name=name)))
            for qq, name in names.items()
        ]
        chat, pick = m.llm.chat, m.llm.pick_sticker

        async def counted(call, *args, **kwargs):
            for attempt in range(_RETRIES):
                try:
                    result = await call(*args, **kwargs)
                    break
                except httpx.HTTPStatusError as error:
                    status = error.response.status_code
                    if status < 500 and status != 429 or attempt == _RETRIES - 1:
                        raise ReplayError(f"HTTP {status} {error.response.text[:200]}") from error
                except (httpx.TransportError, ValueError, KeyError) as error:
                    if attempt == _RETRIES - 1:
                        raise ReplayError(repr(error)) from error
                await asyncio.sleep(3 * (attempt + 1))
            if (ledger := _ledger.get()) is not None:
                ledger.append(result[1])
            return result

        m.llm.chat = lambda *a, **k: counted(chat, *a, **k)
        m.llm.pick_sticker = lambda *a, **k: counted(pick, *a, **k)

    def conversation(self, data: dict[str, Any], scene: Scene):
        """快照里这个群截到 ``scene.cut`` 的记录：只留最近一段，大小和线上压缩后差不多。"""

        saved = data[scene.key]
        lines = [x for x in saved["lines"] if x["number"] <= scene.cut]
        kept, total = [], 0.0
        for line in reversed(lines):
            total += self.module.estimate_tokens(line["text"]) + 8
            if total > _KEEP_TOKENS and kept:
                break
            kept.append(line)
        kept.reverse()
        fresh = kept[0]["number"] >= saved.get("summary_from", 0)
        return self.module.Conversation.restore(
            {
                "summary": saved.get("summary", "") if fresh else "",
                "next_number": scene.cut + 1,
                "passed": scene.cut,
                "lines": kept,
            }
        )

    async def play(self, data: dict[str, Any], scene: Scene) -> Reply:
        m = self.module
        conversation = self.conversation(data, scene)
        target = conversation.lines[-1]
        sent: list[list] = []
        session = _session(scene, target, sent)
        conversation.place = m._place(session, conversation)
        _now.set(target.at + 5)
        ledger: list[dict] = []
        _ledger.set(ledger)
        if scene.kind == "call":
            await m.respond(session, conversation, target, force=True, mood="call")
        elif scene.kind == "named":
            await m.respond(session, conversation, target, mood="named")
        elif scene.kind == "answer":
            await m.respond(session, conversation, None, follow=True, answer=target)
        else:
            await m.respond(session, conversation, None, follow=scene.kind == "follow")
        hers = [x for x in conversation.lines if x.number > scene.cut and x.speaker is None]
        bubbles = []
        for line, content in zip(hers, sent, strict=False):
            quote = content[0] if content and type(content[0]).__name__ == "Quote" else None
            quoted = quote and conversation.by_message_id(quote.id)
            bubbles.append(f"↪#{quoted.number} {line.text}" if quoted else line.text)
        cost = sum(m.estimate_cost(u, m.datetime.now()) for u in ledger)
        return Reply(bubbles, len(ledger), cost)


def _session(scene: Scene, target, sent: list[list]):
    name, _, qq = (target.speaker or "群友(0)").rpartition("(")
    # 从盲评题还原的场景（label:编号）也当成群聊。
    group = scene.key.partition(":")[2] if scene.key.startswith(("group:", "label:")) else None
    index = len(sent)

    async def send(content):
        sent.append(list(content))
        return [SimpleNamespace(id=f"eval-{scene.id}-{index + len(sent)}")]

    return SimpleNamespace(
        user=SimpleNamespace(id=qq.rstrip(")"), nick=None, name=name),
        account=SimpleNamespace(self_id=next(iter(accounts()), "0"), protocol=SimpleNamespace()),
        event=SimpleNamespace(
            guild=SimpleNamespace(id=group, name=group) if group else None,
            channel=SimpleNamespace(id=group or qq.rstrip(")")),
            member=None,
            message=SimpleNamespace(id=target.message_id, message=[]),
        ),
        send=send,
    )
