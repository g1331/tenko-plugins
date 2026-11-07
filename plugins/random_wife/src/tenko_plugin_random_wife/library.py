"""老婆库：插件数据目录下 ``wives/`` 里的图片，文件名（去掉后缀）就是老婆的名字。

旧版 bot 的 ``wife/`` 目录里的图片拷进来就能用，导入方法见 README。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from loguru import logger
from tenko_api.storage import data_dir

MAX_BYTES = 10 * 1024 * 1024
MAX_NAME = 30
MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


lock = asyncio.Lock()
"""改老婆库、婚姻登记和投稿记录时都先拿这把锁，同一时间只有一处在改。"""


class LibraryError(Exception):
    """要原样回给群友的错误。"""


def folder(kind: str = "wives") -> Path:
    path = data_dir() / kind
    path.mkdir(parents=True, exist_ok=True)
    return path


def wives() -> dict[str, Path]:
    return {
        path.stem: path
        for path in sorted(folder().iterdir())
        if path.is_file() and path.suffix.lower() in MIME
    }


def mime(data: bytes, path: Path) -> str:
    """按文件头判断：旧版老婆库里有不少 jpg 存成了 .png，反过来的也有。"""

    try:
        return MIME[suffix(data)]
    except LibraryError:
        return MIME[path.suffix.lower()]


def check_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise LibraryError("要给老婆起个名字")
    if len(name) > MAX_NAME:
        raise LibraryError(f"名字太长了，最多 {MAX_NAME} 个字")
    if name.startswith(".") or any(c in name for c in '/\\:*?"<>|') or not name.isprintable():
        raise LibraryError('名字里不能有 / \\ : * ? " < > | 这些符号')
    return name


def suffix(data: bytes) -> str:
    """按文件头判断格式，不信任 QQ 给的文件名。"""

    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    raise LibraryError("这不是能用的图片（只收 jpg、png、gif、webp、bmp）")


async def download(src: str) -> bytes:
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            async with client.stream("GET", src) as response:
                if response.status_code != 200:
                    raise LibraryError(f"图片下载失败（{response.status_code}）")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data += chunk
                    if len(data) > MAX_BYTES:
                        raise LibraryError("图片太大了，最多 10 MB")
    except httpx.HTTPError as error:
        logger.warning("随机老婆下载图片失败：{}", type(error).__name__)
        raise LibraryError("图片下载失败，稍后再试") from error
    return bytes(data)


def add(name: str, data: bytes) -> Path:
    if name in wives():
        raise LibraryError(f"{name} 已经在老婆库里了")
    path = folder() / f"{name}{suffix(data)}"
    path.write_bytes(data)
    return path


def remove(name: str) -> bool:
    path = wives().get(name)
    if path is None:
        return False
    path.unlink()
    return True
