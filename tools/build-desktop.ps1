$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $root
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run tools/setup.ps1 -Desktop first.' }
& $python -m PyInstaller --noconfirm --distpath 'desktop\dist' --workpath 'desktop\build' 'desktop\YoyoAgentOperations.spec'
if ($LASTEXITCODE -ne 0) { throw 'Desktop build failed.' }
Write-Output 'Built desktop/dist/YoyoAgentOperations/YoyoAgentOperations.exe; no application was launched.'
