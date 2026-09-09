$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$frontendDir = Join-Path $repoRoot "frontend"
$distTarget = Join-Path $frontendDir "dist"
$staticTarget = Join-Path $repoRoot "backend\static"
. (Join-Path $PSScriptRoot "lib\build-safety.ps1")

Assert-SafeReplaceDirectory -RootPath $repoRoot -TargetPath $distTarget
if (Test-Path -LiteralPath $distTarget) { Remove-Item -LiteralPath $distTarget -Recurse -Force }

Push-Location $frontendDir
try {
    if (-not (Test-Path "node_modules")) { npm ci }
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "Frontend build failed." }
}
finally { Pop-Location }

Assert-SafeReplaceDirectory -RootPath $repoRoot -TargetPath $staticTarget
if (Test-Path -LiteralPath $staticTarget) { Remove-Item -LiteralPath $staticTarget -Recurse -Force }
New-Item -ItemType Directory -Path $staticTarget | Out-Null
Copy-Item -Path (Join-Path $frontendDir "dist\*") -Destination $staticTarget -Recurse
Write-Host "Built static application at $staticTarget"
