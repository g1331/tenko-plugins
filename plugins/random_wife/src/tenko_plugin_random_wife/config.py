"""插件配置：``config/tenko.toml`` 里的 ``[plugin_config.random_wife]``。"""

from __future__ import annotations

from arclet.entari import BasicConfModel, plugin


class RandomWifeConfig(BasicConfModel):
    """对应 config/tenko.toml 中的 ``[plugin_config.random_wife]``。"""

    review_group: str = ""
    """群员投稿的老婆发到这个群审核，群管理员引用回复 y 同意；留空时发给 Master 审核。"""


config = plugin.get_config(RandomWifeConfig)
