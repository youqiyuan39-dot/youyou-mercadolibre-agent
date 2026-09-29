param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $projectRoot

$candidates = @(
    @{ Command = 'py'; Prefix = @('-3') },
    @{ Command = 'python'; Prefix = @() }
)
$selected = $null
foreach ($candidate in $candidates) {
    if (-not (Get-Command $candidate.Command -ErrorAction SilentlyContinue)) { continue }
    $versionCheck = @($candidate.Prefix) + @('-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)')
    & $candidate.Command @versionCheck 2>$null
    if ($LASTEXITCODE -eq 0) { $selected = $candidate; break }
}
if (-not $selected) {
    Write-Host 'Python 3.11 or newer is required. Install from https://www.python.org/downloads/windows/' -ForegroundColor Red
    exit 1
}

$pythonArgs = @($selected.Prefix) + @('-m', 'src.draft_editor', '--port', '8789')
Write-Host "Project: $projectRoot"
if ($CheckOnly) {
    Write-Host "Python command: $($selected.Command) $($selected.Prefix -join ' ')"
    exit 0
}
Write-Host 'Open http://127.0.0.1:8789 after the server starts.'
& $selected.Command @pythonArgs
exit $LASTEXITCODE
