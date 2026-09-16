<#
.SYNOPSIS
    Build and test arr-proxy.

.DESCRIPTION
    unit   - pure logic, no containers beyond the image itself
    mock   - proxy + four scripted *arr instances, plus failure-mode checks
    real   - proxy + four genuine linuxserver Sonarr/Radarr containers
    all    - all of the above (default)

.EXAMPLE
    .\run-tests.ps1
    .\run-tests.ps1 -Suite mock
    .\run-tests.ps1 -Suite real -Keep
#>
[CmdletBinding()]
param(
    [ValidateSet('unit', 'mock', 'real', 'all')]
    [string]$Suite = 'all',

    # Leave the containers running afterwards so you can poke at them.
    [switch]$Keep
)

# NOT 'Stop': Windows PowerShell 5.1 turns a native command's stderr into
# terminating errors, and docker writes its build progress there.  Success is
# judged by $LASTEXITCODE instead, which is what these tools actually set.
$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot
$e2e = Join-Path $root 'tests\e2e'
$failures = @()

function Write-Phase($text) {
    Write-Host ''
    Write-Host ('=' * 70) -ForegroundColor Cyan
    Write-Host "  $text" -ForegroundColor Cyan
    Write-Host ('=' * 70) -ForegroundColor Cyan
}

function Invoke-Step($name, [scriptblock]$body) {
    & $body
    if ($LASTEXITCODE -ne 0) {
        $script:failures += $name
        Write-Host "FAILED: $name" -ForegroundColor Red
    }
}

Write-Phase 'Building images'
docker build -t arrproxy:e2e $root
if ($LASTEXITCODE -ne 0) { Write-Host 'image build failed' -ForegroundColor Red; exit 1 }
docker build -t arrproxy-tester:e2e -f (Join-Path $e2e 'Dockerfile.tester') $e2e
if ($LASTEXITCODE -ne 0) { Write-Host 'tester image build failed' -ForegroundColor Red; exit 1 }

if ($Suite -in 'unit', 'all') {
    Write-Phase 'Unit tests'
    Invoke-Step 'unit' {
        $env:MSYS_NO_PATHCONV = '1'
        docker run --rm -v "${root}\tests\unit:/tests/unit:ro" `
            -e PYTHONPATH=/app -w /app --entrypoint python arrproxy-tester:e2e `
            -m pytest /tests/unit -q -p no:cacheprovider
    }
}

if ($Suite -in 'mock', 'all') {
    Write-Phase 'End-to-end against scripted instances'
    Push-Location $e2e
    try {
        python make_seeds.py | Out-Null
        docker compose -f docker-compose.e2e.yml down -v --remove-orphans 2>&1 | Out-Null
        docker compose -f docker-compose.e2e.yml up -d --wait
        if ($LASTEXITCODE -ne 0) { Write-Host 'mock stack failed to start' -ForegroundColor Red; exit 1 }

        Invoke-Step 'e2e-mock' {
            docker compose -f docker-compose.e2e.yml --profile test run --rm tester
        }
        Write-Phase 'Failure modes (instances stopped and restarted)'
        Invoke-Step 'e2e-degraded' { python check_degraded.py }
    }
    finally {
        if (-not $Keep) {
            docker compose -f docker-compose.e2e.yml down -v --remove-orphans 2>&1 | Out-Null
        }
        Pop-Location
    }
}

if ($Suite -in 'real', 'all') {
    Write-Phase 'End-to-end against real Sonarr and Radarr'
    Push-Location $e2e
    try {
        # Always start from empty volumes: the assertions depend on each instance
        # numbering its titles from 1, which leftover state silently breaks.
        docker compose -f docker-compose.real.yml --profile proxy --profile test `
            down -v --remove-orphans 2>&1 | Out-Null
        docker compose -f docker-compose.real.yml up -d `
            real-sonarr-main real-sonarr-anime real-radarr-main real-radarr-anime --wait
        if ($LASTEXITCODE -ne 0) { Write-Host 'real instances failed to start' -ForegroundColor Red; exit 1 }

        # Root folders have to exist and be writable by the container user
        # before the *arrs will accept them.
        docker exec -u 0 real-sonarr-main sh -c `
            'mkdir -p /data/tv /data/anime /data/movies /data/anime-movies && chown -R 1000:1000 /data'

        python setup_real.py
        if ($LASTEXITCODE -ne 0) { Write-Host 'real instance setup failed' -ForegroundColor Red; exit 1 }

        docker compose -f docker-compose.real.yml --profile proxy up -d proxy --wait
        Invoke-Step 'e2e-real' {
            docker compose -f docker-compose.real.yml --profile test run --rm tester
        }
    }
    finally {
        if (-not $Keep) {
            docker compose -f docker-compose.real.yml --profile proxy --profile test `
                down -v --remove-orphans 2>&1 | Out-Null
        }
        Pop-Location
    }
}

Write-Phase 'Summary'
if ($failures.Count -eq 0) {
    Write-Host 'All suites passed.' -ForegroundColor Green
    exit 0
}
Write-Host ("Failed: " + ($failures -join ', ')) -ForegroundColor Red
exit 1
