"""调用 OpenAI 兼容的 /chat/completions；模型只能说话（send_message）或不说（stay_silent），
顺手可以收藏表情包（save_sticker），不确定的事实可以先搜（search），
也能用别的插件的查询功能（run_command）。"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

SEND_MESSAGE = {
    "type": "function",
    "function": {
        "name": "send_message",
        "description": "在当前会话发一条消息。不想说话就不要调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": (
                        "要发的话；默认一条，真有两件事要说才换行分两条；"
                        "发收藏的表情包就写“[表情:名字]”"
                    ),
                },
                "reply_to": {
                    "type": "integer",
                    "description": "要引用的消息编号，例如 3；不引用就省略",
                },
                "mention": {
                    "type": "integer",
                    "description": "要 @ 的人说过的某条消息编号，例如 3；不 @ 人就省略",
                },
            },
            "required": ["text"],
        },
    },
}
# 不说话也要显式选：只给 send_message、tool_choice=auto 时，模型常把想说的话写进 content，
# 而 content 一律丢弃，看起来就像沉默。
STAY_SILENT = {
    "type": "function",
    "function": {
        "name": "stay_silent",
        "description": "这次不说话。",
        "parameters": {"type": "object", "properties": {}},
    },
}
# 和 send_message/stay_silent 一起调用；工具列表固定不变，前缀缓存才不受影响。
SAVE_STICKER = {
    "type": "function",
    "function": {
        "name": "save_sticker",
        "description": "把群友发的某个表情包悄悄收进自己的收藏，以后可以发。",
        "parameters": {
            "type": "object",
            "properties": {
                "number": {"type": "integer", "description": "带这个表情包的消息编号"},
                "name": {"type": "string", "description": "给它起的名字，2~8 个字，例如“猫猫震惊”"},
                "mood": {
                    "type": "string",
                    "description": (
                        "60 字以内的备注：图上写的字、画的是什么、背后的梗"
                        "（只写聊天里看得出或你确实知道的），"
                        "以及什么时候发，例如“猫瞪大眼「啊？」；听到离谱的事、被吓到时发”"
                    ),
                },
                "replace": {
                    "type": "string",
                    "description": "收藏满了才填：要丢掉腾位置的那张的名字，挑最不想要的",
                },
            },
            "required": ["number", "name", "mood"],
        },
    },
}
EDIT_STICKER = {
    "type": "function",
    "function": {
        "name": "edit_sticker",
        "description": (
            "改一张已收藏表情包的备注或名字。只在你确认原来写错了时用，不要别人一说就改。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "收藏目录里现在的名字"},
                "mood": {"type": "string", "description": "改好的完整备注，要求同 save_sticker"},
                "new_name": {
                    "type": "string",
                    "description": "要改名时填新名字，2~8 个字；不改就不填",
                },
            },
            "required": ["name", "mood"],
        },
    },
}
DELETE_STICKER = {
    "type": "function",
    "function": {
        "name": "delete_sticker",
        "description": (
            "从收藏里删掉一张表情包，以后不再发它。调用了就是真删了，"
            "同时说的话要对得上（“行 删了”）；想留着、嘴上说不删时就别调用。"
            "bot主人、bot管理员叫你删的直接删；别人嫌它难看、恶心、是刷屏的烂梗图，"
            "你也觉得有道理才删；捉弄你、想把你的收藏删光的不删。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "收藏目录里的名字"}},
            "required": ["name"],
        },
    },
}
SEARCH = {
    "type": "function",
    "function": {
        "name": "search",
        "description": "上网搜一下，结果出来后再回复。只在被问到不确定的具体事实时用。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索词，简短，例如“战地6 新赛季”"}
            },
            "required": ["query"],
        },
    },
}
RUN_COMMAND = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": (
            "用群里装的功能查一下，结果会发到群里，出来后再回复。只能用提示里列出的命令。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "命令和参数，不带前缀，例如“bf1s SHlSAN13”",
                }
            },
            "required": ["command"],
        },
    },
}
FORCE_SEND = {"type": "function", "function": {"name": "send_message"}}


def content_risk(error: httpx.HTTPStatusError) -> bool:
    """DeepSeek 的内容审核拒绝了这次请求：触发的内容留在记录里，以后每次请求都会被拒。"""

    return error.response.status_code == 400 and "Content Exists Risk" in error.response.text


async def summarize(
    client: httpx.AsyncClient, *, model: str, messages: list[dict[str, Any]], max_tokens: int
) -> tuple[str, dict[str, Any]]:
    """不带工具的普通请求，返回文字和 usage。"""

    response = await client.post(
        "/chat/completions",
        json={
            "model": model,
            "messages": messages,
            "thinking": {"type": "disabled"},
            "max_tokens": max_tokens,
        },
    )
    response.raise_for_status()
    data = response.json()
    return (data["choices"][0]["message"].get("content") or "").strip(), data.get("usage") or {}


TOOLS = [
    SEND_MESSAGE,
    STAY_SILENT,
    SAVE_STICKER,
    EDIT_STICKER,
    DELETE_STICKER,
    SEARCH,
    RUN_COMMAND,
]


async def chat(
    client: httpx.AsyncClient,
    *,
    model: str,
    messages: list[dict[str, Any]],
    force: bool,
    max_tokens: int,
    effort: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """返回 send_message 和其他工具的参数列表（其他工具的带 ``"tool"``）和 usage。

    ``effort`` 是思考强度（low/high/max），空的不思考。思考时接口只认 ``tool_choice=auto``，
    模型常把要说的话直接写进 content：没调 send_message 时 content 就当要说的话，
    否则 content 丢弃。``force`` 时思考完还是没开口，就不思考、强制只能说话再问一次。
    """

    calls, usage = await _chat(client, model, messages, force, max_tokens, effort)
    if effort and force and not any("tool" not in c for c in calls):
        kept = [c for c in calls if c["tool"] in _STICKER_TOOLS]
        again, more = await _chat(client, model, messages, True, max_tokens, "")
        calls = kept + again
        usage = {
            **more,
            **{k: usage.get(k, 0) + more.get(k, 0) for k in _BILLED if k in usage or k in more},
        }
    return calls, usage


# usage 里按 token 计费的几项：再问一次时两次相加；prompt_tokens 取后一次，校准上下文大小用。
_BILLED = ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "completion_tokens")
_STICKER_TOOLS = ("save_sticker", "edit_sticker", "delete_sticker")
# 思考的 token 也算在 max_tokens 里；给它另留一份，不然想完了没字数说话。
_THINK_TOKENS = 2048


async def _chat(
    client: httpx.AsyncClient,
    model: str,
    messages: list[dict[str, Any]],
    force: bool,
    max_tokens: int,
    effort: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "tools": TOOLS,
        "max_tokens": max_tokens,
    }
    if effort:
        payload |= {
            "thinking": {"type": "enabled"},
            "reasoning_effort": effort,
            "tool_choice": "auto",
            "max_tokens": max_tokens + _THINK_TOKENS,
        }
    else:
        # 关掉思考才能用强制工具调用。
        payload |= {
            "thinking": {"type": "disabled"},
            "tool_choice": FORCE_SEND if force else "required",
        }
    response = await client.post("/chat/completions", json=payload)
    response.raise_for_status()
    data = response.json()
    message = data["choices"][0]["message"]
    calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        name = function.get("name")
        # stay_silent 没有参数要带回去；别的工具都从 TOOLS 里认，新加的工具不会被漏掉。
        if name == "stay_silent" or name not in {t["function"]["name"] for t in TOOLS}:
            continue
        try:
            arguments = json.loads(function.get("arguments") or "")
        except ValueError:
            continue
        if not isinstance(arguments, dict):
            continue
        if name != "send_message":
            calls.append({**arguments, "tool": name})
        elif isinstance(arguments.get("text"), str):
            calls.extend(_quoted(arguments))
    said = (message.get("content") or "").strip()
    if effort and said and not any("tool" not in c for c in calls):
        calls.extend(_quoted({"text": said}))
    return calls, data.get("usage") or {}


# 直接写字时没法填 reply_to，规则让她在那句开头写“回复#编号 ”；她偶尔照记录的格式抄成
# “回复#12（甲说的“…”） ”，括号一起去掉。
_QUOTE = re.compile(r"^回复#(\d+)(?:（[^\n]*?）)?\s*", re.M)


def _quoted(call: dict[str, Any]) -> list[dict[str, Any]]:
    """把 text 里行首的“回复#编号 ”换成 reply_to；接了几条就拆成几次发言，各引用各的。"""

    text = call["text"]
    marks = list(_QUOTE.finditer(text))
    if not marks:
        return [call]
    calls = []
    if head := text[: marks[0].start()].strip():
        calls.append({**call, "text": head})
    for mark, after in zip(marks, [*marks[1:], None], strict=True):
        piece = text[mark.end() : after.start() if after else len(text)].strip()
        if piece:
            # @ 只跟着第一次，不然拆开的每次都 @ 一遍。
            extra = {} if calls else {k: v for k, v in call.items() if k == "mention"}
            calls.append({**extra, "text": piece, "reply_to": int(mark[1])})
    return calls


