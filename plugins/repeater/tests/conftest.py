"""用真实的 Tenko 宿主加载复读机插件，不启动网络服务。"""

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
    (root / "state.json").write_text(json.dumps({"enabled": ["repeater"]}))
    yield SimpleNamespace(root=root)
    os.chdir(previous)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def runtime(host):
    runtime = PluginRuntime(host.root / "no-builtin", state_file=host.root / "state.json")
    await runtime.load_all(external_configs={"repeater": {"cooldown_seconds": 60}})
    yield runtime
    runtime.unload("repeater")


@pytest.fixture(scope="session")
def module(runtime):
    return runtime.loaded_plugins["repeater"].module
