#!/usr/bin/env bash
# Isolated Linux x64 research tools only; never configure work-PC providers.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
    echo 'Cloud research setup requires Linux x86_64; use the Windows install guide on the work PC.' >&2
    exit 2
fi
mkdir -p /workspace/agy-cloud/pip-cache /workspace/agy-cloud/npm-cache
export PIP_CACHE_DIR=/workspace/agy-cloud/pip-cache
python3 -m venv .venv-hybrid
.venv-hybrid/bin/python -m pip install --disable-pip-version-check --require-hashes --only-binary=:all: -r requirements-windows.lock
.venv-hybrid/bin/python -m pip install --disable-pip-version-check pytest==9.1.1 ruff==0.17.0
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt pytest==9.1.1
npm install --prefix /workspace/agy-cloud/opencode-inspection --cache /workspace/agy-cloud/npm-cache --no-fund --no-audit --ignore-scripts opencode-linux-x64@1.18.35
mkdir -p /workspace/agy-cloud/opencode-inspection/xdg/{data,config,cache,state} /workspace/agy-cloud/pwsh
curl -fsSL https://github.com/PowerShell/PowerShell/releases/download/v7.5.3/powershell-7.5.3-linux-x64.tar.gz -o /workspace/agy-cloud/pwsh.tar.gz
printf '%s\n' '5c74e0bbafd8be59e72267f05700fd6615344d9cf4d30460d9b6d12cd4a88a8c  /workspace/agy-cloud/pwsh.tar.gz' | sha256sum --check -
tar -xzf /workspace/agy-cloud/pwsh.tar.gz -C /workspace/agy-cloud/pwsh
chmod +x /workspace/agy-cloud/pwsh/pwsh
.venv-hybrid/bin/python -c 'import mcp,pydantic; print("hybrid-local dependencies ready")'
