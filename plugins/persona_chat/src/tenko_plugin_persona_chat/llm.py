"""调用 OpenAI 兼容的 /chat/completions；模型只能说话（send_message）或不说（stay_silent），
顺手可以收藏表情包（save_sticker），不确定的事实可以先搜（search）。"""

from __future__ import annotations

import json
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
                        "要发的话；想分几条连着发就换行，每行一条，最多三条；"
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
FORCE_SEND = {"type": "function", "function": {"name": "send_message"}}


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


async def chat(
    client: httpx.AsyncClient,
    *,
    model: str,
    messages: list[dict[str, Any]],
    force: bool,
    max_tokens: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """返回 send_message 和其他工具的参数列表（其他工具的带 ``"tool"``）和 usage。

    assistant 的 content 一律丢弃。
    """

    response = await client.post(
        "/chat/completions",
        json={
            "model": model,
            "messages": messages,
            # 群聊闲聊不需要推理；关掉思考才能用强制工具调用。
            "thinking": {"type": "disabled"},
            "tools": [SEND_MESSAGE, STAY_SILENT, SAVE_STICKER, EDIT_STICKER, SEARCH],
            "tool_choice": FORCE_SEND if force else "required",
            "max_tokens": max_tokens,
        },
    )
    response.raise_for_status()
    data = response.json()
    calls = []
    for call in data["choices"][0]["message"].get("tool_calls") or []:
        function = call.get("function") or {}
        name = function.get("name")
        if name not in ("send_message", "save_sticker", "edit_sticker", "search"):
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
            calls.append(arguments)
    return calls, data.get("usage") or {}


async def search(client: httpx.AsyncClient, query: str, count: int = 4) -> list[dict[str, Any]]:
    """DeepSeek 的网页搜索：和对话共用 API key，按次计费，搜不到不收费。"""

    response = await client.post("/v1/tools/search", json={"query": query[:128], "count": count})
    response.raise_for_status()
    return response.json().get("results") or []
