import pytest

from hypnos.scrub import contains_secret, scrub, scrub_text

SECRETS = [
    ("aws_key", "export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"),
    ("anthropic_key", "key is sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345"),
    ("openai_key", "OPENAI=sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"),
    ("github_token", "git clone https://ghp_abcdefghijklmnopqrstuvwxyz0123456789@github.com/a/b"),
    ("github_token", "token github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz"),
    ("gitlab_token", "glpat-abcdefghijklmnopqrst"),
    ("slack_token", "xoxb-1234567890-abcdefghij"),
    ("google_key", "AIzaSyA1234567890abcdefghijklmnopqrstuv"),
    ("stripe_key", "sk_live_abcdefghijklmnop1234"),
    ("hf_token", "HF_TOKEN=hf_abcdefghijklmnopqrstuvwxyzABCDEF12"),
    ("npm_token", "//registry.npmjs.org/:_authToken=npm_abcdefghijklmnopqrstuvwxyz0123456789"),
    ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"),
    ("bearer", "curl -H 'Authorization: Bearer abc123def456ghi789' https://api.example.com"),
    ("url_credentials", "postgres://admin:hunter2secret@db.internal:5432/app"),
    ("private_key", "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"),
    ("runpod_key", "RUNPOD_API_KEY=rpa_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"),
]


@pytest.mark.parametrize("kind,text", SECRETS)
def test_secret_shapes_are_redacted(kind, text):
    out = scrub_text(text)
    assert "[REDACTED:" in out
    assert contains_secret(text)
    assert not contains_secret(out)


def test_specific_kind_is_used():
    assert "[REDACTED:aws_key]" in scrub_text("AKIAIOSFODNN7EXAMPLE")
    assert "[REDACTED:anthropic_key]" in scrub_text("sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345")


def test_assigned_secrets_keep_the_key_name():
    assert scrub_text("API_KEY=abcdef123456") == "API_KEY=[REDACTED:assigned_secret]"
    assert scrub_text('password: "hunter22"') == 'password: "[REDACTED:assigned_secret]"'
    assert scrub_text('{"client_secret": "zzzzyyyyxxxx"}') == '{"client_secret": "[REDACTED:assigned_secret]"}'
    assert scrub_text("mysql -u root --password s3cretpw") == "mysql -u root --password [REDACTED:cli_secret_flag]"


def test_env_references_are_not_secrets():
    assert scrub_text("API_KEY=$API_KEY") == "API_KEY=$API_KEY"
    assert scrub_text("token=<your-token>") == "token=<your-token>"


def test_personal_data():
    assert scrub_text("mail me at jane.doe+x@example.co.id") == "mail me at [REDACTED:email]"
    assert scrub_text("ssh deploy@10.0.3.7") == "ssh deploy@[REDACTED:ipv4]"
    assert "[REDACTED:ipv6]" in scrub_text("ping 2001:db8:85a3:0:0:8a2e:370:7334")


@pytest.mark.parametrize("text", [
    "pnpm add lodash",
    "npm install --save-dev vitest@1.6.0",
    "git commit -m 'feat(api): add retry to fetchUser'",
    "git push origin feature/login-form",
    "git remote add origin git@github.com:owner/repo.git",
    "curl http://127.0.0.1:8765/v1/models",
    "python -m pytest tests/test_api.py -k retry -q",
    "rm -rf ./build/cache",
    "max_tokens=512 num_tokens: 2048",
    "cd src/components/HeaderComponentWithLongName2024.tsx",
    "pwd = os.getcwd()",
    "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    "docker run --rm -p 0.0.0.0:3000:3000 app",
])
def test_ordinary_commands_survive(text):
    assert scrub_text(text) == text


def test_git_sha_is_redacted_but_short_sha_kept():
    assert scrub_text("git checkout 3f2c1a9") == "git checkout 3f2c1a9"
    assert "[REDACTED:hex_secret]" in scrub_text("git checkout 3f2c1a9b8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b")


def test_high_entropy_tokens():
    assert "[REDACTED:high_entropy]" in scrub_text("x Zk9Lm2Qp7Rt4Vw8Xy3Ab6Cd1Ef5Gh0Ij2Kl")
    assert scrub_text("this_is_a_very_long_snake_case_identifier_name") == (
        "this_is_a_very_long_snake_case_identifier_name")


def test_idempotent():
    for _, text in SECRETS:
        once = scrub_text(text)
        assert scrub_text(once) == once


def test_structures_and_secret_fields():
    data = {
        "command": "export TOKEN=abcdef123456 && pnpm test",
        "env": {"GITHUB_TOKEN": "plainvalue", "PATH": "/usr/bin"},
        "list": ["AKIAIOSFODNN7EXAMPLE", 3, None],
    }
    out = scrub(data)
    assert out["command"] == "export TOKEN=[REDACTED:assigned_secret] && pnpm test"
    assert out["env"]["GITHUB_TOKEN"] == "[REDACTED:secret_field]"
    assert out["env"]["PATH"] == "/usr/bin"
    assert out["list"] == ["[REDACTED:aws_key]", 3, None]


def test_known_literals():
    assert scrub_text("value is zz9-plural-z-alpha", extra_literals=("zz9-plural-z-alpha",)) == (
        "value is [REDACTED:known_secret]")


def test_empty_and_non_string():
    assert scrub_text("") == ""
    assert scrub(None) is None
    assert scrub(42) == 42
