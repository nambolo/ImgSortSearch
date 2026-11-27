# ImgSortSearch - portable Installation
# Installiert Python, alle Pakete, exiftool und die KI-Modelle ausschliesslich
# in den Ordner, in dem dieses Skript liegt. Nichts wird systemweit installiert.
# Deinstallieren = Ordner loeschen.
param(
    # auto: Grafikkarte erkennen. nvidia: CUDA. directml: AMD Radeon / Intel-Grafik. cpu: ohne Grafikkarte.
    [ValidateSet("auto", "nvidia", "directml", "cpu")]
    [string]$Beschleunigung = "auto",
    [switch]$NurCpu,          # wie -Beschleunigung cpu
    [switch]$OhneModelle,     # Modelle erst beim ersten Start laden
    [switch]$NurExiftool      # nur exiftool (neu) laden, alles andere bleibt, wie es ist
)
if ($NurCpu) { $Beschleunigung = "cpu" }
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
$env:UV_PYTHON_BIN_DIR      = Join-Path $Umg "python\bin"   # keine python.exe-Verknuepfungen im Benutzerprofil
$env:UV_NO_MODIFY_PATH      = "1"
$env:IMGSORTSEARCH_HOME     = $Daten
$env:HF_HOME                = Join-Path $Daten "modelle\hf"
$env:TORCH_HOME             = Join-Path $Daten "modelle\torch"
$env:PIP_CACHE_DIR          = Join-Path $Umg "cache\pip"
$env:PYTHONNOUSERSITE       = "1"

function Schritt($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
# Eigene Kennung: SourceForge liefert "Browsern" (alles mit "Mozilla", auch der PowerShell-Standard)
# eine Warteseite statt der Datei
$UA = "ImgSortSearch-Installer/1.0"
function Laden($url, $ziel) {
    for ($i = 1; $i -le 3; $i++) {
        try { Invoke-WebRequest -Uri $url -OutFile $ziel -UseBasicParsing -UserAgent $UA; return }
        catch { if ($i -eq 3) { throw }; Start-Sleep -Seconds (3 * $i) }
    }
}
function IstZip($datei) {
    if (-not (Test-Path $datei) -or (Get-Item $datei).Length -lt 1000) { return $false }
    $fs = [IO.File]::OpenRead($datei)
    try { return ($fs.ReadByte() -eq 0x50 -and $fs.ReadByte() -eq 0x4B) } finally { $fs.Close() }
}
function ExiftoolLaden($exDir) {
    # exiftool.org hat nur die neueste Version; SourceForge ist der offizielle Spiegel (mit allen Versionen)
    $quellen = @(
        @{ ver = "https://exiftool.org/ver.txt"; zip = "https://exiftool.org/exiftool-{0}_64.zip" },
        @{ ver = "https://downloads.sourceforge.net/project/exiftool/ver.txt"; zip = "https://downloads.sourceforge.net/project/exiftool/exiftool-{0}_64.zip" }
    )
    $zip = Join-Path $Umg "exiftool.zip"
    $tmp = Join-Path $Umg "exiftool-tmp"
    $versionen = @()
    $fehler = @()
    foreach ($q in $quellen) {
        try {
            $c = (Invoke-WebRequest -Uri $q.ver -UseBasicParsing -UserAgent $UA).Content
            if ($c -is [byte[]]) { $c = [Text.Encoding]::ASCII.GetString($c) }   # Spiegel liefern ver.txt als Binaerdatei
            $versionen += ([string]$c).Trim()
        } catch { $fehler += "$($q.ver): $($_.Exception.Message)" }
    }
    $versionen = @($versionen | Where-Object { $_ -match '^\d+\.\d+$' } | Select-Object -Unique)
    $geladen = $null
    foreach ($ver in $versionen) {
        foreach ($q in $quellen) {
            $url = $q.zip -f $ver
            try {
                if (Test-Path $zip) { Remove-Item $zip -Force }
                Laden $url $zip
                if (IstZip $zip) { $geladen = $ver; break }
                $fehler += "$($url): keine ZIP-Datei erhalten"
            } catch { $fehler += "$($url): $($_.Exception.Message)" }
        }
        if ($geladen) { break }
    }
    if (-not $geladen) { throw ("kein Download hat geklappt: " + ($fehler -join " | ")) }
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $exe = Get-ChildItem $tmp -Recurse -Filter "exiftool*.exe" | Select-Object -First 1
    if (-not $exe) { throw "exiftool.exe nicht im Archiv gefunden" }
    if (Test-Path $exDir) { Remove-Item $exDir -Recurse -Force }
    Move-Item $exe.Directory.FullName $exDir
    $neu = Join-Path $exDir $exe.Name
    if ($exe.Name -ne "exiftool.exe") { Rename-Item -LiteralPath $neu -NewName "exiftool.exe" }
    Remove-Item $zip, $tmp -Recurse -Force -ErrorAction SilentlyContinue
    & (Join-Path $exDir "exiftool.exe") -ver | Out-Null
    Pruefe "exiftool-Test"
    return $geladen
}

function Pruefe($was) { if ($LASTEXITCODE -ne 0) { throw "$was fehlgeschlagen (Fehlercode $LASTEXITCODE)" } }

New-Item -ItemType Directory -Force -Path $Umg, $Daten, $UvDir | Out-Null

if ($NurExiftool) {
    Schritt "exiftool laden"
    $v = ExiftoolLaden (Join-Path $Umg "exiftool")
    Write-Host "exiftool $v installiert. Fertig." -ForegroundColor Green
    exit 0
}

# Feste Versionen, damit die Installation in einem Jahr noch genauso funktioniert
$UvVersion          = "0.13.10"
$TorchDirectml      = "torch-directml==0.2.5.dev240914"   # braucht genau torch 2.4.1 / torchvision 0.19.1

Schritt "1/6  uv (Paketverwaltung) laden"
$uv = Join-Path $UvDir "uv.exe"
$uvOk = $false
if (Test-Path $uv) { $uvOk = ((& $uv --version) -match [regex]::Escape($UvVersion)) }
if (-not $uvOk) {
    $zip = Join-Path $Umg "uv.zip"
    Laden "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip" $zip
    Expand-Archive -Path $zip -DestinationPath $UvDir -Force
    Remove-Item $zip
}
& $uv --version
Pruefe "uv"

Schritt "2/6  Python 3.12 in den Ordner installieren"
# --no-bin/--no-registry: nichts im Benutzerprofil oder in der Windows-Registry eintragen
& $uv python install 3.12 --no-bin --no-registry
Pruefe "Python-Installation"
if (-not (Test-Path $Py)) { & $uv venv $Venv --python 3.12; Pruefe "Anlegen der Umgebung" }

Schritt "3/6  Grafikkarte erkennen und PyTorch installieren"
$karten = @()
try { $karten = @(Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }) } catch { }
if ($karten.Count) { Write-Host ("Gefundene Grafik: " + ($karten -join ", ")) }
if ($Beschleunigung -eq "auto") {
    if ($karten -match "NVIDIA") { $Beschleunigung = "nvidia" }
    elseif ($karten -match "AMD|Radeon|Intel|Arc|Iris|UHD") { $Beschleunigung = "directml" }
    else { $Beschleunigung = "cpu" }
}
$constraints = Join-Path $Umg "constraints.txt"
# Wurde vorher mit einer anderen Grafikkarten-Variante installiert, PyTorch austauschen
# (sonst bleibt z. B. die DirectML-Version liegen, weil "torch" schon vorhanden ist)
$merker = Join-Path $Umg "beschleunigung.txt"
$vorher = if (Test-Path $merker) { "$(Get-Content $merker -Raw)".Trim() } else { "" }
if ($vorher -and $vorher -ne $Beschleunigung) {
    Write-Host "Vorher installiert: $vorher, jetzt: $Beschleunigung. Tausche PyTorch aus."
    & $uv pip uninstall --python $Py torch torchvision torch-directml
}
switch ($Beschleunigung) {
    "nvidia" {
        Write-Host "NVIDIA: installiere PyTorch mit CUDA (grosser Download, ca. 2,5 GB)."
        & $uv pip install --python $Py torch torchvision --index-url https://download.pytorch.org/whl/cu126
    }
    "directml" {
        Write-Host "AMD/Intel: installiere PyTorch mit DirectML (nutzt bei Laptops auch den gemeinsamen Grafikspeicher)."
        # torch-directml braucht genau torch 2.4.1; dazu passende Versionen der anderen Pakete
        & $uv pip install --python $Py $TorchDirectml
    }
    default {
        Write-Host "Installiere die CPU-Version."
        & $uv pip install --python $Py torch torchvision --index-url https://download.pytorch.org/whl/cpu
    }
}
Pruefe "PyTorch-Installation"
Set-Content -Path $merker -Value $Beschleunigung -Encoding ASCII
# Die installierte torch-Version festhalten, damit spaetere Pakete sie nicht austauschen
$tvText = & $Py -c "import torch, torchvision; print(torch.__version__.split('+')[0], torchvision.__version__.split('+')[0])"
if ($LASTEXITCODE -ne 0 -or -not $tvText) {
    throw "PyTorch laesst sich nicht starten. Meist fehlt die 'Microsoft Visual C++ Redistributable' (x64) von microsoft.com. Danach install.bat erneut starten."
}
$tv = ([string]$tvText).Trim().Split(" ")
$zeilen = @("torch==$($tv[0])", "torchvision==$($tv[1])")
if ($Beschleunigung -eq "directml") { $zeilen += @("open_clip_torch<4", "transformers<4.50") }
Set-Content -Path $constraints -Value $zeilen -Encoding ASCII

