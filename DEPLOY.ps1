[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?/?$')]
    [string]$RepositoryUrl,
    [switch]$Push
)

$ErrorActionPreference = 'Stop'
$projectPath = [IO.Path]::GetFullPath($PSScriptRoot)
Set-Location -LiteralPath $projectPath

function Invoke-ProjectGit {
    param([string[]]$GitArguments)
    & git -C $projectPath @GitArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Git command failed: git $($GitArguments -join ' ')"
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $projectPath '.git'))) {
    Invoke-ProjectGit -GitArguments @('init', '-b', 'main')
}

$repositoryRoot = & git -C $projectPath rev-parse --show-toplevel 2>$null
if ($LASTEXITCODE -ne 0 -or -not $repositoryRoot) {
    throw 'The project .git is not a valid repository. Review it before deploying.'
}
$resolvedRepositoryRoot = [IO.Path]::GetFullPath([string]$repositoryRoot).TrimEnd('\', '/')
$resolvedProjectRoot = $projectPath.TrimEnd('\', '/')
if (-not [string]::Equals($resolvedRepositoryRoot, $resolvedProjectRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Git resolved to a parent or different workspace. No remote, index or commit was changed.'
}

$existingRemote = & git -C $projectPath remote get-url origin 2>$null
if ($LASTEXITCODE -eq 0) {
    $expected = $RepositoryUrl.TrimEnd('/').Replace('.git', '')
    $actual = ([string]$existingRemote).TrimEnd('/').Replace('.git', '')
    if ($actual -ne $expected) {
        throw 'This project already has a different origin. Review it before changing the remote.'
    }
} else {
    Invoke-ProjectGit -GitArguments @('remote', 'add', 'origin', $RepositoryUrl)
}

$branchName = & git -C $projectPath symbolic-ref --short HEAD
if ($LASTEXITCODE -ne 0 -or $branchName -ne 'main') {
    throw 'Deploy expects a main branch. Review the branch before continuing.'
}

Invoke-ProjectGit -GitArguments @('add', '--', '.')
& git -C $projectPath diff --cached --quiet
if ($LASTEXITCODE -eq 1) {
    Invoke-ProjectGit -GitArguments @('commit', '-m', 'Build LBR wave research site')
} elseif ($LASTEXITCODE -ne 0) {
    throw 'Unable to inspect staged changes.'
}

if ($Push) {
    Invoke-ProjectGit -GitArguments @('push', '-u', 'origin', 'main')
    Write-Output 'Pushed main. In GitHub Settings > Pages, select GitHub Actions as the source.'
} else {
    Write-Output 'Local main and origin are prepared. No network push was performed.'
    Write-Output 'Review the local commit and repository, then rerun with -Push when ready.'
}
