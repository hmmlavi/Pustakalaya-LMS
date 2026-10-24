param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$distRoot = Join-Path $projectRoot "dist"
$releaseRoot = Join-Path $distRoot "release"

if (Test-Path $releaseRoot) { Remove-Item -Recurse -Force $releaseRoot }
New-Item -ItemType Directory -Force -Path $releaseRoot | Out-Null

& $Python -m PyInstaller --noconfirm --clean (Join-Path $projectRoot "packaging\pustakalaya.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

$portable = Join-Path $releaseRoot "Pustakalaya-windows-portable.zip"
$exe = Join-Path $distRoot "Pustakalaya.exe"
if (-not (Test-Path $exe) -or (Get-Item $exe).Length -eq 0) { throw "Expected executable was not produced" }
Copy-Item $exe (Join-Path $releaseRoot "Pustakalaya.exe")
Compress-Archive -Path (Join-Path $releaseRoot "Pustakalaya.exe") -DestinationPath $portable -CompressionLevel Optimal

function Get-Sha256Line([string]$Path) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.IO.File]::ReadAllBytes($Path)
        $hash = ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
        "$hash  $([IO.Path]::GetFileName($Path))"
    } finally {
        $sha.Dispose()
    }
}

Get-Sha256Line (Join-Path $releaseRoot "Pustakalaya.exe") | Set-Content (Join-Path $releaseRoot "SHA256SUMS.txt")
Get-Sha256Line $portable | Add-Content (Join-Path $releaseRoot "SHA256SUMS.txt")

Write-Host "Windows artifacts written to $releaseRoot"
