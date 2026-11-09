"""BF6 图鉴：把统计里的内部 ID（mg_l110、air_panthera、mpaftermath……）翻成名字和图片。

接口条目自带 ``name`` / ``image`` 时用接口的；没有时查随包的 ``assets/catalog.json``
（由 bf-manager 网页的图鉴生成，来源写在文件的 ``source`` 里）。两边都没有就显示 ID。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from . import api

CATALOG = Path(__file__).parent / "assets" / "catalog.json"


@cache
def _catalog() -> dict[str, dict[str, dict[str, Any]]]:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def _entry(kind: str, id: str) -> dict[str, Any]:
    return _catalog()[kind].get(id, {})


def name(kind: str, item: dict[str, Any] | str) -> str:
    if isinstance(item, str):
        return _entry(kind, item).get("name") or item
    return item.get("name") or _entry(kind, item["id"]).get("name") or item["id"]


def image(kind: str, item: dict[str, Any] | str) -> str | None:
    """完整图片地址，没有图返回 None。"""

    if isinstance(item, str):
        return api.site(_entry(kind, item).get("image"))
    return api.site(item.get("image") or _entry(kind, item["id"]).get("image"))


# —— 分类 ——


def weapon_category(id: str) -> str:
    return _entry("weapon_categories", id.split("_")[0]).get("name") or "其他"


def vehicle_type(id: str) -> str | None:
    return _entry("vehicles", id).get("type")


def vehicle_group(id: str) -> str:
    """和网页一样：图鉴里标成海上作战的算海上，air_ 开头算空中，其余算地面。"""

    if vehicle_type(id) == "海上作战":
        return "nav"
    return "air" if id.startswith("air_") else "sur"


def gadget_group(id: str) -> str:
    if id.split("_")[0] in ("rl", "gl", "il"):
        return "发射器"
    if id == "c4" or id.startswith("mine_"):
        return "爆炸物"
    if id.startswith("gren_") or id == "tknife":
        return "投掷物"
    if id.startswith("callin_"):
        return "呼叫支援"
    return "其他装备"


@dataclass(frozen=True)
class Group:
    """武器、载具排行能筛的类别：命令里认的说法，以及怎么从条目 ID 判断归属。"""

    key: str
    label: str
    names: tuple[str, ...]


WEAPON_GROUPS = (
    Group("ar", "突击步枪", ("突击步枪", "突击")),
    Group("crb", "卡宾枪", ("卡宾枪", "卡宾")),
    Group("smg", "冲锋枪", ("冲锋枪", "冲锋", "PDW")),
    Group("mg", "机枪", ("机枪", "轻机枪")),
    Group("snp", "狙击步枪", ("狙击步枪", "狙击枪", "狙击", "栓狙")),
    Group("dmr", "精准射手步枪", ("精准射手步枪", "精确射手步枪", "射手步枪", "连狙", "DMR")),
    Group("sg", "霰弹枪", ("霰弹枪", "散弹枪", "霰弹", "喷子")),
    Group("pst", "手枪", ("手枪", "配枪", "副武器")),
    # 配备不在武器列表里，是单独的一份统计
    Group("gadgets", "配备", ("配备", "装备", "小工具")),
)
VEHICLE_GROUPS = (
    Group("air", "空中载具", ("空中", "飞机", "空载")),
    Group("sur", "地面载具", ("地面", "陆地", "地载")),
    Group("nav", "海上载具", ("海上", "船", "海军")),
)
GROUPS = {"weapons": WEAPON_GROUPS, "vehicles": VEHICLE_GROUPS}


def find_group(kind: str, word: str) -> Group | None:
    folded = word.casefold()
    return next((g for g in GROUPS[kind] if folded in (n.casefold() for n in g.names)), None)


def in_group(kind: str, group: Group, id: str) -> bool:
    if kind == "weapons":
        return id.split("_")[0] == group.key
    return vehicle_group(id) == group.key
