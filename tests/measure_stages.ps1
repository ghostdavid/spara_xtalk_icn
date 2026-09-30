param([string]$ExePath)

# Split onefile startup time into stages:
#   t_extract = launch -> %TEMP%\_MEI* extraction done (python*.dll present)
#   t_window  = launch -> main window title appears
$t0 = Get-Date
$null = Start-Process $ExePath
$t_extract = $null
$app = $null
while (((Get-Date) - $t0).TotalSeconds -lt 120) {
    Start-Sleep -Milliseconds 200
    if ($null -eq $t_extract) {
        $mei = Get-ChildItem $env:TEMP -Directory -Filter '_MEI*' -ErrorAction SilentlyContinue |
               Where-Object { Test-Path (Join-Path $_.FullName 'python*.dll') } |
               Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($mei) { $t_extract = ((Get-Date) - $t0).TotalSeconds }
    }
    $app = Get-Process | Where-Object { $_.MainWindowTitle -match 'PowerSum' } |
           Select-Object -First 1
    if ($app) { break }
}
$t_window = ((Get-Date) - $t0).TotalSeconds
Write-Output ("extract={0}s  window={1}s  import+gui={2}s" -f
    [math]::Round($t_extract,1), [math]::Round($t_window,1),
    [math]::Round($t_window - $t_extract,1))
if ($null -ne $app) { Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue }
