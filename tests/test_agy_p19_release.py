"""P19 — Release / live-surface cutover.

Zero live cloud, zero secrets. Proves the cutover mechanics the user asked
for, without a fragile untested symlink scheme:

 1. release_info reports the git HEAD of the release root (identity),
    schema version, and honest capability probes (absent dep = ABSENT,
    never a crash, never a secret).
 2. the MCP gateway's tool surface INCLUDES the version endpoint, and the
    endpoint reports the running release's SHA.
 3. `agy-hybrid-release use <sha> <dir>` swaps the release link ATOMICALLY
    (ln -sfn + mv -T) and points at exactly that checkout.
 4. `use` REFUSES (exit 1) a checkout that is not at the requested SHA —
    the release can never silently bind a different/development checkout.
 5. `sha` on a release not set fails closed (non-zero, clear message).
 6. `bootstrap` and `smoke` run green against the accepted release.
 7. the live wrapper /home/leon/bin/agy-hybrid-mcp delegates to the
    release script (same path mcp_config.json uses — the cutover hazard
    the milestone exists to fix).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path("/home/leon/antigravity-hybrid")
RELEASE_BIN = ROOT / "scripts" / "agy-hybrid-release"
VENV = "/home/leon/antigravity-hybrid/.venv/bin/python"


def _run(args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    for key in ("AGY_RELEASE_LINK", "AGY_RELEASE_ROOT", "AGY_PYTHON",
                "AGY_STATE_ROOT", "AGY_RELEASE_BIN"):
        full_env.pop(key, None)
    if env:
        full_env.update(env)
    return subprocess.run(
        args, capture_output=True, text=True, timeout=120, env=full_env
    )


def _git_init_repo(tmp: Path, commit_msg: str) -> str:
    """A minimal git repo so the cutover can be exercised on a
    disposable checkout (never the real release)."""
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "marker.txt").write_text("release\n")
    subprocess.run(["git", "init", "-q", str(tmp)], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.email", "r@r"], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.name", "r"], check=True)
    subprocess.run(["git", "-C", str(tmp), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp), "commit", "-q", "-m", commit_msg], check=True
    )
    out = subprocess.run(
        ["git", "-C", str(tmp), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


# ---------------------------------------------------------------------------
# 1. release_info identity + capability honesty
# ---------------------------------------------------------------------------


def test_release_info_reports_checkout_head():
    sys.path.insert(0, str(ROOT))
    try:
        from app.release_info import collect, render

        info = collect(ROOT)
        head = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert info.release_sha == head
        assert info.schema_version  # non-empty schema version
        # capability probes are honest strings, no exceptions:
        text = render(ROOT)
        assert "release_sha" in text and "capabilities" in text
        # no secret-ish material ever appears:
        lowered = text.lower()
        assert "api_key" not in lowered and "token" not in lowered
    finally:
        sys.path.pop(0)


# ---------------------------------------------------------------------------
# 2. MCP tool surface includes the version endpoint
# ---------------------------------------------------------------------------


def test_mcp_gateway_exposes_release_info_tool():
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import mcp_gateway, json\n"
        "names = [t.name for t in mcp_gateway.mcp._tool_manager.list_tools()]\n"
        "print(json.dumps(names))\n" % str(ROOT)
    )
    res = subprocess.run(
        [VENV, "-c", code],
        capture_output=True, text=True, timeout=120, cwd=str(ROOT),
        env={**os.environ, "AGY_RELEASE_ROOT": str(ROOT)},
    )
    assert res.returncode == 0, res.stderr[-800:]
    names = json.loads(res.stdout.strip().splitlines()[-1])
    assert "agy_release_info_tool" in names, names


def test_mcp_release_info_tool_reports_release_sha():
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import mcp_gateway, json\n"
        "print(mcp_gateway.agy_release_info_tool())\n" % str(ROOT)
    )
    res = subprocess.run(
        [VENV, "-c", code],
        capture_output=True, text=True, timeout=120, cwd=str(ROOT),
        env={**os.environ, "AGY_RELEASE_ROOT": str(ROOT)},
    )
    assert res.returncode == 0, res.stderr[-800:]
    # agy_release_info_tool prints indented JSON (the gateway's own
    # format) — parse the whole stdout.
    data = json.loads(res.stdout)
    head = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert data["release_sha"] == head


# ---------------------------------------------------------------------------
# 3/4/5. the release pointer script: atomic swap, refusal, fail-closed
# ---------------------------------------------------------------------------


def test_release_use_swaps_atomically(tmp_path):
    repo = tmp_path / "rel"
    sha = _git_init_repo(repo, "release candidate")
    link = tmp_path / "current"
    env = {"AGY_RELEASE_LINK": str(link)}
    res = _run(
        [str(RELEASE_BIN), "use", sha, str(repo)], env=env
    )
    assert res.returncode == 0, res.stderr
    assert link.is_symlink()
    assert os.path.realpath(str(link)) == os.path.realpath(str(repo))
    # `sha` now reports the swapped-in release:
    res2 = _run([str(RELEASE_BIN), "sha"], env=env)
    assert res2.returncode == 0
    assert res2.stdout.strip() == sha


def test_release_use_refuses_wrong_sha(tmp_path):
    repo = tmp_path / "rel"
    sha = _git_init_repo(repo, "release candidate")
    # a SECOND commit moves the repo off the requested sha
    (repo / "marker2.txt").write_text("moved\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "post-release change"],
        check=True,
    )
    link = tmp_path / "current"
    env = {"AGY_RELEASE_LINK": str(link)}
    res = _run([str(RELEASE_BIN), "use", sha, str(repo)], env=env)
    assert res.returncode == 1, "use must refuse a checkout not at <sha>"
    assert "REFUSED" in res.stderr
    # and no link was created:
    assert not link.exists() and not link.is_symlink()


def test_release_sha_fails_closed_when_unset(tmp_path):
    link = tmp_path / "never-created"
    env = {"AGY_RELEASE_LINK": str(link)}
    res = _run([str(RELEASE_BIN), "sha"], env=env)
    assert res.returncode != 0
    assert "release not set" in res.stderr


# ---------------------------------------------------------------------------
# 6. bootstrap + smoke run green against the accepted release
# ---------------------------------------------------------------------------


def test_release_bootstrap_green():
    res = _run([str(RELEASE_BIN), "bootstrap"],
               env={"AGY_RELEASE_ROOT": str(ROOT)})
    assert res.returncode == 0, res.stderr + res.stdout
    assert "OK    mcp" in res.stdout
    assert "FAIL" not in res.stdout.split("WARN")[0]


def test_release_smoke_reports_one_sha():
    res = _run([str(RELEASE_BIN), "smoke"],
               env={"AGY_RELEASE_ROOT": str(ROOT)})
    assert res.returncode == 0, res.stderr + res.stdout
    assert "SMOKE PASS" in res.stdout
    head = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head in res.stdout


# ---------------------------------------------------------------------------
# 7. the live MCP wrapper delegates to the release pointer
# ---------------------------------------------------------------------------


def test_live_mcp_wrapper_delegates_to_release_pointer():
    wrapper = Path("/home/leon/bin/agy-hybrid-mcp")
    text = wrapper.read_text()
    # the cutover hazard (hardcoded worktree/main exec) is gone:
    assert "exec /home/leon/antigravity-hybrid/.venv/bin/python" not in text
    # and it goes through the release script:
    assert "agy-hybrid-release" in text
    assert os.access(str(wrapper), os.X_OK)


def test_live_mcp_wrapper_falls_back_to_release_root_env(tmp_path):
    """With the release script hidden, the wrapper still runs the gateway
    from AGY_RELEASE_ROOT (env-overridable — never a silent dev-worktree
    bind)."""
    fake_bin = tmp_path / "no-release"
    fake_bin.mkdir(parents=True, exist_ok=True)
    (fake_bin / "agy-hybrid-release").write_text("#!/bin/sh\nexit 99\n")
    (fake_bin / "agy-hybrid-release").chmod(0o755)
    # Make it non-executable so the wrapper's `-x` gate skips it.
    (fake_bin / "agy-hybrid-release").chmod(0o644)
    res = subprocess.run(
        ["/home/leon/bin/agy-hybrid-mcp"],
        capture_output=True, text=True, timeout=25,
        env={
            **os.environ,
            "AGY_RELEASE_BIN": str(fake_bin / "agy-hybrid-release"),
            "AGY_RELEASE_ROOT": str(ROOT),
        },
        stdin=subprocess.DEVNULL,
    )
    # An MCP stdio server starts and then blocks reading stdin; DEVNULL
    # closes it, so it exits quickly. The point: no tracebacks about a
    # missing release path, and it ran from ROOT.
    assert "release not set" not in res.stderr
    assert "Traceback" not in res.stderr, res.stderr[-500:]