function Assert-SafeReplaceDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$RootPath,
        [Parameter(Mandatory = $true)][string]$TargetPath
    )

    $trimChars = [char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    $rootFull = [IO.Path]::GetFullPath($RootPath).TrimEnd($trimChars)
    $targetFull = [IO.Path]::GetFullPath($TargetPath).TrimEnd($trimChars)
    $rootPrefix = $rootFull + [IO.Path]::DirectorySeparatorChar
    if (-not $targetFull.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Replace target escaped the allowed root: $targetFull"
    }

    $current = $targetFull
    $resolvedAncestor = $null
    while ($true) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Refusing to replace through a reparse-point path: $current"
            }
            if (-not $item.PSIsContainer) {
                throw "Replace path segment is not a directory: $current"
            }
            if ($null -eq $resolvedAncestor) { $resolvedAncestor = (Resolve-Path -LiteralPath $current).Path }
        }

        if ($current.Equals($rootFull, [StringComparison]::OrdinalIgnoreCase)) { break }
        $parent = [IO.Directory]::GetParent($current)
        if ($null -eq $parent) { throw "Replace target has no parent within the allowed root: $targetFull" }
        $current = $parent.FullName.TrimEnd($trimChars)
        if (-not $current.Equals($rootFull, [StringComparison]::OrdinalIgnoreCase) -and
            -not $current.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Replace target escaped the allowed root while checking parents: $targetFull"
        }
    }

    $resolvedRoot = (Resolve-Path -LiteralPath $rootFull).Path.TrimEnd($trimChars)
    $resolvedPrefix = $resolvedRoot + [IO.Path]::DirectorySeparatorChar
    if ($null -eq $resolvedAncestor -or
        (-not $resolvedAncestor.Equals($resolvedRoot, [StringComparison]::OrdinalIgnoreCase) -and
         -not $resolvedAncestor.StartsWith($resolvedPrefix, [StringComparison]::OrdinalIgnoreCase))) {
        throw "Resolved replace target escaped the allowed root: $targetFull"
    }
}
