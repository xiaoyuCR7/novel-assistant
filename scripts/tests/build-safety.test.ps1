$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
. (Join-Path $repoRoot "scripts\lib\build-safety.ps1")

$testRoot = Join-Path ([IO.Path]::GetTempPath()) ("novel-assistant-build-safety-" + [Guid]::NewGuid().ToString("N"))
$realParent = Join-Path $testRoot "real-parent"
$linkedParent = Join-Path $testRoot "linked-parent"
$sentinel = Join-Path $realParent "sentinel.txt"

try {
    New-Item -ItemType Directory -Path $realParent | Out-Null
    Set-Content -LiteralPath $sentinel -Value "must survive rejected delete"
    Assert-SafeReplaceDirectory -RootPath $testRoot -TargetPath (Join-Path $realParent "dist")

    New-Item -ItemType Junction -Path $linkedParent -Target $realParent | Out-Null
    $rejected = $false
    try {
        Assert-SafeReplaceDirectory -RootPath $testRoot -TargetPath (Join-Path $linkedParent "dist")
    }
    catch {
        if ($_.Exception.Message -notmatch "reparse") { throw }
        $rejected = $true
    }
    if (-not $rejected) { throw "A target beneath a reparse-point parent was not rejected." }
    if (-not (Test-Path -LiteralPath $sentinel)) { throw "The reparse rejection did not preserve the sentinel file." }
    if ((Get-Content -LiteralPath $sentinel -Raw).Trim() -ne "must survive rejected delete") {
        throw "The reparse rejection changed the sentinel file."
    }
}
finally {
    if (Test-Path -LiteralPath $linkedParent) {
        $linkedItem = Get-Item -LiteralPath $linkedParent -Force
        if (-not ($linkedItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Refusing to unlink a non-reparse cleanup path: $($linkedItem.FullName)"
        }
        # Windows PowerShell 5.1 can throw a NullReferenceException when
        # Remove-Item unlinks a junction. Directory.Delete removes only the
        # reparse-point entry and never traverses into its target.
        [IO.Directory]::Delete($linkedItem.FullName)
    }
    if (Test-Path -LiteralPath $testRoot) {
        $resolvedTestRoot = (Resolve-Path -LiteralPath $testRoot).Path
        $resolvedTemp = (Resolve-Path -LiteralPath ([IO.Path]::GetTempPath())).Path.TrimEnd([IO.Path]::DirectorySeparatorChar)
        if (-not $resolvedTestRoot.StartsWith($resolvedTemp + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Test cleanup escaped the system temporary directory: $resolvedTestRoot"
        }
        Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
    }
}

Write-Host "Build deletion safety tests passed; reparse rejected and sentinel preserved."
