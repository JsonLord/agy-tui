"""Actual owned synthetic process trees; never install or invoke OpenCode 2.x."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.hybrid_local.policy import HybridError
from app.hybrid_local.service import HybridLocal

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX process tree fixture; native Windows Job Object validation pending",
)


@pytest.fixture
def owned(tmp_path):
    root = tmp_path / "Projet équipe 日本語"
    root.mkdir()
    (root / "opencode.json").write_text("unchanged native config sentinel")
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "workspaces": {
                    "a": {
                        "root": str(root),
                        "capabilities": ["read", "edit"],
                        "write_paths": ["src"],
                    }
                }
            }
        )
    )
    binary = tmp_path / "fixture-opencode"
    pid_file = tmp_path / "owned-child.pid"
    binary.write_text(f"""#!{sys.executable}
import json,sys,subprocess,os
from pathlib import Path
if '--version' in sys.argv:
    print('1.18.35'); sys.exit(0)
if '--help' in sys.argv:
    print('acp --cwd --pure --hostname --port'); sys.exit(0)
child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(90)'])
Path({str(pid_file)!r}).write_text(str(child.pid))
options = [{{'id':'model','category':'model','currentValue':'synthetic/native'}},{{'id':'mode','category':'mode','currentValue':'hybrid-local-engineer'}}]
for line in sys.stdin:
    message=json.loads(line); method=message.get('method')
    if method=='initialize': result={{'agentCapabilities':{{'loadSession':True}}}}
    elif method=='session/new': result={{'sessionId':'owned-fixture','configOptions':options}}
    elif method=='session/set_config_option': result={{'configOptions':options}}
    elif method=='session/prompt':
        if os.environ.get('HYBRID_FIXTURE_CRASH')=='1': sys.exit(9)
        continue
    elif method=='session/cancel': continue
    else: result={{}}
    if 'id' in message: print(json.dumps({{'jsonrpc':'2.0','id':message['id'],'result':result}}),flush=True)
""")
    binary.chmod(0o700)
    service = HybridLocal(registry, tmp_path / "state", str(binary))
    return service, pid_file, root


def stopped(pid):
    path = Path(f"/proc/{pid}/stat")
    if path.exists():
        return path.read_text().split()[2] == "Z"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_timeout_kills_owned_tree_preserves_unrelated_and_blocks_duplicate(owned):
    service, pid_file, root = owned
    before = (root / "opencode.json").read_bytes()
    unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(90)"])
    try:
        result = asyncio.run(service.run("a", "Controlled fixture timeout", timeout=1))
        assert result["status"] == "REMOTE_STOP_UNCONFIRMED"
        assert result["recovery_required"]
        assert stopped(int(pid_file.read_text()))
        assert unrelated.poll() is None
        with pytest.raises(HybridError, match="WORKSPACE_BUSY_OR_RECOVERY_REQUIRED"):
            asyncio.run(service.run("a", "Do not dispatch a duplicate", timeout=1))
        assert (root / "opencode.json").read_bytes() == before
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_cancellation_kills_only_owned_tree_and_retains_uncertain_evidence(owned):
    service, pid_file, _ = owned

    async def run():
        task = asyncio.create_task(
            service.run("a", "Controlled fixture cancellation", timeout=30)
        )
        for _ in range(200):
            if pid_file.exists() and service.store.latest().get("status") == "RUNNING":
                break
            await asyncio.sleep(0.01)
        assert pid_file.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service.store.latest()["status"] == "REMOTE_STOP_UNCONFIRMED"
        assert stopped(int(pid_file.read_text()))

    asyncio.run(run())


def test_worker_crash_closes_owned_children_and_keeps_workspace_blocked(
    owned, monkeypatch
):
    service, pid_file, root = owned
    monkeypatch.setenv("HYBRID_FIXTURE_CRASH", "1")
    before = (root / "opencode.json").read_bytes()
    result = asyncio.run(service.run("a", "Controlled crash fixture", timeout=10))
    assert result["status"] == "REMOTE_STOP_UNCONFIRMED"
    assert result["error"] == "ACP_DISCONNECTED"
    assert stopped(int(pid_file.read_text()))
    assert (root / "opencode.json").read_bytes() == before
