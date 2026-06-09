"""Tests for the environment-variable contract of BPConfig (Phase 1)."""
from blue_prism_mcp.config import BPConfig


def test_from_env_defaults_with_empty_environment():
    cfg = BPConfig.from_env(env={})
    assert cfg.base_url == ""
    assert cfg.verify_ssl is True
    assert cfg.cache_ttl == 30.0
    assert cfg.paging_mode == "auto"
    assert cfg.page_size == 1000
    assert cfg.page_size_param == "pageSize"
    assert cfg.enable_actions is False


def test_from_env_reads_all_fields():
    cfg = BPConfig.from_env(
        env={
            "BP_API_BASE_URL": "https://bp.example/api/v7",
            "BP_API_USERNAME": "svc",
            "BP_API_PASSWORD": "secret",
            "BP_API_VERIFY_SSL": "false",
            "BP_API_CACHE_TTL": "5",
            "BP_API_PAGING_MODE": "offset",
            "BP_API_PAGE_SIZE": "50",
            "BP_API_MAX_PAGES": "10",
            "BP_API_PAGE_SIZE_PARAM": "limit",
            "BP_API_PAGE_TOKEN_PARAM": "cursor",
            "BP_API_PAGE_OFFSET_PARAM": "skip",
            "BP_ENABLE_ACTIONS": "true",
        }
    )
    assert cfg.base_url == "https://bp.example/api/v7"
    assert cfg.username == "svc"
    assert cfg.password == "secret"
    assert cfg.verify_ssl is False
    assert cfg.cache_ttl == 5.0
    assert cfg.paging_mode == "offset"
    assert cfg.page_size == 50
    assert cfg.max_pages == 10
    assert cfg.page_size_param == "limit"
    assert cfg.page_token_param == "cursor"
    assert cfg.page_offset_param == "skip"
    assert cfg.enable_actions is True


def test_base_url_trailing_slash_is_stripped():
    # A trailing slash would otherwise yield '.../api/v7//resources' on every
    # request (auth included), which some proxies route as a distinct 404.
    assert BPConfig(base_url="https://bp.example/api/v7/").base_url == "https://bp.example/api/v7"
    assert BPConfig(base_url="https://bp.example/api/v7//").base_url == "https://bp.example/api/v7"
    assert BPConfig.from_env(env={"BP_API_BASE_URL": "https://bp.example/"}).base_url == "https://bp.example"


def test_config_is_frozen():
    cfg = BPConfig.from_env(env={})
    try:
        cfg.base_url = "mutated"  # type: ignore[misc]
    except Exception as exc:  # FrozenInstanceError
        assert "cannot assign" in str(exc).lower() or "frozen" in type(exc).__name__.lower()
    else:
        raise AssertionError("BPConfig should be immutable")
