param([switch]$Rag)
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
$folder=Join-Path $root 'models'
New-Item -ItemType Directory -Path $folder -Force | Out-Null
$revision='3885219b6810b007914f3a7950a8d1b469d598a5'
$files=@(
    @{Name='Qwen3.5-9B-Q4_K_M.gguf'; Hash='03b74727a860a56338e042c4420bb3f04b2fec5734175f4cb9fa853daf52b7e8'},
    @{Name='mmproj-F16.gguf'; Hash='f70dc3509053962b0d0d3ee8a7eacebf5d60aa560cad78254ae8698516ae029f'}
)
foreach ($item in $files) {
    $target=Join-Path $folder $item.Name
    if (Test-Path -LiteralPath $target) {
        if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLower() -ne $item.Hash) { throw "Existing model checksum mismatch: $target" }
        continue
    }
    $partial=$target+'.download'
    if (Test-Path -LiteralPath $partial) { throw "Existing partial download retained: $partial" }
    Invoke-WebRequest -UseBasicParsing -Uri "https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/$revision/$($item.Name)" -OutFile $partial
    if ((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLower() -ne $item.Hash) { throw "Downloaded checksum mismatch; file retained: $partial" }
    Move-Item -LiteralPath $partial -Destination $target
}
if ($Rag) {
    $python=Join-Path $root 'tools\rag_env\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) { throw 'Run setup.ps1 -Rag first.' }
    Set-Location -LiteralPath $root
    & $python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-zh-v1.5',cache_dir='tools/rag_models',providers=['CPUExecutionProvider'])"
    if ($LASTEXITCODE -ne 0) { throw 'Embedding model installation failed.' }
}
Write-Output 'Model weights verified. No server or chat was started. Install the documented llama.cpp runtime separately.'
