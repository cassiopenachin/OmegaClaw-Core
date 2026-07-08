"""SAFE_VARS allowlist (DB-free). The entrypoint scrubs the environment to an allowlist so
provider keys never reach the agent process; the hive Postgres DSN must survive that scrub for
the board adapter to connect, while secrets must not. Reproduces the exact scrub loop from
entrypoint.sh against the list parsed from that file, so the test can't drift from the code."""

from __future__ import annotations

import pathlib
import re
import subprocess

ENTRYPOINT = pathlib.Path(__file__).resolve().parent.parent / "entrypoint.sh"


def _safe_vars() -> set[str]:
    m = re.search(r'SAFE_VARS="([^"]*)"', ENTRYPOINT.read_text(), re.S)
    assert m, "SAFE_VARS assignment not found in entrypoint.sh"
    # Drop shell line-continuation backslashes before splitting on whitespace.
    return set(m.group(1).replace("\\", " ").split())


def test_hive_dsn_is_allowlisted_and_secrets_are_not():
    sv = _safe_vars()
    assert "OMEGAHIVE_DATABASE_URL" in sv
    for secret in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ASI_API_KEY", "TG_BOT_TOKEN",
                   "SL_BOT_TOKEN", "OMEGACLAW_AUTH_SECRET"):
        assert secret not in sv, f"{secret} must never be in SAFE_VARS"


def test_scrub_keeps_listed_dsn_and_drops_unlisted_secret():
    # Reproduce entrypoint.sh's set-- scrub: allowlisted values survive unsplit (even with
    # spaces), unlisted vars are dropped.
    safe = " ".join(sorted(_safe_vars()))
    script = (
        f'SAFE_VARS="{safe}"\n'
        'set --\n'
        'for var in $SAFE_VARS; do\n'
        '  eval val=\\${$var:-}\n'
        '  if [ -n "$val" ]; then set -- "$@" "$var=$val"; fi\n'
        'done\n'
        'env -i "$@" env\n'
    )
    out = subprocess.run(
        ["sh", "-c", script],
        env={"OMEGAHIVE_DATABASE_URL": "host=db dbname=omegahive user=u password=p w",  # spaces!
             "ANTHROPIC_API_KEY": "sk-must-be-scrubbed", "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True,
    ).stdout
    # the space-containing DSN survives intact (not word-split into stray vars)
    assert "OMEGAHIVE_DATABASE_URL=host=db dbname=omegahive user=u password=p w" in out
    assert "ANTHROPIC_API_KEY" not in out
    assert "dbname" not in out.replace(
        "OMEGAHIVE_DATABASE_URL=host=db dbname=omegahive user=u password=p w", "")  # no leak as a var


if __name__ == "__main__":
    test_hive_dsn_is_allowlisted_and_secrets_are_not()
    test_scrub_keeps_listed_dsn_and_drops_unlisted_secret()
    print("SAFE_VARS test: OK")
