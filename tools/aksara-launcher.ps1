<#
  Aksara Voice launcher — used by the "Start Aksara Voice" / "Stop Aksara Voice" shortcuts.

    start    open the studio; starts the server if needed. If an OLDER version of Aksara Voice is
             still running (code was updated), it is restarted automatically.
    stop     stop the Aksara Voice server on the port.
    restart  stop + start.

  Only a process that is listening on the studio port AND answers /api/health as Aksara Voice is ever
  stopped, so other Python programs are never touched. Works in Windows PowerShell 5.1 and 7.
#>
param(
  [ValidateSet("start", "stop", "restart")] [string] $Action = "start",
  [int] $Port = 7788,
  [switch] $NoBrowser,
  [switch] $Quiet   # no message boxes (for tests)
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$LogDir = Join-Path $Root "logs"
$Url = "http://127.0.0.1:$Port/"

function Show-Message([string] $text, [string] $icon = "Information") {
  Write-Host $text
  if ($Quiet) { return }
  try {
    Add-Type -AssemblyName System.Windows.Forms
    [void][System.Windows.Forms.MessageBox]::Show($text, "Aksara Voice", "OK", $icon)
  } catch { }
}

function Get-CodeVersion {
  $m = Select-String -Path (Join-Path $Root "app.py") -Pattern '^VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
  if ($m) { return $m.Matches[0].Groups[1].Value }
  return ""
}

function Get-Health {
  try { return Invoke-RestMethod -Uri ($Url + "api/health") -TimeoutSec 3 } catch { return $null }
}

function Get-PortOwner {
  $c = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($c) { return [int]$c.OwningProcess }
  return $null
}

function Stop-Studio {
  $health = Get-Health
  $owner = Get-PortOwner
  if (-not $owner) { Write-Host "Aksara Voice is not running on port $Port."; return $true }
  if (-not $health -or -not $health.ok) {
    Show-Message "Port $Port is used by another program (PID $owner), not Aksara Voice. Nothing was stopped." "Warning"
    return $false
  }
  $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$owner"
  $parent = $null
  if ($proc -and $proc.ParentProcessId) {
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($proc.ParentProcessId)"
  }
  Write-Host "Stopping Aksara Voice $($health.version) (PID $owner)..."
  Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue
  # The .venv python.exe is a small launcher that starts the real interpreter as a child.
  if ($parent -and $parent.Name -eq "python.exe" -and $parent.CommandLine -match "app\.py") {
    Stop-Process -Id $parent.ProcessId -Force -ErrorAction SilentlyContinue
  }
  for ($i = 0; $i -lt 40; $i++) {
    if (-not (Get-PortOwner)) { Write-Host "Stopped."; return $true }
    Start-Sleep -Milliseconds 250
  }
  Show-Message "Aksara Voice did not stop in time. Close its window manually." "Warning"
  return $false
}

function Start-Studio {
  if (-not (Test-Path $Py)) {
    Show-Message "The project environment (.venv) was not found.`nSee 'Setup' in README.md." "Error"
    exit 1
  }
  $want = Get-CodeVersion
  $health = Get-Health
  if ($health -and $health.ok) {
    if ($want -and $health.version -ne $want) {
      Write-Host "Running version $($health.version) is older than the code ($want); restarting."
      if (-not (Stop-Studio)) { exit 1 }
    } else {
      Write-Host "Aksara Voice $($health.version) is already running."
      if (-not $NoBrowser) { Start-Process $Url }
      return
    }
  } elseif (Get-PortOwner) {
    Show-Message "Port $Port is used by another program. Close it, or start Aksara Voice on another port:`n.venv\Scripts\python.exe app.py --port 7790" "Warning"
    exit 1
  }

  New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
  $suffix = ""
  if ($Port -ne 7788) { $suffix = "-$Port" }
  $env:PYTHONUNBUFFERED = "1"
  Start-Process -FilePath $Py -ArgumentList @("app.py", "--no-browser", "--port", "$Port") `
    -WorkingDirectory $Root -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $LogDir "server$suffix.log") `
    -RedirectStandardError (Join-Path $LogDir "server$suffix.err.log") | Out-Null

  for ($i = 0; $i -lt 120; $i++) {   # up to 60 s
    Start-Sleep -Milliseconds 500
    $health = Get-Health
    if ($health -and $health.ok) {
      Write-Host "Aksara Voice $($health.version) started at $Url (Chatterbox keeps loading in the background)."
      if (-not $NoBrowser) { Start-Process $Url }
      return
    }
  }
  Show-Message "Aksara Voice did not start. See logs\server.err.log in the project folder." "Error"
  exit 1
}

switch ($Action) {
  "start"   { Start-Studio }
  "stop"    { if (-not (Stop-Studio)) { exit 1 } }
  "restart" { if (Stop-Studio) { Start-Studio } else { exit 1 } }
}
