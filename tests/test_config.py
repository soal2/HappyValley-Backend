import re

import pytest
from pydantic import ValidationError

from app.config import Settings, expand_cors_origins


def test_cors_origins_include_equivalent_local_hostname() -> None:
    assert expand_cors_origins("http://localhost:5173") == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
    assert expand_cors_origins("http://127.0.0.1:5174") == [
        "http://127.0.0.1:5174",
        "http://localhost:5174",
    ]


def test_cors_origins_preserve_remote_origins_and_remove_duplicates() -> None:
    assert expand_cors_origins(
        " https://app.example.com, http://localhost:5173, http://127.0.0.1:5173 "
    ) == [
        "https://app.example.com",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


def test_ai_network_mode_defaults_to_auto_and_rejects_unknown_values() -> None:
    assert Settings(_env_file=None).AI_NETWORK_MODE == "auto"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, AI_NETWORK_MODE="unknown")  # type: ignore[arg-type]


def test_default_cors_regex_allows_lingguang_hosts_and_sandbox_origin() -> None:
    pattern = Settings(_env_file=None).CORS_ORIGIN_REGEX

    assert re.fullmatch(pattern, "null")
    assert re.fullmatch(pattern, "https://preview.lingguangcontent.com")
    assert re.fullmatch(pattern, "https://release-123.lingguangcontent.com")
    assert re.fullmatch(pattern, "https://lingguangcontent.com")
    assert re.fullmatch(pattern, "https://example.com") is None
    assert re.fullmatch(pattern, "null.example.com") is None
