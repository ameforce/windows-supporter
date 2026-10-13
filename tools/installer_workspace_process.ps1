# Native invocation for the installer workspace owner, including Windows PS 5.1.
# ProcessStartInfo bypasses PowerShell's legacy native argument reconstruction.
function ConvertTo-InstallerNativeArgument {
    param([AllowEmptyString()][string]$Argument)
    $escaped = [regex]::Replace($Argument, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Invoke-InstallerWorkspaceProcess {
    param(
        [string]$Executable,
        [string[]]$Arguments,
        [switch]$CaptureOutput
    )
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $Executable
    $startInfo.UseShellExecute = $false
    $startInfo.Arguments = (($Arguments | ForEach-Object {
        ConvertTo-InstallerNativeArgument $_
    }) -join ' ')
    if ($CaptureOutput) {
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
        $startInfo.StandardOutputEncoding = $strictUtf8
        $startInfo.StandardErrorEncoding = $strictUtf8
    }
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        $null = $process.Start()
        if ($CaptureOutput) {
            # Drain both streams concurrently, without merging their contents.
            $stdoutTask = $process.StandardOutput.ReadToEndAsync()
            $stderrTask = $process.StandardError.ReadToEndAsync()
        }
        $process.WaitForExit()
        if ($CaptureOutput) {
            return [pscustomobject]@{
                ExitCode = $process.ExitCode
                Stdout = $stdoutTask.GetAwaiter().GetResult()
                Stderr = $stderrTask.GetAwaiter().GetResult()
            }
        }
        return $process.ExitCode
    }
    finally {
        $process.Dispose()
    }
}

function Initialize-InstallerWorkspaceChildStreams {
    # Explicit writers affect only this child process; changing console code
    # pages would also affect another host attached to the same console.
    $utf8 = New-Object System.Text.UTF8Encoding($false, $true)
    $stdoutWriter = New-Object System.IO.StreamWriter([Console]::OpenStandardOutput(), $utf8)
    $stderrWriter = New-Object System.IO.StreamWriter([Console]::OpenStandardError(), $utf8)
    $stdoutWriter.AutoFlush = $true
    $stderrWriter.AutoFlush = $true
    [Console]::SetOut($stdoutWriter)
    [Console]::SetError($stderrWriter)
}

function Write-InstallerWorkspaceOutput {
    param($Result)
    $reader = New-Object System.IO.StringReader($Result.Stdout)
    try {
        while ($null -ne ($line = $reader.ReadLine())) {
            Write-Output $line
        }
    }
    finally { $reader.Dispose() }
    # A successful native child may write diagnostics on stderr. Do not turn
    # these into terminating PowerShell errors under ErrorActionPreference=Stop.
    if ($Result.Stderr) { [Console]::Error.Write($Result.Stderr) }
}

function Resolve-InstallerWorkspacePython {
    param([string]$RepoRoot, [string[]]$Candidates = $null)
    if ($null -eq $Candidates) {
        $Candidates = @()
        if ($env:WINDOWS_SUPPORTER_BUILD_PYTHON) {
            $Candidates += $env:WINDOWS_SUPPORTER_BUILD_PYTHON
        }
        $Candidates += @(Get-Command python.exe -CommandType Application -All -ErrorAction SilentlyContinue |
            ForEach-Object { $_.Source })
        $launcher = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue
        if ($launcher) {
            try {
                $located = Invoke-InstallerWorkspaceProcess -Executable $launcher.Source -CaptureOutput -Arguments @(
                    '-3.14', '-X', 'utf8', '-c', 'import sys; print(sys.executable)'
                )
                if ($located.ExitCode -eq 0) { $Candidates += $located.Stdout.Trim() }
            }
            catch { Write-Verbose "Python launcher unavailable: $_" }
        }
        # A working venv is a last resort; mere file existence never selects it.
        $Candidates += Join-Path $RepoRoot '.venv\Scripts\python.exe'
    }
    foreach ($candidate in ($Candidates | Select-Object -Unique)) {
        if (-not $candidate) { continue }
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        try {
            $probe = Invoke-InstallerWorkspaceProcess -Executable $candidate -CaptureOutput -Arguments @(
                '-X', 'utf8', '-c',
                'import pathlib, subprocess, tempfile, uuid, sys; sys.exit(1) if sys.version_info < (3, 14) else print("WINDOWS_SUPPORTER_PYTHON_READY")'
            )
            if ($probe.ExitCode -eq 0 -and $probe.Stdout.Trim() -eq 'WINDOWS_SUPPORTER_PYTHON_READY') {
                return $candidate
            }
        }
        catch { Write-Verbose "Python candidate unavailable: $candidate : $_" }
    }
    throw 'No executable Python 3.14 or newer is available for the installer workspace owner.'
}
