from app.core.redact import redact, register_secrets


def test_scrubs_registered_secret_wherever_it_appears():
    register_secrets(["super-secret-token-value"])
    assert "super-secret-token-value" not in redact(
        "HTTPError: header Authorization: Bearer super-secret-token-value"
    )


def test_scrubs_bearer_and_authorization_patterns():
    out = redact("request failed with Authorization: Bearer abc123def456 at /v1/messages")
    assert "abc123def456" not in out
    assert "***REDACTED***" in out


def test_scrubs_dsn_password():
    out = redact("connection failed: host=127.0.0.1 password=hunter2 dbname=mem")
    assert "hunter2" not in out


def test_scrubs_url_credentials():
    out = redact("GET postgres://postgres:s3cr3t@127.0.0.1:5432/mem failed")
    assert "s3cr3t" not in out


def test_leaves_ordinary_text_alone():
    text = "job 12 failed: unique violation on (source_msg, content_hash)"
    assert redact(text) == text


def test_none_and_empty_pass_through():
    assert redact(None) is None
    assert redact("") == ""
