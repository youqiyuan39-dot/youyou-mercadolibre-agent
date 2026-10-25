param(
    [switch]$CheckOnly,
    [ValidateSet('classic', 'new')]
    [string]$Version = 'classic'
)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $projectRoot

$candidates = @(
    @{ Command = (Join-Path $projectRoot '.venv\Scripts\python.exe'); Prefix = @() },
    @{ Command = 'py'; Prefix = @('-3') },
    @{ Command = 'python'; Prefix = @() }
)
$selected = $null
foreach ($candidate in $candidates) {
    if (-not (Get-Command $candidate.Command -ErrorAction SilentlyContinue)) { continue }
    $versionCheck = @($candidate.Prefix) + @('-c', 'import sys; import PIL; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)')
    & $candidate.Command @versionCheck 2>$null
    if ($LASTEXITCODE -eq 0) { $selected = $candidate; break }
}
if (-not $selected) {
    Write-Host 'Python 3.12 and project dependencies are required. Run tools/setup.ps1 first.' -ForegroundColor Red
    exit 1
}

if ($Version -eq 'new') {
    $module = 'src.workbench_v3'
    $port = '8790'
} else {
    $module = 'src.draft_editor'
    $port = '8789'
}
$pythonArgs = @($selected.Prefix) + @('-m', $module, '--port', $port)
Write-Host "Project: $projectRoot"
if ($CheckOnly) {
    Write-Host "Python command: $($selected.Command) $($selected.Prefix -join ' ')"
    Write-Host "Module: $module; Port: $port"
    exit 0
}
Write-Host "Open http://127.0.0.1:$port after the server starts."
& $selected.Command @pythonArgs
exit $LASTEXITCODE
