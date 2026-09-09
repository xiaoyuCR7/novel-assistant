$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

Push-Location (Join-Path $repoRoot "backend")
try {
    $env:PYTHONPATH = "src"
    & ".\.venv\Scripts\python.exe" -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Backend tests failed." }
    & ".\.venv\Scripts\python.exe" -m ruff check .
    if ($LASTEXITCODE -ne 0) { throw "Backend lint failed." }
}
finally { Pop-Location }

& (Join-Path $repoRoot "scripts\tests\build-safety.test.ps1")
if ($LASTEXITCODE -ne 0) { throw "Build deletion safety tests failed." }

& (Join-Path $repoRoot "scripts\build.ps1")
if ($LASTEXITCODE -ne 0) { throw "Application build and static sync failed." }

Push-Location (Join-Path $repoRoot "frontend")
try {
    if (-not (Test-Path "node_modules")) { npm ci }
    npm test -- --run
    if ($LASTEXITCODE -ne 0) { throw "Frontend tests failed." }
}
finally { Pop-Location }

Push-Location (Join-Path $repoRoot "e2e")
try {
    if (-not (Test-Path "node_modules")) { npm ci }
    npm test
    if ($LASTEXITCODE -ne 0) { throw "End-to-end tests failed." }
}
finally { Pop-Location }
