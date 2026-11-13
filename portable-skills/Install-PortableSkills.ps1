[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = "Stop"

$sourceRoot = $PSScriptRoot
$skillsRoot = Join-Path $HOME ".claude\skills"
$skillNames = @("lean-engineering", "github-sync")

foreach ($skillName in $skillNames) {
    $sourceFile = Join-Path (Join-Path $sourceRoot $skillName) "SKILL.md"
    if (-not (Test-Path -LiteralPath $sourceFile -PathType Leaf)) {
        throw "Required skill file is missing: $sourceFile"
    }

    $destinationDirectory = Join-Path $skillsRoot $skillName
    $destinationFile = Join-Path $destinationDirectory "SKILL.md"

    if (Test-Path -LiteralPath $destinationFile -PathType Leaf) {
        $sourceHash = (Get-FileHash -LiteralPath $sourceFile -Algorithm SHA256).Hash
        $destinationHash = (Get-FileHash -LiteralPath $destinationFile -Algorithm SHA256).Hash

        if ($sourceHash -eq $destinationHash) {
            Write-Host "Already installed and identical: $skillName"
            continue
        }

        Write-Warning "Existing skill differs; leaving it untouched: $destinationFile"
        continue
    }

    if ($PSCmdlet.ShouldProcess($destinationFile, "Install $skillName skill")) {
        New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
        Copy-Item -LiteralPath $sourceFile -Destination $destinationFile
        Write-Host "Installed: $skillName"
    }
}

Write-Host ""
Write-Host "Done. Verify the files under $skillsRoot, then start a fresh Claude Code session."
Write-Host "This script does not install RTK or modify Claude Code settings."
