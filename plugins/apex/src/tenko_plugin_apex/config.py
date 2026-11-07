"""插件配置：``config/tenko.toml`` 里的 ``[plugin_config.apex]``。"""

from __future__ import annotations

from arclet.entari import BasicConfModel, plugin


class ApexConfig(BasicConfModel):
    """对应 config/tenko.toml 中的 ``[plugin_config.apex]``。"""

    api_key: str = ""
    """Apex Legends Status 的 API key，在 https://portal.apexlegendsapi.com 免费申请。"""


config = plugin.get_config(ApexConfig)
