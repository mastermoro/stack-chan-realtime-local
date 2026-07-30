[CmdletBinding()]
param(
    [ValidateSet("127.0.0.1", "0.0.0.0")]
    [string]$ListenAddress = "0.0.0.0",

    [ValidateRange(1, 65535)]
    [int]$Port = 8080,

    [switch]$Reload
)

$ErrorActionPreference = "Stop"

$relayRoot = Split-Path -Parent $PSCommandPath
$pythonPath = Join-Path $relayRoot ".venv\Scripts\python.exe"
$envPath = Join-Path $relayRoot ".env"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Relay virtual environment was not found. Create relay/.venv and install the project dependencies first."
}

if (-not (Test-Path -LiteralPath $envPath)) {
    throw "relay/.env was not found. Copy .env.example to .env and configure it first."
}

$endpointLine = Get-Content -LiteralPath $envPath | Where-Object {
    $_ -match "^AZURE_OPENAI_ENDPOINT="
} | Select-Object -First 1
$endpoint = if ($endpointLine) { $endpointLine.Substring("AZURE_OPENAI_ENDPOINT=".Length) } else { "" }

if (-not $endpoint -or $endpoint -match "YOUR-RESOURCE|example\.openai\.azure\.com") {
    Write-Warning "Foundry is not configured. HTTP health checks will work, but a device session will not connect until relay/.env contains a real Azure OpenAI endpoint and credentials are available."
}

$uvicornArgs = @("-m", "uvicorn", "app.main:app", "--host", $ListenAddress, "--port", $Port)
if ($Reload) {
    $uvicornArgs += "--reload"
}

Write-Host "Starting Relay at http://$($ListenAddress):$Port"
& $pythonPath @uvicornArgs
exit $LASTEXITCODE
