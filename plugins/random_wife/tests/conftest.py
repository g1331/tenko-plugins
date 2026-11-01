import pytest


@pytest.fixture(scope="session")
def module(runtime):
    return runtime.loaded_plugins["random_wife"].module
