"""勋带、勋章、狗牌的名字和分类，取自 bf-manager 网页的图鉴数据，存在 assets/collection.json。

图片不随插件发（光狗牌就有三百多张、七 MB 多），渲染时直接引用 bf-manager 网站上的静态图。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar


@dataclass(frozen=True)
class Ribbon:
    code: str  # r01…r47
    category: str
    name: str
    description: str


@dataclass(frozen=True)
class Medal:
    code: str  # m05、mxp107 这样
    category: str
    name: str
    stages: int


@dataclass(frozen=True)
class Dogtag:
    id: str  # df082、dbxp516 这样
    category: str
    name: str


def _load() -> tuple[list[Ribbon], list[Medal], list[Dogtag]]:
    path = Path(__file__).parent / "assets" / "collection.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return (
        [Ribbon(**x) for x in data["ribbons"]],
        [Medal(**x) for x in data["medals"]],
        [Dogtag(**x) for x in data["dogtags"]],
    )


RIBBONS, MEDALS, DOGTAGS = _load()
RIBBON = {r.code: r for r in RIBBONS}
MEDAL = {m.code: m for m in MEDALS}
DOGTAG = {t.id: t for t in DOGTAGS}


T = TypeVar("T", Ribbon, Medal, Dogtag)


def grouped(items: list[T]) -> list[tuple[str, list[T]]]:
    """按分类分组，分类和组内都保持图鉴里的顺序。"""

    groups: dict[str, list[T]] = {}
    for item in items:
        groups.setdefault(item.category, []).append(item)
    return list(groups.items())


def image(kind: str, key: str, base_url: str) -> str:
    """kind 为 ribbons、medals、dogtags；网页 apps/web/public/bf1/codex 下的图。"""

    return f"{base_url.rstrip('/')}/bf1/codex/{kind}/{key}.webp"
