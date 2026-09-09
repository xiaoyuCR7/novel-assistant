param([int]$Port = 8010)
$ErrorActionPreference = 'Stop'
$studioRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$backendPath = Join-Path $studioRoot 'backend'
$runtimePath = Join-Path $backendPath '.venv\Scripts\python.exe'
$staticPath = Join-Path $backendPath 'static'
if (-not (Test-Path -LiteralPath $runtimePath)) { throw 'Run uv sync --extra dev in backend first.' }
if (-not (Test-Path -LiteralPath (Join-Path $staticPath 'index.html'))) { throw 'Run scripts\build.ps1 first.' }
if (-not $env:NOVEL_DATA_DIR) { $env:NOVEL_DATA_DIR = Join-Path $backendPath 'data' }
$env:NOVEL_STATIC_DIR = $staticPath
$env:PYTHONPATH = Join-Path $backendPath 'src'
Write-Host "Local novel studio: http://127.0.0.1:$Port"
Push-Location $backendPath
try { & $runtimePath -m uvicorn novel_harness.main:app --host 127.0.0.1 --port $Port }
finally { Pop-Location }
