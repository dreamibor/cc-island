Start-Transcript -Path "C:\Users\ZHAOYU\AppData\Local\Temp\ccisland_selftest\bt_reset.log" -Force
Write-Host "=== restarting bthserv ==="
Restart-Service bthserv -Force
Start-Sleep -Seconds 3
Write-Host "=== cycling Bluetooth radios ==="
Get-PnpDevice -Class Bluetooth -Status OK -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host ("  disable: " + $_.FriendlyName)
    Disable-PnpDevice -InstanceId $_.InstanceId -Confirm:$false -ErrorAction Continue
}
Start-Sleep -Seconds 3
Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host ("  enable: " + $_.FriendlyName)
    Enable-PnpDevice -InstanceId $_.InstanceId -Confirm:$false -ErrorAction Continue
}
Start-Sleep -Seconds 4
Write-Host "=== done ==="
Stop-Transcript
