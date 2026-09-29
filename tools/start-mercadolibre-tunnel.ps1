param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$cloudflared = Join-Path $PSScriptRoot 'cloudflared.exe'
$config = Join-Path $projectRoot 'config\cloudflared-mercadolibre.yml'
$runtimeConfig = Join-Path $projectRoot 'config\cloudflared-runtime.yml'

if (-not (Test-Path -LiteralPath $cloudflared)) {
    $installed = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($installed) { $cloudflared = $installed.Source }
    else { throw 'Install cloudflared or put cloudflared.exe in the tools folder.' }
}
if (-not (Test-Path -LiteralPath $config)) { throw "Tunnel config is missing: $config" }
$content = Get-Content -LiteralPath $config -Encoding UTF8
$tunnelLine = $content | Where-Object { $_ -match '^tunnel:\s*([0-9a-fA-F-]{36})\s*$' } | Select-Object -First 1
if (-not $tunnelLine) { throw 'Set a valid tunnel UUID in config/cloudflared-mercadolibre.yml.' }
$tunnelId = [regex]::Match($tunnelLine, '^tunnel:\s*([0-9a-fA-F-]{36})').Groups[1].Value
$credentials = Join-Path (Join-Path $env:USERPROFILE '.cloudflared') "$tunnelId.json"
if (-not (Test-Path -LiteralPath $credentials)) {
    throw "Tunnel credentials are not installed for this Windows user: $credentials"
}
$safePath = $credentials.Replace('\', '/')
$runtime = @("tunnel: $tunnelId", "credentials-file: '$safePath'") + ($content | Where-Object { $_ -notmatch '^\s*(tunnel|credentials-file):' })
[System.IO.File]::WriteAllLines($runtimeConfig, $runtime, [System.Text.UTF8Encoding]::new($false))
if ($CheckOnly) {
    Write-Host "Tunnel config is ready for $tunnelId"
    exit 0
}
& $cloudflared tunnel --config $runtimeConfig run $tunnelId
exit $LASTEXITCODE
