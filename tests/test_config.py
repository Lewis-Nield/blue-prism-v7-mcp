"""Tests for the environment-variable contract of BPConfig (Phase 1)."""

from blue_prism_v7_mcp.config import BPConfig


def test_from_env_defaults_with_empty_environment():
    cfg = BPConfig.from_env(env={})
    assert cfg.base_url == ""
    assert cfg.auth_url == ""
    assert cfg.token_scope == "bp-api bpserver"
    assert cfg.verify_ssl is True
    assert cfg.request_timeout == 30.0
    assert cfg.cache_ttl == 30.0
    assert cfg.paging_mode == "token"  # v7 is token-paged everywhere
    assert cfg.page_size == 1000
    assert cfg.page_size_param == "itemsPerPage"
    assert cfg.enable_actions is False
    assert cfg.data_source == "live"
    assert cfg.audit_log_path == ""
    assert cfg.pii_backend == "null"
    assert cfg.pii_spacy_model == "en_core_web_sm"
    assert cfg.pii_custom_patterns == ()


def test_transport_governance_is_off_unless_configured():
    # A distributable artifact must not change its posture on upgrade: with no
    # transport settings the client emits exactly what it did before Phase 12.
    cfg = BPConfig.from_env(env={})
    assert cfg.max_requests_per_second == 0.0  # no limiter constructed
    assert cfg.max_concurrency == 0  # no semaphore
    assert cfg.pool_maxsize == 0  # requests' own pool default left alone
    assert cfg.max_retries == 0  # no retry layer
    # Only meaningful once the switches above are on.
    assert cfg.max_burst == 10
    assert cfg.limiter_timeout_seconds == 10.0
    assert cfg.retry_base_delay == 0.5


def test_from_env_reads_all_fields():
    cfg = BPConfig.from_env(
        env={
            "BP_API_BASE_URL": "https://bp.example/api/v7",
            "BP_AUTH_URL": "https://auth.example",
            "BP_CLIENT_ID": "svc-client",
            "BP_CLIENT_SECRET": "secret",
            "BP_TOKEN_SCOPE": "bp-api bpserver",
            "BP_API_VERIFY_SSL": "false",
            "BP_API_TIMEOUT": "60",
            "BP_API_CACHE_TTL": "5",
            "BP_API_PAGING_MODE": "offset",
            "BP_API_PAGE_SIZE": "50",
            "BP_API_MAX_PAGES": "10",
            "BP_API_PAGE_SIZE_PARAM": "limit",
            "BP_API_PAGE_TOKEN_PARAM": "cursor",
            "BP_API_PAGE_OFFSET_PARAM": "skip",
            "BP_API_MAX_REQUESTS_PER_SECOND": "5",
            "BP_API_MAX_BURST": "12",
            "BP_API_MAX_CONCURRENCY": "8",
            "BP_API_LIMITER_TIMEOUT": "15",
            "BP_API_POOL_MAXSIZE": "16",
            "BP_API_MAX_RETRIES": "2",
            "BP_API_RETRY_BASE_DELAY": "0.25",
            "BP_ENABLE_ACTIONS": "true",
            "BP_DATA_SOURCE": "mock",
            "BP_AUDIT_LOG_PATH": "/var/log/blue-prism-v7-mcp/audit.jsonl",
            "BP_PII_BACKEND": "regex",
            "BP_PII_SPACY_MODEL": "en_core_web_lg",
            "BP_PII_CUSTOM_PATTERNS": '[{"name": "CLIENT_REF", "pattern": "CLT-\\\\d{8}"}]',
        }
    )
    assert cfg.base_url == "https://bp.example/api/v7"
    assert cfg.auth_url == "https://auth.example"
    assert cfg.client_id == "svc-client"
    assert cfg.client_secret == "secret"
    assert cfg.token_scope == "bp-api bpserver"
    assert cfg.verify_ssl is False
    assert cfg.request_timeout == 60.0
    assert cfg.cache_ttl == 5.0
    assert cfg.paging_mode == "offset"
    assert cfg.page_size == 50
    assert cfg.max_pages == 10
    assert cfg.page_size_param == "limit"
    assert cfg.page_token_param == "cursor"
    assert cfg.page_offset_param == "skip"
    assert cfg.max_requests_per_second == 5.0
    assert cfg.max_burst == 12
    assert cfg.max_concurrency == 8
    assert cfg.limiter_timeout_seconds == 15.0
    assert cfg.pool_maxsize == 16
    assert cfg.max_retries == 2
    assert cfg.retry_base_delay == 0.25
    assert cfg.enable_actions is True
    assert cfg.data_source == "mock"
    assert cfg.audit_log_path == "/var/log/blue-prism-v7-mcp/audit.jsonl"
    assert cfg.pii_backend == "regex"
    assert cfg.pii_spacy_model == "en_core_web_lg"
    assert cfg.pii_custom_patterns == (("CLIENT_REF", "CLT-\\d{8}"),)


