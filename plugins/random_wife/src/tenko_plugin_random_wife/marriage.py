"""抽老婆的规则，不碰消息和文件，方便单独测试。

每个群各自一份“婚姻登记”，每天零点清空：

- 已经有老婆的人再抽，还是自己的老婆；
- 抽到没人要的，就成了你的；
- 抽到别人的老婆只是看看，但在拿到老婆之前，同一个别人的老婆抽到第二次，就抢过来。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

RECENT = 10
"""记住最近抽到的多少个别人的老婆；第二次抽到其中一个就抢走。"""


@dataclass(frozen=True)
class Outcome:
    kind: Literal["mine", "new", "seen", "stolen"]
    """mine：本来就有；new：没人要的归你了；seen：是别人的；stolen：抢过来了。"""
    wife: str
    owner: str | None = None
    """seen 时是现在的主人，stolen 时是被抢的人（都是显示名）。"""


class Registry:
    """一天的登记。状态是可以直接存成 JSON 的字典。

    ``groups[群号] = {"owners": {老婆: {"id", "name"}}, "recent": {QQ: [老婆, ...]}}``
    """

    def __init__(self, state: dict[str, Any] | None, today: str) -> None:
        if not state or state.get("date") != today:
            state = {"date": today, "groups": {}}
        self.state = state

    def _group(self, group: str) -> dict[str, Any]:
        return self.state["groups"].setdefault(group, {"owners": {}, "recent": {}})

    def wife_of(self, group: str, user: str) -> str | None:
        owners = self._group(group)["owners"]
        return next((wife for wife, who in owners.items() if who["id"] == user), None)

    def draw(self, group: str, user: str, name: str, drawn: str, library: set[str]) -> Outcome:
        """``drawn`` 是这次随机到的老婆，``library`` 是老婆库里现有的全部名字。"""

        data = self._group(group)
        owners, recent = data["owners"], data["recent"]
        for gone in [wife for wife in owners if wife not in library]:
            del owners[gone]  # 被管理员删掉的老婆不再算数
        if mine := self.wife_of(group, user):
            return Outcome("mine", mine)
        seen = recent.get(user, [])
        owner = owners.get(drawn)
        if owner is None:
            owners[drawn] = {"id": user, "name": name}
            recent.pop(user, None)
            return Outcome("new", drawn)
        if drawn in seen:
            owners[drawn] = {"id": user, "name": name}
            recent.pop(user, None)
            return Outcome("stolen", drawn, owner["name"])
        recent[user] = [*seen, drawn][-RECENT:]
        return Outcome("seen", drawn, owner["name"])

    def divorce(self, group: str, user: str) -> str | None:
        """返回离掉的老婆；本来就没有时返回 None。"""

        wife = self.wife_of(group, user)
        if wife is not None:
            del self._group(group)["owners"][wife]
        return wife
