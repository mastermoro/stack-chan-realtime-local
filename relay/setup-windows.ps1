[CmdletBinding()]
param(
    [switch]$WithHeadset,
    [switch]$WithoutFirmware,
    [string]$PythonPath
)

$ErrorActionPreference = "Stop"

$relayRoot = Split-Path -Parent $PSCommandPath
$venvPython = Join-Path $relayRoot ".venv\Scripts\python.exe"
$envPath = Join-Path $relayRoot ".env"
$envExamplePath = Join-Path $relayRoot ".env.example"
$stackchanRoot = Join-Path (Split-Path -Parent $relayRoot) "stackchan"
$secretsPath = Join-Path $stackchanRoot "include\secrets.hpp"
$secretsExamplePath = Join-Path $stackchanRoot "include\secrets.example.hpp"

if (-not $PythonPath) {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        try {
            $candidate = & $launcher.Source -3.11 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $candidate) {
                $PythonPath = $candidate | Select-Object -First 1
            }
        } catch {}
    }
}
if (-not $PythonPath) {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        $PythonPath = $python.Source
    }
}
if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath)) {
    throw "Python 3.11 or newer was not found. Install Python and run setup again."
}
$pythonVersionOk = & $PythonPath -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.11 or newer is required."
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "Creating Python virtual environment..."
    & $PythonPath -m venv (Join-Path $relayRoot ".venv")
    if ($LASTEXITCODE -ne 0) {
        throw "Python virtual environment creation failed with exit code $LASTEXITCODE."
    }
}

$extras = @("dev")
if ($WithHeadset) {
    $extras += "headset"
}
if (-not $WithoutFirmware) {
    $extras += "firmware"
}
$installTarget = ".[" + ($extras -join ",") + "]"
Write-Host "Installing Relay dependencies..."
Push-Location $relayRoot
try {
    & $venvPython -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) {
        throw "pip could not be prepared in relay/.venv (exit code $LASTEXITCODE)."
    }
    & $venvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "pip upgrade failed with exit code $LASTEXITCODE."
    }
    & $venvPython -m pip install -e $installTarget
    if ($LASTEXITCODE -ne 0) {
        throw "Relay dependency installation failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}

if (-not (Test-Path -LiteralPath $envPath)) {
    Copy-Item -LiteralPath $envExamplePath -Destination $envPath
    Write-Host "Created relay/.env from the example. Configure Foundry and device credentials."
} else {
    Write-Host "Keeping the existing relay/.env."
}

if (-not (Test-Path -LiteralPath $secretsPath)) {
    Copy-Item -LiteralPath $secretsExamplePath -Destination $secretsPath
    Write-Host "Created stackchan/include/secrets.hpp. Configure Wi-Fi and the device token."
} else {
    Write-Host "Keeping the existing stackchan/include/secrets.hpp."
}

Write-Host ""
Write-Host "Setup complete."
Write-Host "1. Edit relay/.env."
Write-Host "2. Run .\run-manager.ps1 and open http://127.0.0.1:8787"
Write-Host "3. Edit stackchan/include/secrets.hpp."
Write-Host "4. Deploy from stackchan with .\deploy-from-relay.ps1"
Write-Host "   The deployment script detects this PC's LAN address and generates the Relay endpoint."
Write-Host "Windows Firewall may ask for permission when the Relay first listens on the LAN."
