param([switch]$Offline)
$ErrorActionPreference = 'Stop'
$taskBundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (Test-Path -LiteralPath $taskBundledPython) { $taskPythonPath = $taskBundledPython }
else {
    $taskPythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $taskPythonCommand) { throw 'Python 3.11 or newer is required to update market data.' }
    $taskPythonPath = $taskPythonCommand.Source
}
$taskUpdateArgs = @((Join-Path $PSScriptRoot 'scripts\update_data.py'))
if ($Offline) { $taskUpdateArgs += '--offline' }
$env:PYTHONIOENCODING = 'utf-8'
& $taskPythonPath @taskUpdateArgs
if ($LASTEXITCODE -eq 2) { Write-Warning 'Current sources were unavailable. Cached prices remain visible with a stale status. Retry later.' }
elseif ($LASTEXITCODE -ne 0) { throw 'Market data generation failed. See the diagnostics above.' }
