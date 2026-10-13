#Requires -Version 5.1
# No elevation, global settings or execution-policy changes.
[CmdletBinding()]
param([string]$InstallRoot, [string]$PythonPath)
$ErrorActionPreference = 'Stop'
if (-not $InstallRoot) {
    if (-not $env:LOCALAPPDATA) { throw 'Supply -InstallRoot: LOCALAPPDATA unavailable.' }
    $InstallRoot = Join-Path $env:LOCALAPPDATA 'AGYArgusHybrid'
}
if (-not $PythonPath) {
    $candidate = Get-Command python.exe, python3, python -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($candidate) { $PythonPath = $candidate.Source }
    else {
        $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
        if (-not $launcher) { throw 'Install Python 3.11-3.13 for your user and supply -PythonPath.' }
        $PythonPath = & $launcher.Source -3 -c 'import sys; print(sys.executable)'
        if ($LASTEXITCODE -ne 0) { throw 'Python launcher failed; supply -PythonPath.' }
    }
}
$backend = Join-Path $PSScriptRoot 'windows-readiness.py'
$arguments = @($backend, 'doctor', '--root', $InstallRoot)
& $PythonPath @arguments
if ($LASTEXITCODE -ne 0) { throw "Operation failed (exit $LASTEXITCODE). Review the diagnostic and installation receipt before retrying." }
