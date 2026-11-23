# Windows entry point (ASCII only: PowerShell 5.1 reads .ps1 as ANSI without a BOM)
#
#   powershell -ExecutionPolicy Bypass -File .\make.ps1 test
param(
    [Parameter(Position = 0)]
    [ValidateSet('help', 'test', 'test-center', 'test-node', 'accept', 'smoke', 'local', 'stop', 'dev', 'down', 'logs', 'ps')]
    [string]$Task = 'test'
)

$ErrorActionPreference = 'Stop'
$py = if ($env:SOLIRAG_PY) { $env:SOLIRAG_PY } else { 'python' }

function Invoke-Pytest([string]$dir) {
    Push-Location (Join-Path $PSScriptRoot $dir)
    try { & $py -m pytest -q } finally { Pop-Location }
}

function Invoke-Script([string]$rel, [string[]]$extra = @()) {
    Push-Location $PSScriptRoot
    try { & $py $rel @extra } finally { Pop-Location }
}

switch ($Task) {
    'help' {
        Write-Host 'make.ps1 test | test-center | test-node | accept | smoke | local | stop | dev | down | logs | ps'
        Write-Host '  test       run all unit tests (center + node)'
        Write-Host '  accept     in-process end-to-end (no containers)'
        Write-Host '  local      start 4 real processes (fake dify + 2 nodes + center)'
        Write-Host '  smoke      run scripts/smoke_real.py against those processes'
        Write-Host '  stop       stop the local processes'
        Write-Host '  dev        docker compose dev stack (1 center + 2 nodes + fake dify)'
    }
    'test-center' { Invoke-Pytest 'center' }
    'test-node' { Invoke-Pytest 'node' }
    'test' {
        Invoke-Pytest 'center'
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        Invoke-Pytest 'node'
    }
    'accept' { Invoke-Script 'scripts\accept.py' }
    'smoke' { Invoke-Script 'scripts\smoke_real.py' }
    'local' { Invoke-Script 'scripts\run_local.py' }
    'stop' { Invoke-Script 'scripts\run_local.py' -extra @('--stop') }
    'dev' { docker compose -f (Join-Path $PSScriptRoot 'docker-compose.dev.yml') up -d --build }
    'down' { docker compose -f (Join-Path $PSScriptRoot 'docker-compose.dev.yml') down }
    'logs' { docker compose -f (Join-Path $PSScriptRoot 'docker-compose.dev.yml') logs -f --tail=100 }
    'ps' { docker compose -f (Join-Path $PSScriptRoot 'docker-compose.dev.yml') ps }
}

exit $LASTEXITCODE
