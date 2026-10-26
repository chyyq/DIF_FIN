param([int]$Port = 8000, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$taskNodeCommand = Get-Command node -ErrorAction SilentlyContinue
$taskNodePath = if ($taskNodeCommand) { $taskNodeCommand.Source } else { Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe' }
if (-not (Test-Path -LiteralPath $taskNodePath)) { throw 'Node.js is required. Install Node.js 20 or newer, then run this script again.' }
if ($Port -lt 1024 -or $Port -gt 65535) { throw 'Port must be between 1024 and 65535.' }
$env:LBR_PORT = [string]$Port
$env:LBR_OPEN_BROWSER = if ($NoBrowser) { '0' } else { '1' }
Write-Host "LBR local preview: http://127.0.0.1:$Port"
& $taskNodePath (Join-Path $PSScriptRoot 'scripts\serve.mjs')
if ($LASTEXITCODE -ne 0) { throw 'LBR preview did not start or stopped with an error. Check the port and message above.' }