def test_pii_backend_is_normalised_from_the_environment():
    # Case/whitespace slips are config footguns, not deployment decisions;
    # genuinely unknown values still refuse to start in build_scrubber.
    assert BPConfig.from_env(env={"BP_PII_BACKEND": " ReGeX "}).pii_backend == "regex"


def test_data_source_is_normalised_from_the_environment():
    # Same rule as pii_backend: forgive casing/whitespace noise here; unknown
    # values still refuse to start in the server's build_client.
    assert BPConfig.from_env(env={"BP_DATA_SOURCE": " MoCk "}).data_source == "mock"


def test_malformed_pii_custom_patterns_fail_loudly():
    # A deployment that configured extra redaction must not run without it:
    # bad JSON, a non-object entry, and a missing key all raise.
    for bad in ("not json", '{"name": "X"}', '[{"name": "X"}]', "[42]"):
        try:
            BPConfig.from_env(env={"BP_PII_CUSTOM_PATTERNS": bad})
        except (ValueError, TypeError) as exc:
            assert "BP_PII_CUSTOM_PATTERNS" in str(exc)
        else:
            raise AssertionError(f"expected {bad!r} to be rejected")


def test_url_trailing_slashes_are_stripped():
    # A trailing slash would otherwise yield '.../api/v7//resources' (and
    # '.../connect/token' equivalents) on every request, which some proxies
    # route as a distinct 404.
    assert BPConfig(base_url="https://bp.example/api/v7/").base_url == "https://bp.example/api/v7"
    assert BPConfig(base_url="https://bp.example/api/v7//").base_url == "https://bp.example/api/v7"
    assert BPConfig(auth_url="https://auth.example/").auth_url == "https://auth.example"
    assert (
        BPConfig.from_env(env={"BP_API_BASE_URL": "https://bp.example/"}).base_url
        == "https://bp.example"
    )


def test_credential_whitespace_is_stripped():
    # Whitespace around a pasted client id/secret is never part of the value,
    # but it is truthy — unstripped, a space-only credential would pass the
    # server's missing-settings check and fail only at the first token fetch.
    cfg = BPConfig(client_id=" svc-mcp ", client_secret=" s3cret\n")
    assert cfg.client_id == "svc-mcp"
    assert cfg.client_secret == "s3cret"
    assert BPConfig(client_id="   ").client_id == ""
    assert BPConfig(base_url=" https://bp.example/api/v7 ").base_url == "https://bp.example/api/v7"


def test_audit_log_path_whitespace_is_stripped():
    # Pasted whitespace must not change which file the audit lands in, and a
    # space-only value must read as unconfigured (build_audit_log fails loud).
    assert BPConfig(audit_log_path=" /var/log/audit.jsonl ").audit_log_path == (
        "/var/log/audit.jsonl"
    )
    assert BPConfig(audit_log_path="   ").audit_log_path == ""
    assert (
        BPConfig.from_env(env={"BP_AUDIT_LOG_PATH": " audit.jsonl "}).audit_log_path
        == "audit.jsonl"
    )


def test_client_secret_is_excluded_from_repr():
    # A host that logs or debug-prints its config must never echo the
    # credential; the field is still readable, just absent from repr().
    cfg = BPConfig(client_id="svc-mcp", client_secret="s3cret")
    assert "s3cret" not in repr(cfg)
    assert cfg.client_secret == "s3cret"


def test_config_is_frozen():
    cfg = BPConfig.from_env(env={})
    try:
        cfg.base_url = "mutated"  # type: ignore[misc]
    except Exception as exc:  # FrozenInstanceError
        assert "cannot assign" in str(exc).lower() or "frozen" in type(exc).__name__.lower()
    else:
        raise AssertionError("BPConfig should be immutable")