Schritt "4/6  Programmpakete installieren"
$torchIndex = @()
if ($Beschleunigung -eq "nvidia") { $torchIndex = @("--extra-index-url", "https://download.pytorch.org/whl/cu126", "--index-strategy", "unsafe-best-match") }
& $uv pip install --python $Py -r (Join-Path $Root "requirements.txt") -c $constraints @torchIndex
Pruefe "Paketinstallation"

Schritt "5/6  exiftool laden"
$exDir = Join-Path $Umg "exiftool"
if (-not (Test-Path (Join-Path $exDir "exiftool.exe"))) {
    try {
        $ver = ExiftoolLaden $exDir
        Write-Host "exiftool $ver installiert."
    } catch {
        Write-Warning "exiftool konnte nicht geladen werden ($_). Das Programm laeuft trotzdem, nur ohne RAW-Vorschau und eingebettete Tags. Spaeter nachladen mit: exiftool-nachladen.bat"
    }
}

# Gewaehlte Beschleunigung als Voreinstellung merken (in der Oberflaeche aenderbar)
$einst = Join-Path $Daten "einstellungen.json"
if (-not (Test-Path $einst)) {
    Set-Content -Path $einst -Value ('{"beschleunigung": "' + $(if ($Beschleunigung -eq "cpu") { "cpu" } else { "auto" }) + '"}') -Encoding ASCII
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
Write-Host "Beschleunigung: $Beschleunigung" -ForegroundColor Green
Write-Host "Fertig! Starten mit  start.bat  (Doppelklick)." -ForegroundColor Green
