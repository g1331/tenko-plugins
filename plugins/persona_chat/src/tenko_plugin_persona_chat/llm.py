"""调用 OpenAI 兼容的 /chat/completions；模型只能说话（send_message）或不说（stay_silent）。"""

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
                "text": {"type": "string", "description": "要发的话，一到两句"},
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
    """返回 send_message 的参数列表和 usage。assistant 的 content 一律丢弃。"""

    response = await client.post(
        "/chat/completions",
        json={
            "model": model,
            "messages": messages,
            # 群聊闲聊不需要推理；关掉思考才能用强制工具调用。
            "thinking": {"type": "disabled"},
            "tools": [SEND_MESSAGE, STAY_SILENT],
            "tool_choice": FORCE_SEND if force else "required",
            "max_tokens": max_tokens,
        },
    )
    response.raise_for_status()
    data = response.json()
    calls = []
    for call in data["choices"][0]["message"].get("tool_calls") or []:
        function = call.get("function") or {}
        if function.get("name") != "send_message":
            continue
        try:
            arguments = json.loads(function.get("arguments") or "")
        except ValueError:
            continue
        if isinstance(arguments, dict) and isinstance(arguments.get("text"), str):
            calls.append(arguments)
    return calls, data.get("usage") or {}
