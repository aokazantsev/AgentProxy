param(
    [Parameter(Mandatory = $true)][string]$Root,
    [Parameter(Mandatory = $true)][string]$Output
)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path -LiteralPath $Root).Path.TrimEnd([char]92)
$staging = Join-Path ([IO.Path]::GetTempPath()) ("Payload-" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $staging | Out-Null

foreach ($entry in Get-Content -LiteralPath (Join-Path $Root 'installer\payload.txt')) {
    $relative = $entry.Trim()
    if ($relative.Length -eq 0) { continue }
    $source = Join-Path $Root $relative
    if (-not (Test-Path -LiteralPath $source)) { throw "payload file missing: $relative" }
    $destination = Join-Path $staging $relative
    New-Item -ItemType Directory -Force -Path (Split-Path $destination) | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination -Recurse
}

New-Item -ItemType Directory -Force -Path (Split-Path $Output) | Out-Null
if (Test-Path $Output) { Remove-Item $Output -Force }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::CreateFromDirectory($staging, $Output, [IO.Compression.CompressionLevel]::Optimal, $false)
Remove-Item $staging -Recurse -Force
"payload: {0:N1} KB" -f ((Get-Item $Output).Length / 1KB)
