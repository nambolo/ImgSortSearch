param([string]$Liste)
# Legt die in der Liste stehenden Dateien in die Zwischenablage (wie Strg+C im Explorer).
$pfade = @(Get-Content -LiteralPath $Liste -Encoding UTF8 | Where-Object { $_ -ne '' })
Set-Clipboard -LiteralPath $pfade
