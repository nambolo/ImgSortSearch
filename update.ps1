# ImgSortSearch - Programm aktualisieren (holt den neuesten Stand von GitHub)
# Nutzt ein vorhandenes git oder laedt ein portables git (MinGit) nach "umgebung\git".
# Die Ordner "umgebung" (Python, Pakete) und "daten" (Index, Einstellungen, Modelle)
# werden nicht angefasst. Danach werden neue oder geaenderte Pakete nachinstalliert.
param(
    [switch]$OhnePakete      # nur den Programmcode aktualisieren
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root     = Split-Path -Parent $MyInvocation.MyCommand.Path
$Umg      = Join-Path $Root "umgebung"
$RepoUrl  = "https://github.com/nambolo/ImgSortSearch.git"
$Branch   = "main"
$MinGitVersion = "2.54.0"

function Schritt($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Ende($code) {
    Write-Host ""
    Read-Host "Fertig. Enter zum Schliessen" | Out-Null
    exit $code
}

try {
    # Laeuft das Programm noch? Dann koennen Dateien nicht sauber ersetzt werden.
    $py = Join-Path $Umg "venv\Scripts"
    $laeuft = @(Get-Process python, pythonw -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($py, [StringComparison]::OrdinalIgnoreCase) })
    if ($laeuft.Count) {
        Write-Warning "ImgSortSearch laeuft noch. Bitte das Programmfenster schliessen."
        Read-Host "Danach Enter druecken" | Out-Null
    }

    Schritt "1/3  git suchen"
    $git = $null
    $sys = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($sys) { $git = $sys.Source }
    $portabel = Join-Path $Umg "git\cmd\git.exe"
    if (-not $git -and (Test-Path $portabel)) { $git = $portabel }
    if (-not $git) {
        Write-Host "Kein git gefunden. Lade portables git (MinGit $MinGitVersion) nach umgebung\git ..."
        New-Item -ItemType Directory -Force -Path $Umg | Out-Null
        $zip = Join-Path $Umg "mingit.zip"
        $url = "https://github.com/git-for-windows/git/releases/download/v$MinGitVersion.windows.1/MinGit-$MinGitVersion-64-bit.zip"
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        Expand-Archive -Path $zip -DestinationPath (Join-Path $Umg "git") -Force
        Remove-Item $zip
        $git = $portabel
    }
    Write-Host "git: $git"

    Schritt "2/3  Neuesten Stand holen"
    Push-Location $Root
    try {
        if (-not (Test-Path (Join-Path $Root ".git"))) {
            # Ordner stammt aus dem ZIP-Download: einmalig in ein git-Verzeichnis verwandeln
            Write-Host "Ordner ist noch nicht mit GitHub verbunden, richte das einmalig ein."
            & $git init -q
            if ($LASTEXITCODE -ne 0) { throw "git init fehlgeschlagen" }
            & $git remote add origin $RepoUrl
        }
        & $git config core.autocrlf false     # Dateien genau so wie auf GitHub (.bat mit Windows-Zeilenenden)
        & $git remote set-url origin $RepoUrl
        $vorher = [string](& $git rev-parse --verify -q HEAD)   # leer, wenn frisch eingerichtet
        & $git fetch -q origin $Branch
        if ($LASTEXITCODE -ne 0) { throw "Download von GitHub fehlgeschlagen (Internet?)" }
        $neu = ([string](& $git rev-parse FETCH_HEAD)).Trim()
        $sauber = -not [string](& $git status --porcelain --untracked-files=no)
        if ($vorher.Trim() -eq $neu -and $sauber) {
            Write-Host "Schon auf dem neuesten Stand." -ForegroundColor Green
        } else {
            # Programmdateien auf den GitHub-Stand setzen (wie git pull, aber ohne Konflikte).
            # "umgebung" und "daten" stehen in .gitignore und bleiben unberuehrt.
            & $git reset -q --hard FETCH_HEAD
            if ($LASTEXITCODE -ne 0) { throw "Aktualisieren fehlgeschlagen" }
            & $git branch -M $Branch
            & $git branch -q --set-upstream-to "origin/$Branch" $Branch
            Write-Host "Aktualisiert auf:" -ForegroundColor Green
            & $git log -1 --format="  %h  %cd  %s" --date=format:"%d.%m.%Y %H:%M"
        }
    } finally { Pop-Location }

    Schritt "3/3  Pakete nachziehen"
    $pyExe = Join-Path $Umg "venv\Scripts\python.exe"
    if ($OhnePakete) {
        Write-Host "Uebersprungen."
    } elseif (-not (Test-Path $pyExe)) {
        Write-Host "Noch nicht installiert. Bitte jetzt install.bat ausfuehren."
    } else {
        # install.ps1 ist wiederholbar: Vorhandenes bleibt, nur Neues/Geaendertes wird geladen
        $merker = Join-Path $Umg "beschleunigung.txt"
        $args2 = @("-OhneModelle")
        if (Test-Path $merker) { $args2 += @("-Beschleunigung", (Get-Content $merker -Raw).Trim()) }
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "install.ps1") @args2
        if ($LASTEXITCODE -ne 0) { throw "Pakete konnten nicht aktualisiert werden" }
    }
    Write-Host ""
    Write-Host "Update abgeschlossen. Starten mit start.bat." -ForegroundColor Green
    Ende 0
} catch {
    Write-Host ""
    Write-Host "FEHLER: $_" -ForegroundColor Red
    Ende 1
}
