param([switch]$Assistant, [switch]$Rag, [switch]$Desktop, [string]$Python = 'python', [string]$Wheelhouse = '')
$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
$root = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $root
function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed: $Exe (exit $LASTEXITCODE)" }
}
Invoke-Checked $Python @('-c', 'import sys; assert sys.version_info[:2] == (3,12), "Use Python 3.12 x64"; assert sys.maxsize > 2**32, "Use 64-bit Python"')
$pipSource = @()
if ($Wheelhouse) { $pipSource = @('--no-index', '--find-links', (Resolve-Path -LiteralPath $Wheelhouse).Path) }
function Install-Environment([string]$Relative, [string]$Requirements) {
    $directory = Join-Path $root $Relative
    $exe = Join-Path $directory 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $exe)) { Invoke-Checked $Python @('-m', 'venv', $directory) }
    Invoke-Checked $exe (@('-m', 'pip', 'install') + $pipSource + @('-r', (Join-Path $root $Requirements)))
    Invoke-Checked $exe @('-m', 'pip', 'check')
}
Install-Environment '.venv' $(if ($Desktop) { 'requirements-desktop.lock.txt' } else { 'requirements.txt' })
if ($Assistant) { Install-Environment 'tools\assistant_env' 'tools\assistant-requirements.lock.txt' }
if ($Rag) { Install-Environment 'tools\rag_env' 'tools\rag-requirements.lock.txt' }
Write-Output 'Setup complete. No server, model, collection or publication was started.'
