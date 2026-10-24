import os

import pytest

from secsdk import Client, CoreSettings

# Variables that could leak real credentials or change behaviour in tests.
_SCRUB_PREFIXES = ("SECSDK_", "TENABLE_", "TIO_", "AZURE_", "FEEDLY_", "NVD_", "SEC_")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith(_SCRUB_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # no stray .env file
    monkeypatch.setenv("SECSDK_AZURE_USE_CLI", "false")
    monkeypatch.setattr("secsdk.http.time.sleep", lambda s: None)  # instant retries


@pytest.fixture
def settings(tmp_path):
    return CoreSettings(
        cache_enabled=False, rate_limit=False, max_retries=2, cache_dir=tmp_path / "cache"
    )


@pytest.fixture
def client(settings):
    with Client(settings) as c:
        yield c
