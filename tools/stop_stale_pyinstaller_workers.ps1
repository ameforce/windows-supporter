param(
    [Parameter(Mandatory = $true)]
    [string]$RepoRoot
)

$resolvedRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
$venvPython = Join-Path $resolvedRoot ".venv\Scripts\python.exe"

Get-CimInstance Win32_Process |
    Where-Object {
        ($_.Name -eq "python.exe" -and $_.ExecutablePath -eq $venvPython -and $_.CommandLine -like "*PyInstaller*") -or
        ($_.Name -eq "uv.exe" -and $_.CommandLine -like "*PyInstaller*" -and $_.CommandLine -like "*$resolvedRoot*")
    } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
