"""插件配置：``config/tenko.toml`` 里的 ``[plugin_config.bf6]``。"""

from __future__ import annotations

from arclet.entari import BasicConfModel, plugin


class Bf6Config(BasicConfModel):
    """对应 config/tenko.toml 中的 ``[plugin_config.bf6]``。"""

    base_url: str = "https://bfm.umrbotech.com"
    """bf-manager 地址，不带 /api/v1；武器、地图等图片也从这里加载。"""


config = plugin.get_config(Bf6Config)
