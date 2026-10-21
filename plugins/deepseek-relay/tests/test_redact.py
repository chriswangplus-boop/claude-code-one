import json

from relay.redact import redact, redact_obj
from relay.util import sha256_text


def test_api_key_and_email_and_home():
    text = "key sk-abcdefghijklmnopqrstuvwx mail a.b@example.com home /Users/alice/proj"
    result = redact(text)
    assert "sk-abcdefghijklmnopqrstuvwx" not in result.text
    assert "a.b@example.com" not in result.text
    assert "/Users/alice" not in result.text
    assert result.counts["api_key"] == 1
    assert result.counts["email"] == 1
    assert result.counts["home_path"] == 1


def test_sha256_survives_so_pointers_stay_verifiable():
    digest = sha256_text("payload")
    text = f'RELAY round 12: read .relay/HANDOFF.md (sha256 {digest})'
    result = redact(text)
    assert digest in result.text
    assert not result.changed


def test_private_key_block():
    text = "before\n-----BEGIN OPENSSH PRIVATE KEY-----\nabc123\n-----END OPENSSH PRIVATE KEY-----\nafter"
    result = redact(text)
    assert "PRIVATE KEY" not in result.text
    assert result.text.startswith("before") and result.text.endswith("after")


def test_env_and_keyed_secrets():
    text = 'GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz\npassword = "hunter2hunter2"\nSAFE_VALUE=hello'
    result = redact(text)
    assert "ghp_" not in result.text
    assert "hunter2hunter2" not in result.text
    assert "SAFE_VALUE=hello" in result.text


def test_known_secret_exact_topic():
    topic = "relay-7f3c9a2b1d4e6f8a0c5b7d9e"
    text = f"https://ntfy.sh/{topic}"
    result = redact(text, extra_secrets=[topic])
    assert topic not in result.text
    assert "[secret]" in result.text


def test_redact_obj_sensitive_fields_and_nested():
    payload = {
        "text": "contact me at bob@example.com",
        "api_key": "anything at all",
        "nested": {"list": ["/Users/bob/x", "clean"]},
    }
    clean, counts = redact_obj(payload)
    assert clean["api_key"] == "[secret]"
    assert "bob@example.com" not in clean["text"]
    assert clean["nested"]["list"][0] == "/Users/<user>/x"
    assert clean["nested"]["list"][1] == "clean"
    assert counts["sensitive_field"] == 1


def test_sentinel_json_payload_is_not_mangled():
    payload = {
        "round": 12,
        "role": "reviewer",
        "file": ".relay/REVIEW.md",
        "sha256": sha256_text("review"),
        "summary": "Approved. See /Users/alice/proj/src/app.py",
    }
    clean, _ = redact_obj(payload)
    assert clean["sha256"] == payload["sha256"]
    assert clean["file"] == ".relay/REVIEW.md"
    assert "/Users/alice" not in clean["summary"]


def test_long_base64_is_redacted():
    blob = "QWxleGFuZHJpYVN1cGVyU2VjcmV0VG9rZW5WYWx1ZTEyMzQ1Ng=="
    result = redact(f"token {blob}")
    assert blob not in result.text
