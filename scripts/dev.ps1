$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$backendDir = Join-Path $repoRoot "backend"
$frontendDir = Join-Path $repoRoot "frontend"
$frontendPort = if ($env:NOVEL_FRONTEND_PORT) { [int]$env:NOVEL_FRONTEND_PORT } else { 5173 }

if (-not (Test-Path (Join-Path $backendDir ".venv\Scripts\python.exe"))) {
    throw "Backend environment missing. Run 'uv sync --extra dev' in backend first."
}
if (-not (Test-Path (Join-Path $frontendDir "node_modules"))) {
    throw "Frontend dependencies missing. Run 'npm ci' in frontend first."
}

$backendJob = Start-Job -Name "novel-backend" -ScriptBlock {
    Set-Location $using:backendDir
    $env:PYTHONPATH = "src"
    & ".\.venv\Scripts\python.exe" -m uvicorn novel_harness.main:app --reload --host 127.0.0.1 --port 8000
}
$frontendJob = Start-Job -Name "novel-frontend" -ScriptBlock {
    Set-Location $using:frontendDir
    npm.cmd run dev -- --host 127.0.0.1 --port $using:frontendPort --strictPort
}

Write-Host "Novel Director Studio: http://127.0.0.1:$frontendPort"
Write-Host "Press Ctrl+C to stop both services."
try {
    while ($backendJob.State -eq "Running" -and $frontendJob.State -eq "Running") {
        # Native stderr includes normal Uvicorn logs; do not stop both services for it.
        Receive-Job $backendJob -ErrorAction Continue
        Receive-Job $frontendJob -ErrorAction Continue
        Start-Sleep -Seconds 1
    }
    Receive-Job $backendJob, $frontendJob -ErrorAction Continue
    throw "A development service stopped. Check the messages above."
}
finally {
    Stop-Job $backendJob, $frontendJob -ErrorAction SilentlyContinue
    Remove-Job $backendJob, $frontendJob -Force -ErrorAction SilentlyContinue
}
