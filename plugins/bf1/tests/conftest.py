import pytest


@pytest.fixture(scope="session")
def module(runtime):
    return runtime.loaded_plugins["bf1"].module
