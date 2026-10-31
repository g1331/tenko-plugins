"""武器、载具按类别和兵种分组，沿用旧 bot 的 -步枪、-突击兵、-坦克 这些子命令。

bf-manager 返回的每件武器、载具都带分类（简体）。兵种除了主武器那几类，旧 bot 还按 GUID
挂了各自的配枪和装备；这里换成 bf-manager 用的名字，GUID 和名字的对应取自 bf-manager 的
blaze_catalog.json。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .pages import cn


@dataclass(frozen=True)
class Group:
    label: str  # 显示在标题里
    names: tuple[str, ...]  # 命令名，第一个是主名
    categories: frozenset[str]
    extras: frozenset[str] = frozenset()  # 不在这些分类里、也算进来的武器或载具名

    def __contains__(self, item: dict[str, Any]) -> bool:
        # bf-manager 给的已经是简体，转一下以防万一
        return cn(item.get("category")) in self.categories or cn(item.get("name")) in self.extras


def _group(label: str, names: str, categories: str, extras: str = "") -> Group:
    return Group(
        label,
        tuple(names.split()),
        frozenset(categories.split()),
        frozenset(e for e in extras.split("|") if e),
    )


_ASSAULT = "冲锋枪 霰弹枪"
_SCOUT = "步枪 制式步枪"
_TANKS = "重型坦克 巡航坦克 轻型坦克 攻击坦克 突击装甲车"
_PLANES = "攻击机 轰炸机 战斗机 重型轰炸机"

WEAPON_GROUPS = (
    _group(
        "突击兵",
        "突击兵 突击 土鸡兵 土鸡",
        _ASSAULT,
        "M1917 卡宾枪（巡逻）|费罗梅尔停止手枪（自动）|短管霰弹枪|炸药|反坦克手榴弹|反坦克地雷"
        "|反坦克火箭炮|防空火箭炮",
    ),
    _group(
        "医疗兵",
        "医疗兵 医疗",
        "半自动步枪",
        "步枪手榴弹（破片）|步枪手榴弹（烟雾）|步枪手榴弹（高爆）|医疗用针筒|医护箱|绷带包"
        "|M1917 卡宾枪（巡逻）|卡尔卡诺 M91 卡宾枪（巡逻）",
    ),
    _group(
        "支援兵",
        "支援兵 支援",
        "轻机枪",
        "P08 Artillerie|皮珀 M1893|M1911（加长）|Mle 1903（加长）|C93（卡宾枪）|弹药箱|弹药包"
        "|迫击炮（空爆）|迫击炮（高爆）|维修工具|磁吸地雷|十字弓发射器（高爆）|十字弓发射器（破片）",
    ),
    _group(
        "侦察兵",
        "侦察兵 侦察 斟茶兵 斟茶",
        _SCOUT,
        "信号枪（侦察）|信号枪（闪光）|K 弹|绊索炸弹（高爆）|绊索炸弹（燃烧）|绊索炸弹（毒气）",
    ),
    _group("精英兵", "精英兵", "战场装备", "奇兵棒"),
    _group("冲锋枪", "冲锋枪", "冲锋枪"),
    _group("霰弹枪", "霰弹枪 散弹枪", "霰弹枪"),
    _group("轻机枪", "轻机枪 机枪", "轻机枪"),
    _group("半自动步枪", "半自动步枪 半自动", "半自动步枪"),
    _group("步枪", "步枪 狙击枪", _SCOUT),
    _group("佩枪", "佩枪 手枪", "佩枪"),
    _group("配备", "配备 装备", "配备"),
    _group("手榴弹", "手榴弹 手雷 投掷物", "手榴弹"),
    _group("近战武器", "近战", "近战武器"),
    _group("坦克/驾驶员", "驾驶员 坦克驾驶员", "坦克/驾驶员"),
)

VEHICLE_GROUPS = (
    _group("坦克", "坦克", _TANKS),
    _group(
        "地面载具",
        "地面",
        f"{_TANKS} 火炮装甲车 地面载具 马匹 定点武器",
        "装甲列车|Char 2C",
    ),
    _group("飞机", "飞机", _PLANES),
    _group("飞船", "飞船 飞艇", "飞船", "飞船 L30"),
    _group("空中载具", "空中", f"{_PLANES} 飞船", "飞船 L30"),
    _group("海上载具", "海上", "船只 驱逐舰", "无畏舰"),
    _group("定点武器", "定点", "定点武器"),
    _group("机械巨兽", "巨兽 机械巨兽", "机械巨兽"),
)

GROUPS = {"weapons": WEAPON_GROUPS, "vehicles": VEHICLE_GROUPS}


def find(kind: str, word: str) -> Group | None:
    return next((g for g in GROUPS[kind] if word in g.names), None)
