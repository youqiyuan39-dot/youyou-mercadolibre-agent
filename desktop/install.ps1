param([switch]$DesktopShortcut)
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
$exe=Join-Path $PSScriptRoot 'dist\YoyoAgentOperations\YoyoAgentOperations.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw 'Build first with tools/build-desktop.ps1.' }
$directory=Join-Path $root '桌面入口'
New-Item -ItemType Directory -Path $directory -Force | Out-Null
$targets=@((Join-Path $directory '悠悠 Agent 工作台.lnk'))
if ($DesktopShortcut) { $targets+=Join-Path ([Environment]::GetFolderPath('Desktop')) '悠悠 Agent 工作台.lnk' }
foreach ($target in $targets) { if (Test-Path -LiteralPath $target) { throw "Existing shortcut retained: $target" } }
$shell=New-Object -ComObject WScript.Shell
foreach ($target in $targets) {
    $link=$shell.CreateShortcut($target)
    $link.TargetPath=$exe
    $link.WorkingDirectory=$root
    $link.IconLocation=(Join-Path $PSScriptRoot 'youyou-original.ico')+',0'
    $link.Description='悠悠 Agent 工作台；连接本项目后台'
    $link.Save()
}
Write-Output 'Created shortcuts. Application was not launched.'
