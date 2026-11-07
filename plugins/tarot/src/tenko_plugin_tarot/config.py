"""插件配置：``config/tenko.toml`` 里的 ``[plugin_config.tarot]``。"""

from __future__ import annotations

from arclet.entari import BasicConfModel, plugin


class TarotConfig(BasicConfModel):
    """对应 config/tenko.toml 中的 ``[plugin_config.tarot]``。"""

    three_card_minutes: int = 60
    """三张牌阵两次之间至少隔多少分钟，从上次占卜起算；0 表示不限。"""
    celtic_cross_daily: bool = True
    """凯尔特十字每人每天一次，当天再占还是那一组；关掉后不限次数。"""


config = plugin.get_config(TarotConfig)
