param(
    [ValidateSet('new', 'classic')]
    [string]$Version = 'new',
    [string]$DestinationDirectory = [Environment]::GetFolderPath('DesktopDirectory'),
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$launcher = if ($Version -eq 'new') { '启动新版工作台.cmd' } else { '启动工作台.cmd' }
$target = Join-Path $projectRoot $launcher
$icon = Join-Path $projectRoot 'assets\brand\youyou-original.ico'
if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { throw "Launcher not found: $target" }
if (-not (Test-Path -LiteralPath $icon -PathType Leaf)) { throw "Icon not found: $icon" }
$directory = [System.IO.Path]::GetFullPath($DestinationDirectory)
$shortcut = Join-Path $directory '悠悠 Agent 工作台.lnk'
if ($CheckOnly) {
    Write-Host "Target: $target"
    Write-Host "Icon: $icon"
    Write-Host "Shortcut: $shortcut"
    exit 0
}
if (-not (Test-Path -LiteralPath $directory -PathType Container)) { throw "Destination directory not found: $directory" }
if (Test-Path -LiteralPath $shortcut) { throw "Shortcut already exists; move or rename it first: $shortcut" }
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($shortcut)
$link.TargetPath = $target
$link.WorkingDirectory = $projectRoot
$link.IconLocation = "$icon,0"
$link.Description = '悠悠美客多 AI 上架工作台'
$link.Save()
Write-Host "Created: $shortcut"