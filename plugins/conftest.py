"""所有插件共用一个真实的 Tenko 宿主（Entari 只允许一个实例），不启动网络服务。"""

import json
import os
from types import SimpleNamespace

import pytest
import pytest_asyncio
from arclet.entari import Entari
from arclet.entari.config import EntariConfig
from tenko.host.plugins import PluginRuntime


@pytest.fixture(scope="session")
def host(tmp_path_factory):
    root = tmp_path_factory.mktemp("tenko-host")
    previous = os.getcwd()
    os.chdir(root)
    EntariConfig(root / "entari.yml")
    Entari(ignore_self_message=True)
    (root / "state.json").write_text(json.dumps({"enabled": ["repeater", "persona_chat"]}))
    yield SimpleNamespace(root=root)
    os.chdir(previous)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def runtime(host):
    runtime = PluginRuntime(host.root / "no-builtin", state_file=host.root / "state.json")
    await runtime.load_all(
        external_configs={
            "repeater": {"cooldown_seconds": 60},
            # 不调用真实接口：测试里替换 llm.chat。
            "persona_chat": {"api_key": "test", "batch_size": 3, "quiet_seconds": 0},
        }
    )
    yield runtime
    for plugin_id in ("repeater", "persona_chat"):
        runtime.unload(plugin_id)
