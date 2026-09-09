[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$OutputDirectory = "",
    [string]$CompilerPath = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    [switch]$SkipBuild,
    [switch]$KeepBuildArtifacts
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$installerDefinition = Join-Path $repoRoot "installer\windows-supporter.iss"
$compiler = (Resolve-Path -LiteralPath $CompilerPath -ErrorAction Stop).Path

function Invoke-GitText {
    param([string[]]$Arguments)
    $output = & git -C $repoRoot @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed: $($output -join [Environment]::NewLine)"
    }
    return (($output -join [Environment]::NewLine).Trim())
}

if (-not $Version) {
    $Version = Invoke-GitText @("describe", "--tags", "--exact-match", "HEAD")
}
$Version = $Version.Trim()
if ($Version.StartsWith("v", [StringComparison]::OrdinalIgnoreCase)) {
    $Version = $Version.Substring(1)
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Version must be an exact semantic version such as 0.22.0."
}

if (-not $SkipBuild) {
    $status = Invoke-GitText @("status", "--porcelain", "--untracked-files=all")
    if ($status) {
        throw "Installer build requires a clean tagged worktree."
    }
}

if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path ([IO.Path]::GetTempPath()) "windows-supporter-installer-v$Version"
}
$outputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null

$sourceExe = Join-Path $repoRoot "dist\windows-supporter.exe"
$installerName = "WindowsSupporter-v$Version-Setup.exe"
$installerPath = Join-Path $outputDirectory $installerName
$sidecarPath = "$installerPath.sha256"

if (-not $SkipBuild) {
    $previousArtifactOnly = $env:WINDOWS_SUPPORTER_BUILD_ARTIFACT_ONLY
    $previousStepLog = $env:WINDOWS_SUPPORTER_EMIT_STEP_LOG
    try {
        $env:WINDOWS_SUPPORTER_BUILD_ARTIFACT_ONLY = "1"
        $env:WINDOWS_SUPPORTER_EMIT_STEP_LOG = "1"
        Push-Location $repoRoot
        try {
            & (Join-Path $repoRoot "build.bat")
            if ($LASTEXITCODE -ne 0) {
                throw "build.bat failed with exit code $LASTEXITCODE."
            }
        }
        finally {
            Pop-Location
        }
    }
    finally {
        if ($null -eq $previousArtifactOnly) {
            Remove-Item Env:WINDOWS_SUPPORTER_BUILD_ARTIFACT_ONLY -ErrorAction SilentlyContinue
        }
        else {
            $env:WINDOWS_SUPPORTER_BUILD_ARTIFACT_ONLY = $previousArtifactOnly
        }
        if ($null -eq $previousStepLog) {
            Remove-Item Env:WINDOWS_SUPPORTER_EMIT_STEP_LOG -ErrorAction SilentlyContinue
        }
        else {
            $env:WINDOWS_SUPPORTER_EMIT_STEP_LOG = $previousStepLog
        }
    }
}

if (-not (Test-Path -LiteralPath $sourceExe -PathType Leaf)) {
    throw "Expected PyInstaller artifact was not found: $sourceExe"
}

Remove-Item -LiteralPath $installerPath, $sidecarPath -Force -ErrorAction SilentlyContinue
& $compiler "/DAppVersion=$Version" "/DSourceExe=$sourceExe" "/O$outputDirectory" $installerDefinition
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup compilation failed with exit code $LASTEXITCODE."
}
if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
    throw "Expected installer artifact was not found: $installerPath"
}

$hash = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToUpperInvariant()
Set-Content -LiteralPath $sidecarPath -Value "$hash  $installerName" -Encoding ascii

if (-not $KeepBuildArtifacts) {
    foreach ($generatedPath in @(
        (Join-Path $repoRoot "build"),
        (Join-Path $repoRoot "dist"),
        (Join-Path $repoRoot "windows-supporter.spec")
    )) {
        Remove-Item -LiteralPath $generatedPath -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Write-Output "INSTALLER_ARTIFACT=$installerPath"
Write-Output "INSTALLER_SHA256=$hash"
Write-Output "INSTALLER_SHA256_FILE=$sidecarPath"
