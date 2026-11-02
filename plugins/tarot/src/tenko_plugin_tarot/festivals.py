"""节日：占卜结果末尾附一句应景的话，不影响抽牌。

只认公历固定日期的节日；春节、七夕、中秋要换算农历，以后再加。
"""

from __future__ import annotations

from datetime import date

from .spreads import Draw

FESTIVALS: dict[tuple[int, int], tuple[str, str]] = {
    (1, 1): ("元旦", "新年第一天的牌，就当是今年给你的一句开场白吧。"),
    (2, 14): ("情人节", "情人节快乐～今天和感情有关的牌，格外值得多看一眼。"),
    (4, 1): ("愚人节", "今天是愚人节，牌说的话请自行判断真假。"),
    (10, 31): ("万圣节", "万圣节快乐，今晚的牌面好像比平时多了一分神秘。"),
    (12, 24): ("平安夜", "平安夜快乐，愿今晚的牌给你带来一点安心。"),
    (12, 25): ("圣诞节", "圣诞快乐！把这次的牌当成一份刚拆开的礼物吧。"),
}
FOOL = "major-00"


def note(day: date, draws: list[Draw]) -> str | None:
    """今天是节日就返回一句话；愚人节抽到愚者另有彩蛋。"""

    found = FESTIVALS.get((day.month, day.day))
    if found is None:
        return None
    name, line = found
    if name == "愚人节" and any(card == FOOL for card, _ in draws):
        return "愚人节抽到愚者，这大概是一年里最应景的一张牌了。"
    return f"🎉 {line}"
