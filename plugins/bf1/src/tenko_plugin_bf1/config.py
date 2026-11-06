"""插件配置：``config/tenko.toml`` 里的 ``[plugin_config.bf1]``。"""

from __future__ import annotations

from arclet.entari import BasicConfModel, plugin


class Bf1Config(BasicConfModel):
    """对应 config/tenko.toml 中的 ``[plugin_config.bf1]``。"""

    base_url: str = "https://bfm.umrbotech.com"
    """bf-manager 地址，不带 /api/v1。"""


config = plugin.get_config(Bf1Config)
