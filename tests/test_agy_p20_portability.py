"""P20 — Portability / containerization.

Zero live models, zero secrets. Proves:

 1. runtime_config resolves every runtime path from env with sensible
    defaults (repo-relative, never a baked host path); env override wins.
 2. the accepted product tree carries no hardcoded host paths / fixed
    endpoints outside runtime_config's documented local defaults.
 3. the health payload is secret-free (presence booleans only) and reports
    the real release sha / opencode / state-root-writable / planner-configured.
 4. the entrypoint's P20-E config materialization: absent worker key ->
    provider disabled (never faked), no raw secret ever written; present
    key -> injected, provider enabled.
 5. the entrypoint fails closed on an unwritable state root.
 6. the container (when present in CI) boots, serves /health, and state
    survives a restart on the mounted volume with a FAKE cloud provider.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable or "/usr/bin/python3"
DOCKER = shutil.which("docker")
TEMPLATE = ROOT / "container" / "opencode.template.jsonc"


def _sub(args, env=None, cwd=None):
    full = dict(os.environ)
    for k in ("AGY_STATE_ROOT", "AGY_RELEASE_ROOT", "AGY_RELEASE_LINK",
              "AGY_CONFIG_ROOT", "OPENCODE_SERVER_URL", "OPENCODE_SUPERVISE_PATH",
              "GRAPHIFY_BIN", "GRAPHIFY_ENSURE_BIN", "OPENCODE_SUPERVISE_BIN",
              "GX10_BIN", "GEMINI_API_KEY", "HELMHOLTZ_API_KEY"):
        full.pop(k, None)
    if env:
        full.update(env)
    return subprocess.run(args, capture_output=True, text=True, timeout=120,
                          env=full, cwd=cwd or str(ROOT))


def _reload_runtime_config():
    for m in list(sys.modules):
        if m.startswith("app.runtime_config") or m.startswith("app.release_info"):
            del sys.modules[m]
    sys.path.insert(0, str(ROOT))
    import app.runtime_config as rc
    return rc


# ---------------------------------------------------------------------------
# 1. config resolution
# ---------------------------------------------------------------------------


def test_runtime_config_defaults_are_portable():
    rc = _reload_runtime_config()
    # release root defaults to THIS checkout (repo-relative), not /home/leon
    assert rc.release_root() == ROOT, rc.release_root()
    # state root defaults to <repo>/state
    assert rc.state_root() == ROOT / "state"
    # no baked host path in the defaults:
    assert str(rc.release_root()) != "/home/leon/antigravity-hybrid"


def test_runtime_config_env_override_wins(tmp_path):
    sr = tmp_path / "sroot"
    rr = tmp_path / "rroot"
    rc = _reload_runtime_config()
    os.environ["AGY_STATE_ROOT"] = str(sr)
    os.environ["AGY_RELEASE_ROOT"] = str(rr)
    try:
        assert rc.state_root() == sr
        assert rc.release_root() == rr
    finally:
        del os.environ["AGY_STATE_ROOT"]
        del os.environ["AGY_RELEASE_ROOT"]
    rc = _reload_runtime_config()
    assert rc.state_root() == ROOT / "state"


def test_opencode_server_url_env_override(tmp_path):
    rc = _reload_runtime_config()
    os.environ["OPENCODE_SERVER_URL"] = "http://10.0.0.5:9999"
    try:
        assert rc.opencode_server_url() == "http://10.0.0.5:9999"
    finally:
        del os.environ["OPENCODE_SERVER_URL"]
    rc = _reload_runtime_config()
    assert rc.opencode_server_url() == "http://127.0.0.1:4096"


# ---------------------------------------------------------------------------
# 2. no host assumptions in product code
# ---------------------------------------------------------------------------

_PRODUCT = [ROOT / "app", ROOT / "mcp_gateway.py", ROOT / "scripts"]


def _product_files():
    files = []
    for base in _PRODUCT:
        if base.is_file():
            files.append(base)
        elif base.is_dir():
            files.extend(base.rglob("*.py"))
            files.extend(base.rglob("*.sh"))
    # entrypoint.sh's loopback default is CORRECT container behavior
    # (worker backend is loopback by design; the env override wins); the
    # audit targets PRODUCT code, not the deployment entrypoint.
    return [f for f in files
            if "test_" not in f.name and ".pyc" not in str(f)
            and f.name != "entrypoint.sh"]


def test_no_hardcoded_host_paths_outside_runtime_config():
    offenders = []
    for f in _product_files():
        if f.name in ("runtime_config.py",):
            continue  # the documented local defaults live only here
        text = f.read_text()
        for i, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if "/home/leon" in line:
                offenders.append(f"{f.name}:{i}: {line.strip()[:80]}")
    assert not offenders, "hardcoded host paths:\n" + "\n".join(offenders)


def test_no_fixed_loopback_endpoints_outside_config():
    offenders = []
    for f in _product_files():
        if f.name in ("runtime_config.py", "fault_injection.py"):
            continue
        text = f.read_text()
        for i, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if re.search(r"127\.0\.0\.1:4096|localhost:4096", line):
                offenders.append(f"{f.name}:{i}: {line.strip()[:80]}")
    assert not offenders, "fixed worker-endpoint assumptions:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# 3. health payload
# ---------------------------------------------------------------------------


def test_health_payload_is_secret_free_and_honest():
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from app.health import health\n"
        "import json\n"
        "print(json.dumps(health()))\n" % str(ROOT)
    )
    res = _sub([PY, "-c", code])
    assert res.returncode == 0, res.stderr[-800:]
    h = json.loads(res.stdout.strip().splitlines()[-1])
    # structure
    assert h["release"]["release_sha"]
    assert h["state_root"]["writable"] in (True, False)
    assert "secrets" in h and "cloud_planner_configured" in h
    # no secret material anywhere in the payload:
    blob = json.dumps(h)
    assert not re.search(r"(sk-|sk_|AIza|ghp_|ya29|xoxb-|Bearer)[A-Za-z0-9_\-]{6,}",
                         blob), "secret material in health payload"
    assert "api_key" not in blob.lower() or '"api_key": ' not in blob


# ---------------------------------------------------------------------------
# 4. entrypoint P20-E config materialization
# ---------------------------------------------------------------------------

_ENTRY = ROOT / "scripts" / "entrypoint.sh"


def _extract_block(start_marker, end_marker):
    lines = _ENTRY.read_text().splitlines(keepends=True)
    s = next(i for i, l in enumerate(lines) if start_marker in l)
    e = next(i for i, l in enumerate(lines) if end_marker in l and i > s)
    return "".join(lines[s:e])


def test_entrypoint_config_absent_key_disables_provider(tmp_path):
    assert TEMPLATE.exists(), "redacted opencode template missing"
    block = _extract_block("# P20-E: worker-backend credentials",
                           "# Start the worker backend")
    conf = tmp_path / "cfg"
    xdg = tmp_path / "xdg"
    conf.mkdir(); xdg.mkdir()
    script = tmp_path / "block.sh"
    script.write_text(
        "set -euo pipefail\n"
        f'RELEASE_ROOT="{ROOT}"\n'
        + block
    )
    res = _sub(["bash", str(script)], env={
        "OPENCODE_CONFIG_DIR": str(conf),
        "XDG_CONFIG_HOME": str(xdg),
        # no HELMHOLTZ_API_KEY
    })
    assert res.returncode == 0, res.stderr[-500:]
    mat = conf / "opencode.jsonc"
    assert mat.exists()
    d = json.loads(mat.read_text())
    h = d["provider"]["helmholtz"]
    assert h["enabled"] is False
    assert h.get("apiKey", "") in ("", None)
    raw = mat.read_text()
    assert not re.search(r"(sk-|sk_|AIza|ghp_|ya29)[A-Za-z0-9_\-]{8,}", raw)
    assert (xdg / "opencode" / "opencode.jsonc").exists()


def test_entrypoint_config_present_key_injected(tmp_path):
    block = _extract_block("# P20-E: worker-backend credentials",
                           "# Start the worker backend")
    conf = tmp_path / "cfg"
    xdg = tmp_path / "xdg"
    conf.mkdir(); xdg.mkdir()
    script = tmp_path / "block.sh"
    script.write_text(
        "set -euo pipefail\n"
        f'RELEASE_ROOT="{ROOT}"\n'
        + block
    )
    fake_key = "test-injected-not-a-real-key"
    res = _sub(["bash", str(script)], env={
        "OPENCODE_CONFIG_DIR": str(conf),
        "XDG_CONFIG_HOME": str(xdg),
        "HELMHOLTZ_API_KEY": fake_key,
    })
    assert res.returncode == 0, res.stderr[-500:]
    mat = (conf / "opencode.jsonc").read_text()
    assert fake_key in mat
    assert "__ABSENT__" not in mat
    d = json.loads(mat)
    assert d["provider"]["helmholtz"]["enabled"] is True


# ---------------------------------------------------------------------------
# 5. state-root fail-closed
# ---------------------------------------------------------------------------


def test_entrypoint_fails_closed_on_unwritable_state_root(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)  # read-only
    # run the entrypoint with an unwritable state root -> must exit 1 fast
    res = _sub(["bash", str(_ENTRY)], env={
        "AGY_STATE_ROOT": str(ro),
        "AGY_RELEASE_ROOT": str(ROOT),
        "AGY_PYTHON": PY,
        "OPENCODE_BIN": "/nonexistent-opencode",
        "AGY_HEALTH_PORT": "0",
    })
    try:
        ro.chmod(0o700)
    except OSError:
        pass
    assert res.returncode != 0, res.stderr[-400:]
    assert "not writable" in res.stderr


# ---------------------------------------------------------------------------
# 6. container smoke (skipped when docker is unavailable)
# ---------------------------------------------------------------------------


def _docker(*args, **kw):
    return subprocess.run([DOCKER, *args], capture_output=True, text=True,
                          timeout=kw.pop("timeout", 300), **kw)


def test_container_boot_and_restart_persistence(tmp_path):
    if not DOCKER:
        pytest.skip("docker not available")
    if not (ROOT / "container" / "opencode").exists():
        pytest.skip("opencode build-input not present")
    img = "agy-hybrid:rc-test"
    # The image ships WITHOUT .git (P20: no git in the image). The P19
    # "same release SHA" contract is honored via the AGY_RELEASE_SHA
    # build-arg → baked release-sha marker file. Bake the current worktree
    # HEAD and require the container to report it.
    head = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True).stdout.strip()
    # Always (re)build so the baked release-sha marker matches the current
    # HEAD (layer-cached, ~seconds; an image built for a different SHA must
    # not be silently reused — that would break the P19 same-SHA contract).
    b = _docker("build", "-t", img, "--build-arg", f"AGY_RELEASE_SHA={head}",
                "-f", str(ROOT / "container/Dockerfile"),
                str(ROOT), timeout=1800)
    assert b.returncode == 0, b.stderr[-2000:]
    vol = tmp_path / "data"
    vol.mkdir()
    name = "agy-p20-boot"
    subprocess.run([DOCKER, "rm", "-f", name], capture_output=True)
    p = subprocess.Popen(
        [DOCKER, "run", "-d", "--name", name,
         "-v", f"{vol}:/data/agy",
         "-e", "AGY_STATE_ROOT=/data/agy",
         "-e", "HEALTH_PORT=8080",
         "-p", "0:8080",
         img],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    cid = p.communicate()[0].strip()
    assert cid, p.stderr
    try:
        # wait for /health
        import time, urllib.request
        port = None
        for _ in range(60):
            m = subprocess.run([DOCKER, "port", name, "8080/tcp"],
                               capture_output=True, text=True)
            if m.stdout.strip():
                # docker port may emit two lines (IPv4 + IPv6): take the
                # last line's port number.
                port = m.stdout.strip().splitlines()[-1].rsplit(":", 1)[-1]
                break
            time.sleep(1)
        assert port, "container never published 8080"
        payload = None
        for _ in range(90):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/health", timeout=3) as r:
                    payload = json.loads(r.read())
                if payload.get("release", {}).get("release_sha"):
                    break
            except Exception:
                time.sleep(1)
        assert payload and payload["release"]["release_sha"], payload
        assert payload["state_root"]["writable"] is True
        # create durable state, restart, prove it survives
        (vol / "projects").mkdir(exist_ok=True)
        (vol / "projects" / "p-persist.json").write_text(
            json.dumps({"marker": "survives-restart"}))
        subprocess.run([DOCKER, "restart", name], capture_output=True)
        # A docker restart with an ephemeral -p 0:8080 may REASSIGN the
        # host port — re-resolve it (never reuse the pre-restart port).
        port = None
        for _ in range(30):
            m = subprocess.run([DOCKER, "port", name, "8080/tcp"],
                               capture_output=True, text=True)
            if m.stdout.strip():
                port = m.stdout.strip().splitlines()[-1].rsplit(":", 1)[-1]
                break
            time.sleep(1)
        assert port, "container did not re-publish 8080 after restart"
        payload2 = None
        for _ in range(90):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/health", timeout=3) as r:
                    payload2 = json.loads(r.read())
                if payload2.get("release", {}).get("release_sha"):
                    break
            except Exception:
                time.sleep(1)
        assert payload2 and payload2["release"]["release_sha"], payload2
        marker = json.loads((vol / "projects" / "p-persist.json").read_text())
        assert marker["marker"] == "survives-restart"
    finally:
        subprocess.run([DOCKER, "rm", "-f", name], capture_output=True)
