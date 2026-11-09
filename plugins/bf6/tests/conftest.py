import pytest


@pytest.fixture(scope="session")
def module(runtime):
    return runtime.loaded_plugins["bf6"].module
