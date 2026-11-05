"""调用 OpenAI 兼容的 /chat/completions；模型只能说话（send_message）或不说（stay_silent），
顺手可以收藏表情包（save_sticker），不确定的事实可以先搜（search），
也能用别的插件的查询功能（run_command），答应到点做的事登记成提醒（remind）。"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from loguru import logger

# 先看懂再开口：线上她常把别人两人之间的“你”当成在说她、把甲说的事安到乙头上（2026-11 日志里
# 31 段）。看场面、决定说不说、怎么说原本挤在一步里，思考也就几十个 token。让她每次先写一句
# 场面再写话，参数按顺序生成，写话时就有这句垫着；日志里也能看到她到底是怎么看的。
SCENE = {
    "type": "string",
    "description": (
        "先看懂再说，一句话，群友看不到：最新几条是谁在跟谁说什么，"
        "有没有人在跟你说话（@你、引用你、接你刚才的话）"
    ),
}

SEND_MESSAGE = {
    "type": "function",
    "function": {
        "name": "send_message",
        "description": "在当前会话发一条消息。不想说话就不要调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "scene": SCENE,
                "to": {
                    "type": "integer",
                    "description": (
                        "这句在接哪条消息的编号，例如 3；对着大家随口说、不接哪一条就填 0"
                    ),
                },
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
            "required": ["scene", "to", "text"],
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
        "parameters": {
            "type": "object",
            "properties": {"scene": SCENE},
            "required": ["scene"],
        },
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
        "description": (
            "悄悄上网搜一下，结果出来后再回复。对方问的、冲你说的话里有你不确定、没听过的事实、"
            "梗、外号、人名时用，别反问对方。"
        ),
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
REMIND = {
    "type": "function",
    "function": {
        "name": "remind",
        "description": (
            "答应了到某个时间去做的事（叫人起床、提醒上号、到点喊一声），和说话一起调用登记下来："
            "到点插件会叫你 @ 那个人说一句。只提醒一次；不调用，到点你就想不起来。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "at": {
                    "type": "string",
                    "description": "到点的本地时间，照提示里的当前时间推算，例如“2026-11-03 09:00”",
                },
                "mention": {
                    "type": "integer",
                    "description": "要提醒的人说过的某条消息编号，例如 3",
                },
                "note": {
                    "type": "string",
                    "description": "到点要做什么，30 字以内，例如“叫他起床”",
                },
            },
            "required": ["at", "mention", "note"],
        },
    },
}
CANCEL_REMINDER = {
    "type": "function",
    "function": {
        "name": "cancel_reminder",
        "description": (
            "对方说不用提醒了、时间改了时，取消提示里列出的某条提醒（改时间就取消后重新 remind）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "提示里提醒的编号"}},
            "required": ["id"],
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
    REMIND,
    CANCEL_REMINDER,
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
    否则 content 丢弃。``force`` 时思考完还是没开口，就不思考、强制只能说话再问一次；
    content 是她把思考写错了地方时（见 :func:`_chat`），不思考、只能用工具再问一次。
    """

    calls, usage, aloud = await _chat(client, model, messages, force, max_tokens, effort)
    if effort and (force or aloud) and not any("tool" not in c for c in calls):
        kept = [c for c in calls if c["tool"] in SIDE_TOOLS]
        again, more, _ = await _chat(client, model, messages, force, max_tokens, "")
        calls = kept + again
        usage = {
            **more,
            **{k: usage.get(k, 0) + more.get(k, 0) for k in _BILLED if k in usage or k in more},
        }
    return calls, usage


# usage 里按 token 计费的几项：再问一次时两次相加；prompt_tokens 取后一次，校准上下文大小用。
_BILLED = ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "completion_tokens")
# 和说话一起调用、说完话插件再办的工具：没开口要强制再问时留着，不然白调了。
SIDE_TOOLS = ("save_sticker", "edit_sticker", "delete_sticker", "remind", "cancel_reminder")
# 思考的 token 也算在 max_tokens 里；给它另留一份，不然想完了没字数说话。
_THINK_TOKENS = 2048


async def _chat(
    client: httpx.AsyncClient,
    model: str,
    messages: list[dict[str, Any]],
    force: bool,
    max_tokens: int,
    effort: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    """第三项为真：思考时她没调 send_message、思考是空的，content 当成她的思考，没当成要说的话。"""

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "tools": TOOLS,
        "max_tokens": max_tokens,
    }
    if effort:
        payload |= {
            "messages": _with_reasoning(messages),
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
        # 工具都从 TOOLS 里认，新加的工具不会被漏掉。
        if name not in {t["function"]["name"] for t in TOOLS}:
            continue
        try:
            arguments = json.loads(function.get("arguments") or "")
        except ValueError:
            arguments = {}
        if not isinstance(arguments, dict):
            continue
        if name == "stay_silent":
            # 不说话也带回她看到的场面，只用来记日志。
            calls.append({"tool": name, "scene": str(arguments.get("scene") or "")})
        elif name != "send_message":
            calls.append({**arguments, "tool": name})
        elif isinstance(arguments.get("text"), str):
            calls.extend(_quoted(arguments))
    said = (message.get("content") or "").strip()
    usage = data.get("usage") or {}
    if not (effort and said and not any("tool" not in c for c in calls)):
        return calls, usage, False
    thought = " ".join((message.get("reasoning_content") or "").split())[:150]
    if not thought:
        # 思考是空的，content 多半就是她的思考：11-05 她把“这个话题我插不上什么，可以短回一句
        # ……stay_silent也行”整段写进 content，拆成两条发到了群里。思考为空时她本来也没想，
        # 交给不思考、只能用工具的那次重问，不丢什么（这类回复约占两成，多是单发一张表情包）。
        logger.info("AI群友思考是空的，正文不当成要说的话：{}", " ".join(said.split())[:150])
        return calls, usage, True
    # 思考模式下她常直接写字、不调 send_message，也就没填 scene：拿思考的开头当她看到的场面。
    calls.extend(_quoted({"text": said, "scene": f"（想）{thought}"}))
    return calls, usage, False


def _with_reasoning(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """思考模式下，最后一条 user 之后的 assistant 算同一轮，DeepSeek 要求带回 reasoning_content，
    缺了整个请求 400。她刚说完话、还没人接时就是这样：记录里存的只有说的话，补个空的。"""

    last = max((i for i, m in enumerate(messages) if m["role"] == "user"), default=-1)
    return [
        {**m, "reasoning_content": m.get("reasoning_content", "")}
        if i > last and m["role"] == "assistant"
        else m
        for i, m in enumerate(messages)
    ]


# 直接写字时没法填 reply_to，规则让她在那句开头写“回复#编号 ”；她偶尔照记录的格式抄成
# “回复#12（甲说的“…”） ”，括号一起去掉。有了 to 以后她还会只写“#781 ”（回放里见到），
# 同样当成在接那条，不然编号就原样发到群里。
_QUOTE = re.compile(r"^(?:回复)?#(\d+)(?:（[^\n]*?）)?\s*", re.M)


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
            # @ 只跟着第一次，不然拆开的每次都 @ 一遍；她看到的场面每次都带着，记日志用。
            keep = ("scene",) if calls else ("scene", "mention")
            extra = {k: v for k, v in call.items() if k in keep}
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
