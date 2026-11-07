"""每天抽老婆和离婚：读写当天的婚姻登记，回复抽到的老婆。规则本身在 :mod:`.marriage`。"""

from __future__ import annotations

import random
from datetime import datetime

from arclet.entari import MessageChain
from satori import Image, Text
from tenko_api.storage import Store

from . import library
from .library import lock
from .marriage import Registry

_registry_store = Store("marriages", version=1)
rng = random.Random()


def _today() -> str:
    return datetime.now().date().isoformat()


def _photo(path) -> Image:
    data = path.read_bytes()
    return Image.of(raw=data, mime=library.mime(data, path))


async def draw(group: str, user: str, name: str) -> MessageChain | str:
    wives = library.wives()
    if not wives:
        return "老婆库还是空的，请管理员先用 /添加老婆 加几个"
    async with lock:
        registry = Registry(_registry_store.load(), _today())
        outcome = registry.draw(group, user, name, rng.choice(list(wives)), set(wives))
        _registry_store.save(registry.state)
    wife, photo = outcome.wife, _photo(wives[outcome.wife])
    if outcome.kind in ("mine", "new"):
        return MessageChain([Text(f"你今天的老婆是{wife}哦~不想要了可以 /离婚\n"), photo])
    if outcome.kind == "stolen":
        return MessageChain(
            [Text(f"你抢走了【{outcome.owner}】的老婆{wife}~\n"), photo, Text("\n达成成就【NTR】")]
        )
    return MessageChain([Text(f"你抽到了{wife}，是【{outcome.owner}】的老婆哦~\n"), photo])


async def divorce(group: str, user: str) -> str:
    async with lock:
        registry = Registry(_registry_store.load(), _today())
        wife = registry.divorce(group, user)
        _registry_store.save(registry.state)
    if wife is None:
        return "你还没有老婆，先 /抽老婆 抽一个吧~"
    return f"你已经和{wife}离婚了~"
