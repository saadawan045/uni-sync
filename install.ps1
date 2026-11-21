# uni-sync installer for Windows 10/11. No admin needed.
# In PowerShell, from the uni-sync folder:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
$ErrorActionPreference = "Stop"
$Dir = $PSScriptRoot
$Model = if ($env:UNISYNC_MODEL) { $env:UNISYNC_MODEL } else { "qwen2.5:3b" }

function Step($t) { Write-Host "`n==> $t" -ForegroundColor Cyan }
function Refresh-Path {
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
function Need($cmd, $wingetId, $what) {
  if (Get-Command $cmd -ErrorAction SilentlyContinue) { return }
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "$what isn't installed and winget isn't available. Install $what yourself, then run this again."
  }
  Write-Host "Installing $what..."
  winget install --id $wingetId -e --silent --accept-package-agreements --accept-source-agreements --scope user
  if ($LASTEXITCODE -ne 0) { winget install --id $wingetId -e --silent --accept-package-agreements --accept-source-agreements }
  Refresh-Path
  if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
    throw "$what was installed but isn't on PATH yet. Close this window, open a new PowerShell and run the installer again."
  }
}

Step "Checking Python, Node.js and Ollama (installs any that are missing)"
$py = $null
foreach ($c in @("py -3.13", "py -3.12", "py -3.11", "python")) {
  $exe, $arg = $c.Split(" ", 2)
  if (Get-Command $exe -ErrorAction SilentlyContinue) {
    $v = & $exe $arg -c "import sys; print(sys.version_info >= (3, 11))" 2>$null
    if ($v -eq "True") { $py = $c; break }
  }
}
if (-not $py) {
  Need "py" "Python.Python.3.12" "Python 3.12"
  $py = "py -3.12"
}
Need "node" "OpenJS.NodeJS.LTS" "Node.js (for WhatsApp)"
Need "ollama" "Ollama.Ollama" "Ollama (the local AI)"

Step "Python environment"
$exe, $arg = $py.Split(" ", 2)
if (-not (Test-Path "$Dir\.venv\Scripts\python.exe")) { & $exe $arg -m venv "$Dir\.venv" }
$vpy = "$Dir\.venv\Scripts\python.exe"
& $vpy -m pip install -q --upgrade pip
& $vpy -m pip install -q -r "$Dir\requirements.txt"

Step "Hidden browser for the student portal"
& $vpy -m playwright install chromium

Step "WhatsApp bridge"
Push-Location "$Dir\wa-bridge"; npm ci --omit=dev --no-audit --no-fund; Pop-Location

Step "Local AI model ($Model, about 2 GB; runs only on this laptop)"
try { ollama pull $Model } catch { Write-Host "Couldn't download the model now. Open the Ollama app once, then run: ollama pull $Model" -ForegroundColor Yellow }

Step "The uni-sync command"
$bin = "$env:LOCALAPPDATA\uni-sync\bin"
New-Item -ItemType Directory -Force $bin | Out-Null
Set-Content -Encoding ASCII "$bin\uni-sync.cmd" "@echo off`r`nset PYTHONPATH=$Dir`r`n`"$vpy`" -P -X utf8 -m unisync %*"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not ($userPath -split ";" | Where-Object { $_ -eq $bin })) {
  [Environment]::SetEnvironmentVariable("Path", ($userPath.TrimEnd(";") + ";" + $bin), "User")
}
Refresh-Path

if ($env:UNISYNC_SKIP_SETUP) {
  Write-Host "`nInstalled. Run 'uni-sync setup' in a new window to finish." -ForegroundColor Green
} else {
  Write-Host "`nInstalled. Starting setup..." -ForegroundColor Green
  & "$bin\uni-sync.cmd" setup
}
