[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8787,

    [switch]$Restart,

    [switch]$Reload
)

$ErrorActionPreference = "Stop"

$relayRoot = Split-Path -Parent $PSCommandPath
$pythonPath = Join-Path $relayRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Relay virtual environment was not found. Create relay/.venv and install the project dependencies first."
}

function Find-ManagerProcessId {
    param([int]$ProcessId)

    $current = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    while ($null -ne $current) {
        if ($current.CommandLine -match "local_manager:app") {
            return $current.ProcessId
        }
        if ($current.ParentProcessId -eq 0) {
            break
        }
        $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($current.ParentProcessId)" -ErrorAction SilentlyContinue
    }
    return $null
}

if ($Restart) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $listener) {
        $managerProcessId = Find-ManagerProcessId -ProcessId $listener.OwningProcess
        if ($null -eq $managerProcessId) {
            throw "Port $Port is used by a process that is not this Relay Manager. Stop it manually or choose another port."
        }
        try {
            Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$Port/api/stop" | Out-Null
        } catch {
            Write-Warning "The managed Relay could not be stopped through the Manager API: $($_.Exception.Message)"
        }
        Write-Host "Stopping existing Relay Manager process $managerProcessId"
        Stop-Process -Id $managerProcessId
        $deadline = (Get-Date).AddSeconds(5)
        do {
            Start-Sleep -Milliseconds 100
            $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
                Select-Object -First 1
        } while ($null -ne $listener -and (Get-Date) -lt $deadline)
        if ($null -ne $listener) {
            throw "Relay Manager did not release port $Port within 5 seconds."
        }
    }
}

$uvicornArgs = @("-m", "uvicorn", "local_manager:app", "--host", "127.0.0.1", "--port", $Port)
if ($Reload) {
    $uvicornArgs += "--reload"
}

Write-Host "Opening Relay Manager at http://127.0.0.1:$Port"
& $pythonPath @uvicornArgs
exit $LASTEXITCODE
