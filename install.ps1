# ImgSortSearch - portable Installation
# Installiert Python, alle Pakete, exiftool und die KI-Modelle ausschliesslich
# in den Ordner, in dem dieses Skript liegt. Nichts wird systemweit installiert.
# Deinstallieren = Ordner loeschen.
param(
    [switch]$NurCpu,          # keine Grafikkarten-Beschleunigung installieren
    [switch]$OhneModelle      # Modelle erst beim ersten Start laden
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # macht Invoke-WebRequest deutlich schneller
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root   = Split-Path -Parent $MyInvocation.MyCommand.Path
$Umg    = Join-Path $Root "umgebung"
$Daten  = Join-Path $Root "daten"
$UvDir  = Join-Path $Umg "uv"
$Venv   = Join-Path $Umg "venv"
$Py     = Join-Path $Venv "Scripts\python.exe"

# Alle Zwischenspeicher und Downloads bleiben im Ordner
$env:UV_CACHE_DIR           = Join-Path $Umg "cache\uv"
$env:UV_PYTHON_INSTALL_DIR  = Join-Path $Umg "python"
$env:UV_PYTHON_PREFERENCE   = "only-managed"
$env:UV_NO_CONFIG           = "1"
$env:UV_LINK_MODE           = "copy"
$env:IMGSORTSEARCH_HOME     = $Daten
$env:HF_HOME                = Join-Path $Daten "modelle\hf"
$env:TORCH_HOME             = Join-Path $Daten "modelle\torch"
$env:PIP_CACHE_DIR          = Join-Path $Umg "cache\pip"
$env:PYTHONNOUSERSITE       = "1"

function Schritt($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Laden($url, $ziel) {
    for ($i = 1; $i -le 3; $i++) {
        try { Invoke-WebRequest -Uri $url -OutFile $ziel -UseBasicParsing; return }
        catch { if ($i -eq 3) { throw }; Start-Sleep -Seconds (3 * $i) }
    }
}

New-Item -ItemType Directory -Force -Path $Umg, $Daten, $UvDir | Out-Null

Schritt "1/6  uv (Paketverwaltung) laden"
$uv = Join-Path $UvDir "uv.exe"
if (-not (Test-Path $uv)) {
    $zip = Join-Path $Umg "uv.zip"
    Laden "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip" $zip
    Expand-Archive -Path $zip -DestinationPath $UvDir -Force
    Remove-Item $zip
}
& $uv --version

Schritt "2/6  Python 3.12 in den Ordner installieren"
& $uv python install 3.12
if (-not (Test-Path $Py)) { & $uv venv $Venv --python 3.12 }

Schritt "3/6  PyTorch installieren"
$gpu = $false
if (-not $NurCpu) {
    try { $null = & nvidia-smi -L 2>$null; if ($LASTEXITCODE -eq 0) { $gpu = $true } } catch { }
}
if ($gpu) {
    Write-Host "NVIDIA-Grafikkarte gefunden: installiere die beschleunigte Version (grosser Download, ca. 2,5 GB)."
    & $uv pip install --python $Py torch torchvision --index-url https://download.pytorch.org/whl/cu126
} else {
    Write-Host "Installiere die CPU-Version."
    & $uv pip install --python $Py torch torchvision --index-url https://download.pytorch.org/whl/cpu
}
if ($LASTEXITCODE -ne 0) { throw "PyTorch-Installation fehlgeschlagen" }

Schritt "4/6  Programmpakete installieren"
& $uv pip install --python $Py -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Paketinstallation fehlgeschlagen" }

Schritt "5/6  exiftool laden"
$exDir = Join-Path $Umg "exiftool"
if (-not (Test-Path (Join-Path $exDir "exiftool.exe"))) {
    try {
        $ver = (Invoke-WebRequest -Uri "https://exiftool.org/ver.txt" -UseBasicParsing).Content.Trim()
        $zip = Join-Path $Umg "exiftool.zip"
        Laden "https://exiftool.org/exiftool-$($ver)_64.zip" $zip
        $tmp = Join-Path $Umg "exiftool-tmp"
        Expand-Archive -Path $zip -DestinationPath $tmp -Force
        $inner = Get-ChildItem $tmp -Directory | Select-Object -First 1
        if (Test-Path $exDir) { Remove-Item $exDir -Recurse -Force }
        Move-Item $inner.FullName $exDir
        Rename-Item (Join-Path $exDir "exiftool(-k).exe") "exiftool.exe"
        Remove-Item $zip, $tmp -Recurse -Force
        Write-Host "exiftool $ver installiert."
    } catch {
        Write-Warning "exiftool konnte nicht geladen werden ($_). Das Programm laeuft trotzdem, nur ohne RAW-Vorschau und eingebettete Tags."
    }
}

Schritt "6/6  KI-Modelle laden (einmalig, einige GB)"
if ($OhneModelle) {
    Write-Host "Uebersprungen. Die Modelle werden beim ersten Indexlauf geladen."
} else {
    $env:PATH = "$exDir;$env:PATH"
    Push-Location $Root
    & $Py -m imgsortsearch.vorbereiten
    $ok = $LASTEXITCODE
    Pop-Location
    if ($ok -ne 0) { Write-Warning "Nicht alle Modelle konnten geladen werden. Beim Start wird es erneut versucht." }
}

Write-Host ""
Write-Host "Fertig! Starten mit  start.bat  (Doppelklick)." -ForegroundColor Green
