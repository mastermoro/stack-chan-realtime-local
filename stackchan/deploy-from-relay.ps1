[CmdletBinding()]
param(
    [string]$RelayAddress,
    [string]$InterfaceAlias,
    [string]$StackChanAddress,
    [ValidateRange(1, 65535)]
    [int]$RelayPort = 8080,
    [string]$RelayPath = "/v1/realtime",
    [switch]$UseTls,
    [string]$UploadPort,
    [switch]$BuildOnly,
    [switch]$SkipRelayCheck,
    [switch]$ResolveOnly
)

$ErrorActionPreference = "Stop"

function Test-UsableIPv4 {
    param([Parameter(Mandatory)][string]$Address)

    $parsed = $null
    if (-not [System.Net.IPAddress]::TryParse($Address, [ref]$parsed)) {
        return $false
    }

    if ($parsed.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
        return $false
    }

    $bytes = $parsed.GetAddressBytes()
    return $Address -ne "0.0.0.0" -and
        $Address -ne "127.0.0.1" -and
        $bytes[0] -ne 127 -and
        -not ($bytes[0] -eq 169 -and $bytes[1] -eq 254) -and
        $bytes[0] -lt 224
}

function Get-RouteAddressForTarget {
    param([Parameter(Mandatory)][string]$TargetAddress)

    if (-not (Test-UsableIPv4 $TargetAddress)) {
        throw "StackChanAddress must be a usable IPv4 address."
    }

    $socket = [System.Net.Sockets.Socket]::new(
        [System.Net.Sockets.AddressFamily]::InterNetwork,
        [System.Net.Sockets.SocketType]::Dgram,
        [System.Net.Sockets.ProtocolType]::Udp
    )
    try {
        $socket.Connect($TargetAddress, 9)
        return ([System.Net.IPEndPoint]$socket.LocalEndPoint).Address.ToString()
    } finally {
        $socket.Dispose()
    }
}

function Get-InterfaceCandidates {
    param([string]$RequestedAlias)

    $interfaces = [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()
    $candidates = foreach ($networkInterface in $interfaces) {
        if ($networkInterface.OperationalStatus -ne
            [System.Net.NetworkInformation.OperationalStatus]::Up) {
            continue
        }

        if ($RequestedAlias -and
            $networkInterface.Name -ine $RequestedAlias -and
            $networkInterface.Description -ine $RequestedAlias) {
            continue
        }

        try {
            $properties = $networkInterface.GetIPProperties()
            $ipv4Properties = $properties.GetIPv4Properties()
        } catch {
            continue
        }

        $hasGateway = @($properties.GatewayAddresses | Where-Object {
            $_.Address.AddressFamily -eq
                [System.Net.Sockets.AddressFamily]::InterNetwork -and
            $_.Address.ToString() -ne "0.0.0.0"
        }).Count -gt 0

        $isPhysicalType = $networkInterface.NetworkInterfaceType -in @(
            [System.Net.NetworkInformation.NetworkInterfaceType]::Ethernet,
            [System.Net.NetworkInformation.NetworkInterfaceType]::Wireless80211,
            [System.Net.NetworkInformation.NetworkInterfaceType]::GigabitEthernet,
            [System.Net.NetworkInformation.NetworkInterfaceType]::FastEthernetFx,
            [System.Net.NetworkInformation.NetworkInterfaceType]::FastEthernetT
        )

        foreach ($unicast in $properties.UnicastAddresses) {
            $address = $unicast.Address.ToString()
            if (-not (Test-UsableIPv4 $address)) {
                continue
            }
            if (-not $RequestedAlias -and -not $hasGateway) {
                continue
            }

            [pscustomobject]@{
                Address = $address
                InterfaceAlias = $networkInterface.Name
                InterfaceIndex = $ipv4Properties.Index
                IsPhysicalType = $isPhysicalType
                HasGateway = $hasGateway
                Speed = $networkInterface.Speed
            }
        }
    }

    return @($candidates | Sort-Object @{ Expression = "IsPhysicalType"; Descending = $true },
        @{ Expression = "HasGateway"; Descending = $true },
        @{ Expression = "Speed"; Descending = $true },
        InterfaceIndex, Address)
}

function Resolve-RelayAddress {
    if ($RelayAddress) {
        if (-not (Test-UsableIPv4 $RelayAddress)) {
            throw "RelayAddress must be a usable IPv4 address."
        }
        return [pscustomobject]@{
            Address = $RelayAddress
            InterfaceAlias = "explicit"
            Selection = "RelayAddress"
        }
    }

    if ($StackChanAddress) {
        $address = Get-RouteAddressForTarget $StackChanAddress
        return [pscustomobject]@{
            Address = $address
            InterfaceAlias = "route to $StackChanAddress"
            Selection = "StackChanAddress"
        }
    }

    $candidates = @(Get-InterfaceCandidates $InterfaceAlias)
    if ($candidates.Count -eq 0) {
        $hint = if ($InterfaceAlias) {
            "No usable IPv4 address was found on '$InterfaceAlias'."
        } else {
            "No active LAN IPv4 address with a default route was found."
        }
        throw "$hint Use -RelayAddress, -InterfaceAlias, or -StackChanAddress."
    }

    if (-not $InterfaceAlias) {
        $defaultAddress = $null
        try {
            $defaultAddress = Get-RouteAddressForTarget "1.1.1.1"
        } catch {
            # An offline PC can still deploy over a LAN selected below.
        }

        $defaultCandidate = $candidates |
            Where-Object { $_.Address -eq $defaultAddress -and $_.IsPhysicalType } |
            Select-Object -First 1
        if ($defaultCandidate) {
            $candidates = @($defaultCandidate)
        } else {
            $physicalCandidates = @($candidates | Where-Object { $_.IsPhysicalType })
            $physicalInterfaces = @($physicalCandidates |
                Select-Object -ExpandProperty InterfaceIndex -Unique)
            if ($physicalInterfaces.Count -eq 1) {
                $candidates = @($physicalCandidates)
            } elseif ($physicalInterfaces.Count -gt 1) {
                $choices = ($physicalCandidates | Select-Object -First 6 |
                    ForEach-Object { "$($_.InterfaceAlias)=$($_.Address)" }) -join ", "
                throw "Multiple active LAN interfaces were found: $choices. Use -InterfaceAlias, -StackChanAddress, or -RelayAddress."
            }
        }
    }

    return [pscustomobject]@{
        Address = $candidates[0].Address
        InterfaceAlias = $candidates[0].InterfaceAlias
        Selection = if ($InterfaceAlias) { "InterfaceAlias" } else { "default route" }
    }
}

if (-not $RelayPath.StartsWith("/") -or $RelayPath.Contains('"') -or
    $RelayPath.Contains("`r") -or $RelayPath.Contains("`n")) {
    throw "RelayPath must be a single URL path beginning with '/'."
}

$resolved = Resolve-RelayAddress
$scheme = if ($UseTls) { "wss" } else { "ws" }
$httpScheme = if ($UseTls) { "https" } else { "http" }

$result = [pscustomobject]@{
    RelayAddress = $resolved.Address
    InterfaceAlias = $resolved.InterfaceAlias
    Selection = $resolved.Selection
    RelayUrl = "${scheme}://$($resolved.Address):${RelayPort}${RelayPath}"
}

if ($ResolveOnly) {
    $result | ConvertTo-Json
    return
}

$stackchanRoot = Split-Path -Parent $PSCommandPath
$includeRoot = Join-Path $stackchanRoot "include"
$secretsPath = Join-Path $includeRoot "secrets.hpp"
$secretsExamplePath = Join-Path $includeRoot "secrets.example.hpp"
$endpointPath = Join-Path $includeRoot "relay_endpoint.hpp"

if (-not (Test-Path -LiteralPath $secretsPath)) {
    Copy-Item -LiteralPath $secretsExamplePath -Destination $secretsPath
    throw "Created stackchan/include/secrets.hpp. Set Wi-Fi credentials and DEVICE_TOKEN, then run this command again."
}

$secrets = Get-Content -LiteralPath $secretsPath -Raw
if ($secrets -match "YOUR_WIFI_(SSID|PASSWORD)" -or $secrets -match '"replace-me"') {
    throw "stackchan/include/secrets.hpp still contains example credentials. Configure it before deployment."
}

$tlsValue = if ($UseTls) { 1 } else { 0 }
$header = @"
#pragma once

// Generated by deploy-from-relay.ps1. Do not edit or commit this file.
#define RELAY_HOST "$($resolved.Address)"
#define RELAY_PORT $RelayPort
#define RELAY_PATH "$RelayPath"
#define RELAY_USE_TLS $tlsValue
"@
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText($endpointPath, $header + [Environment]::NewLine, $utf8NoBom)

Write-Host "Relay endpoint: $($result.RelayUrl)"
Write-Host "Selected from: $($resolved.Selection) ($($resolved.InterfaceAlias))"
Write-Host "Generated: stackchan/include/relay_endpoint.hpp"

if (-not $SkipRelayCheck) {
    $healthUrl = "${httpScheme}://$($resolved.Address):${RelayPort}/healthz"
    try {
        $null = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3
        Write-Host "Relay health check passed: $healthUrl"
    } catch {
        throw "Relay health check failed at $healthUrl. Start the local Relay, verify Windows Firewall, or use -SkipRelayCheck."
    }
}

$pio = Get-Command pio -ErrorAction SilentlyContinue
if (-not $pio) {
    $bundledPio = Join-Path (Split-Path -Parent $stackchanRoot) "relay\.venv\Scripts\pio.exe"
    if (Test-Path -LiteralPath $bundledPio) {
        $pio = Get-Item -LiteralPath $bundledPio
    }
}
if (-not $pio) {
    throw "PlatformIO command 'pio' was not found. Run setup.cmd to install it."
}

Push-Location $stackchanRoot
try {
    & $pio.Source run
    if ($LASTEXITCODE -ne 0) {
        throw "PlatformIO build failed with exit code $LASTEXITCODE."
    }

    if (-not $BuildOnly) {
        $managerUrl = "http://127.0.0.1:8787"
        $usbBridgeWasRunning = $false
        try {
            $usbStatus = Invoke-RestMethod -Uri "$managerUrl/api/usb/status" -TimeoutSec 2
            $usbBridgeWasRunning = [bool]$usbStatus.running
            if ($usbBridgeWasRunning) {
                $null = Invoke-RestMethod -Method Post -Uri "$managerUrl/api/usb/stop" -TimeoutSec 5
                Write-Host "USB bridge stopped temporarily for firmware upload."
            }
        } catch {
            Write-Verbose "Relay Manager USB bridge status was unavailable: $($_.Exception.Message)"
        }

        $uploadArguments = @("run", "-t", "upload")
        if ($UploadPort) {
            $uploadArguments += @("--upload-port", $UploadPort)
        }
        try {
            & $pio.Source @uploadArguments
            if ($LASTEXITCODE -ne 0) {
                throw "PlatformIO upload failed with exit code $LASTEXITCODE."
            }
            Write-Host "Stack-chan deployment completed."
        } finally {
            if ($usbBridgeWasRunning) {
                try {
                    $null = Invoke-RestMethod -Method Post -Uri "$managerUrl/api/usb/start" -TimeoutSec 5
                    Write-Host "USB bridge restarted after firmware upload."
                } catch {
                    Write-Warning "USB bridge could not be restarted automatically: $($_.Exception.Message)"
                }
            }
        }
    } else {
        Write-Host "Firmware build completed; upload was skipped."
    }
} finally {
    Pop-Location
}
