param([string]$ExePath, [string]$Label)

# NOTE: judge by any non-empty MainWindowTitle (ASCII-only matching is
# unreliable because the process has two windows: main + plot, and
# MainWindowTitle may return either one).
$t0 = Get-Date
$null = Start-Process $ExePath
$app = $null
while (((Get-Date) - $t0).TotalSeconds -lt 90) {
    Start-Sleep -Milliseconds 300
    $app = Get-Process | Where-Object { $_.Path -like '*PowerSum*' -and $_.MainWindowTitle -ne '' } |
           Select-Object -First 1
    if ($app) { break }
}
$dt = ((Get-Date) - $t0).TotalSeconds
Start-Sleep -Seconds 5
$alive = ($null -ne $app) -and (-not $app.HasExited)
Write-Output ("{0}: window={1}s alive_after_5s={2}" -f $Label, [math]::Round($dt,1), $alive)
if ($null -ne $app) { Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue }
