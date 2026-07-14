$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:DEVCTL_TOOL_ROOT = $Root
$env:DEVCTL_OPS_ROOT = $Root
if (-not $env:DEVCTL_REPO_ROOT) {
    $RepoRoot = git rev-parse --show-toplevel 2>$null
    if ($LASTEXITCODE -eq 0 -and $RepoRoot) {
        $env:DEVCTL_REPO_ROOT = $RepoRoot.Trim()
    } else {
        $env:DEVCTL_REPO_ROOT = (Get-Location).Path
    }
}
$env:PYTHONDONTWRITEBYTECODE = "1"
if ($env:PYTHONPATH) {
    $env:PYTHONPATH = "$Root;$env:PYTHONPATH"
} else {
    $env:PYTHONPATH = $Root
}

if ($args.Count -eq 0 -or $args[0] -in @("help", "-h", "--help")) {
    Get-Content -Raw (Join-Path $Root "help.txt")
    exit 0
}

python -m xflow @args
exit $LASTEXITCODE
