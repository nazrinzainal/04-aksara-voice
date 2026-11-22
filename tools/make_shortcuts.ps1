<#
  Creates the "Start Aksara Voice" and "Stop Aksara Voice" shortcuts in the project folder
  (and on the Desktop with -Desktop). Re-run this if the project folder is moved.

    powershell -ExecutionPolicy Bypass -File tools\make_shortcuts.ps1
    powershell -ExecutionPolicy Bypass -File tools\make_shortcuts.ps1 -Desktop
#>
param([switch] $Desktop)

$Root = Split-Path -Parent $PSScriptRoot
$Launcher = Join-Path $Root "tools\aksara-launcher.ps1"
$Icon = Join-Path $Root "tools\aksara.ico"
$PowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$Shell = New-Object -ComObject WScript.Shell

function New-StudioShortcut([string] $folder, [string] $name, [string] $action, [string] $description) {
  $path = Join-Path $folder "$name.lnk"
  $lnk = $Shell.CreateShortcut($path)
  $lnk.TargetPath = $PowerShell
  $lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Launcher`" -Action $action"
  $lnk.WorkingDirectory = $Root
  $lnk.IconLocation = "$Icon,0"
  $lnk.Description = $description
  $lnk.WindowStyle = 7  # minimized
  $lnk.Save()
  Write-Host "Created $path"
}

$targets = @($Root)
if ($Desktop) { $targets += [Environment]::GetFolderPath("Desktop") }
foreach ($folder in $targets) {
  New-StudioShortcut $folder "Start Aksara Voice" "start" "Start (or open) the Aksara Voice studio"
  New-StudioShortcut $folder "Stop Aksara Voice" "stop" "Stop the Aksara Voice server"
}
