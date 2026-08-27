param(
  [Parameter(Mandatory = $true)][string]$InputPath,
  [Parameter(Mandatory = $true)][string]$OutputPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Drawing
$inputFile = [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($InputPath))
$outputFile = [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($OutputPath))
if (-not (Test-Path -LiteralPath $inputFile -PathType Leaf)) {
  throw "Input bitmap does not exist: $inputFile"
}

$image = [System.Drawing.Image]::FromFile($inputFile)
try {
  $image.Save($outputFile, [System.Drawing.Imaging.ImageFormat]::Png)
} finally {
  $image.Dispose()
}

Write-Output $outputFile