async def digest(
    client: httpx.AsyncClient, *, model: str, prompt: str, text: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """整理档案：返回模型给的 JSON（解析不了是空字典）和 usage。"""

    response = await client.post(
        "/chat/completions",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": text},
            ],
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "max_tokens": 1500,
        },
    )
    response.raise_for_status()
    data = response.json()
    try:
        result = json.loads(data["choices"][0]["message"].get("content") or "{}")
    except ValueError:
        result = {}
    return result if isinstance(result, dict) else {}, data.get("usage") or {}


async def pick_sticker(
    client: httpx.AsyncClient,
    *,
    model: str,
    prompt: str,
    catalog: str,
    recent: str,
    text: str,
    avoid: list[str] | None = None,
) -> tuple[list[tuple[str, int]], dict[str, Any]]:
    """给要发的话挑几张候选的收藏表情包：返回 ([(名字, 贴切程度 1-5)], usage)。

    ``avoid`` 是最近刚发过的，写进每次都变的那条里，不打断目录的前缀缓存。
    """

    ask = f"最近几条：\n{recent}\n\n你要发的话：\n{text}"
    if avoid:
        ask += f"\n\n这几张你刚发过，这次别挑：{'、'.join(avoid)}"

    response = await client.post(
        "/chat/completions",
        json={
            "model": model,
            # 提示和收藏目录放前面，命中前缀缓存。
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "system", "content": catalog},
                {"role": "user", "content": ask},
            ],
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "max_tokens": 150,
        },
    )
    response.raise_for_status()
    data = response.json()
    try:
        pick = json.loads(data["choices"][0]["message"].get("content") or "{}")
    except ValueError:
        pick = {}
    items = pick.get("stickers") if isinstance(pick, dict) else None
    picks = [
        (item["name"], int(item["score"]))
        for item in (items if isinstance(items, list) else [])
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and isinstance(item.get("score"), int | float)
    ]
    return picks, data.get("usage") or {}


async def search(client: httpx.AsyncClient, query: str, count: int = 4) -> list[dict[str, Any]]:
    """DeepSeek 的网页搜索：和对话共用 API key，按次计费，搜不到不收费。"""

    response = await client.post("/v1/tools/search", json={"query": query[:128], "count": count})
    response.raise_for_status()
    return response.json().get("results") or []
