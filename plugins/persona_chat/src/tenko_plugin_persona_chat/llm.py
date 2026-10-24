"""调用 OpenAI 兼容的 /chat/completions；模型只能说话（send_message）或不说（stay_silent），
顺手可以收藏表情包（save_sticker）。"""

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
                    "description": "适合在什么心情或场合发，例如“吃惊、无语”",
                },
            },
            "required": ["number", "name", "mood"],
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
    """返回 send_message 和 save_sticker 的参数列表（收藏的带 ``"tool": "save_sticker"``）和 usage。

    assistant 的 content 一律丢弃。
    """

    response = await client.post(
        "/chat/completions",
        json={
            "model": model,
            "messages": messages,
            # 群聊闲聊不需要推理；关掉思考才能用强制工具调用。
            "thinking": {"type": "disabled"},
            "tools": [SEND_MESSAGE, STAY_SILENT, SAVE_STICKER],
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
        if name not in ("send_message", "save_sticker"):
            continue
        try:
            arguments = json.loads(function.get("arguments") or "")
        except ValueError:
            continue
        if not isinstance(arguments, dict):
            continue
        if name == "save_sticker":
            calls.append({**arguments, "tool": name})
        elif isinstance(arguments.get("text"), str):
            calls.append(arguments)
    return calls, data.get("usage") or {}
