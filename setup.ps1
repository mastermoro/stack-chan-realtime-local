[CmdletBinding()]
param(
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$repoRoot = Split-Path -Parent $PSCommandPath
$relayRoot = Join-Path $repoRoot "relay"
$envPath = Join-Path $relayRoot ".env"
$envExamplePath = Join-Path $relayRoot ".env.example"
$secretsPath = Join-Path $repoRoot "stackchan\include\secrets.hpp"
$secretsExamplePath = Join-Path $repoRoot "stackchan\include\secrets.example.hpp"
$venvPython = Join-Path $relayRoot ".venv\Scripts\python.exe"
$venvPio = Join-Path $relayRoot ".venv\Scripts\pio.exe"

function Get-PythonPath {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        try {
            $candidate = & $launcher.Source -3.11 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $candidate) {
                return ($candidate | Select-Object -First 1)
            }
        } catch {}
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        try {
            & $python.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) {
                return $python.Source
            }
        } catch {}
    }
    return $null
}

function Get-CommandVersion {
    param([string]$Command, [string[]]$Arguments)
    $resolved = Get-Command $Command -ErrorAction SilentlyContinue
    if (-not $resolved) {
        return $null
    }
    try {
        $output = & $resolved.Source @Arguments 2>$null
        if ($LASTEXITCODE -eq 0) {
            return ($output | Select-Object -First 1)
        }
    } catch {
        return $resolved.Source
    }
    return $resolved.Source
}

function Read-DotEnv {
    $values = @{}
    $sourcePath = if (Test-Path -LiteralPath $envPath) { $envPath } else { $envExamplePath }
    foreach ($line in Get-Content -LiteralPath $sourcePath) {
        if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') {
            $values[$matches[1]] = $matches[2]
        }
    }
    return $values
}

function Write-DotEnv {
    param([hashtable]$Values)
    if (-not (Test-Path -LiteralPath $envPath)) {
        Copy-Item -LiteralPath $envExamplePath -Destination $envPath
    }
    $lines = [System.Collections.Generic.List[string]]::new()
    foreach ($line in Get-Content -LiteralPath $envPath) {
        $replaced = $false
        foreach ($key in @($Values.Keys)) {
            if ($line -match "^$([regex]::Escape($key))=") {
                $lines.Add("$key=$($Values[$key])")
                $Values.Remove($key)
                $replaced = $true
                break
            }
        }
        if (-not $replaced) {
            $lines.Add($line)
        }
    }
    foreach ($key in $Values.Keys) {
        $lines.Add("$key=$($Values[$key])")
    }
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllLines($envPath, $lines, $utf8NoBom)
}

function ConvertTo-CppString {
    param([string]$Value)
    return $Value.Replace('\', '\\').Replace('"', '\"')
}

function ConvertTo-ProcessArgument {
    param([string]$Value)
    return '"' + $Value.Replace('"', '\"') + '"'
}

function New-Label {
    param([string]$Text, [int]$X, [int]$Y, [int]$Width = 180, [int]$Height = 24)
    $label = [System.Windows.Forms.Label]::new()
    $label.Text = $Text
    $label.Location = [System.Drawing.Point]::new($X, $Y)
    $label.Size = [System.Drawing.Size]::new($Width, $Height)
    return $label
}

function New-TextBox {
    param([int]$X, [int]$Y, [int]$Width = 500, [switch]$Secret)
    $box = [System.Windows.Forms.TextBox]::new()
    $box.Location = [System.Drawing.Point]::new($X, $Y)
    $box.Size = [System.Drawing.Size]::new($Width, 27)
    $box.UseSystemPasswordChar = $Secret
    return $box
}

$form = [System.Windows.Forms.Form]::new()
$form.Text = "Stack-chan Realtime セットアップ"
$form.StartPosition = "CenterScreen"
$form.Size = [System.Drawing.Size]::new(850, 810)
$form.MinimumSize = [System.Drawing.Size]::new(850, 810)
$form.BackColor = [System.Drawing.Color]::FromArgb(245, 247, 250)
$form.Font = [System.Drawing.Font]::new("Yu Gothic UI", 10)

$title = New-Label "Stack-chan Realtime セットアップ" 28 20 760 40
$title.Font = [System.Drawing.Font]::new("Yu Gothic UI", 20, [System.Drawing.FontStyle]::Bold)
$form.Controls.Add($title)
$intro = New-Label "必要なプログラムと接続情報をまとめて準備します。設定ファイルはGitの管理対象外です。" 31 65 760 28
$intro.ForeColor = [System.Drawing.Color]::FromArgb(80, 92, 110)
$form.Controls.Add($intro)

$tabs = [System.Windows.Forms.TabControl]::new()
$tabs.Location = [System.Drawing.Point]::new(25, 105)
$tabs.Size = [System.Drawing.Size]::new(785, 570)
$form.Controls.Add($tabs)

$setupTab = [System.Windows.Forms.TabPage]::new("1. プログラム")
$configTab = [System.Windows.Forms.TabPage]::new("2. 接続設定")
$deviceTab = [System.Windows.Forms.TabPage]::new("3. Stack-chan")
$tabs.TabPages.AddRange(@($setupTab, $configTab, $deviceTab))

$statusLabels = @{}
$statusNames = [ordered]@{
    Python = "Python 3.11 以上"
    Relay = "Relay Python パッケージ"
    PlatformIO = "PlatformIO（ファームウェア）"
    Git = "Git"
    AzureCLI = "Azure CLI（APIキーを使わない場合）"
}
$y = 30
foreach ($entry in $statusNames.GetEnumerator()) {
    $setupTab.Controls.Add((New-Label $entry.Value 30 $y 350))
    $valueLabel = New-Label "確認中..." 395 $y 320
    $statusLabels[$entry.Key] = $valueLabel
    $setupTab.Controls.Add($valueLabel)
    $y += 46
}

$refreshButton = [System.Windows.Forms.Button]::new()
$refreshButton.Text = "状態を更新"
$refreshButton.Location = [System.Drawing.Point]::new(30, 280)
$refreshButton.Size = [System.Drawing.Size]::new(150, 38)
$setupTab.Controls.Add($refreshButton)

$installButton = [System.Windows.Forms.Button]::new()
$installButton.Text = "必要項目をインストール"
$installButton.Location = [System.Drawing.Point]::new(195, 280)
$installButton.Size = [System.Drawing.Size]::new(220, 38)
$installButton.BackColor = [System.Drawing.Color]::FromArgb(44, 125, 220)
$installButton.ForeColor = [System.Drawing.Color]::White
$installButton.FlatStyle = "Flat"
$setupTab.Controls.Add($installButton)

$headsetCheck = [System.Windows.Forms.CheckBox]::new()
$headsetCheck.Text = "PCヘッドセット・テストもインストール"
$headsetCheck.Checked = $true
$headsetCheck.Location = [System.Drawing.Point]::new(30, 335)
$headsetCheck.Size = [System.Drawing.Size]::new(380, 28)
$setupTab.Controls.Add($headsetCheck)

$installNote = New-Label "Pythonがない場合は winget を使ってユーザー領域へ導入します。Azure CLIは認証方法として任意です。" 30 380 700 50
$installNote.ForeColor = [System.Drawing.Color]::FromArgb(80, 92, 110)
$setupTab.Controls.Add($installNote)

$installLog = [System.Windows.Forms.TextBox]::new()
$installLog.Location = [System.Drawing.Point]::new(30, 430)
$installLog.Size = [System.Drawing.Size]::new(700, 82)
$installLog.Multiline = $true
$installLog.ScrollBars = "Vertical"
$installLog.ReadOnly = $true
$installLog.BackColor = [System.Drawing.Color]::White
$setupTab.Controls.Add($installLog)

$endpointBox = New-TextBox 235 34
$realtimeBox = New-TextBox 235 82
$responsesBox = New-TextBox 235 130
$apiKeyBox = New-TextBox 235 178 -Secret
$countryBox = New-TextBox 235 226 120
$timezoneBox = New-TextBox 235 274 250
$browserCheck = [System.Windows.Forms.CheckBox]::new()
$browserCheck.Text = "音声によるブラウザ起動を有効にする"
$browserCheck.Location = [System.Drawing.Point]::new(235, 322)
$browserCheck.Size = [System.Drawing.Size]::new(400, 28)
$domainsBox = New-TextBox 235 366

$configFields = @(
    @("Foundry エンドポイント *", $endpointBox, 34),
    @("Realtime デプロイ名 *", $realtimeBox, 82),
    @("Responses デプロイ名 *", $responsesBox, 130),
    @("APIキー（任意）", $apiKeyBox, 178),
    @("Web検索の国", $countryBox, 226),
    @("タイムゾーン", $timezoneBox, 274),
    @("ブラウザ許可ドメイン", $domainsBox, 366)
)
foreach ($field in $configFields) {
    $configTab.Controls.Add((New-Label $field[0] 28 $field[2] 195))
    $configTab.Controls.Add($field[1])
}
$configTab.Controls.Add($browserCheck)
$authNote = New-Label "APIキーを空欄にする場合は、Azure CLIで az login 済みの資格情報を利用します。" 235 210 500 22
$authNote.ForeColor = [System.Drawing.Color]::FromArgb(80, 92, 110)
$configTab.Controls.Add($authNote)
$domainNote = New-Label "カンマ区切り。空欄では任意のHTTP(S)ホストを許可します。" 235 397 500 22
$domainNote.ForeColor = [System.Drawing.Color]::FromArgb(80, 92, 110)
$configTab.Controls.Add($domainNote)

$deviceIdBox = New-TextBox 235 40 300
$tokenBox = New-TextBox 235 90 500 -Secret
$ssidBox = New-TextBox 235 180 500
$wifiPasswordBox = New-TextBox 235 230 500 -Secret
$deviceTab.Controls.Add((New-Label "端末ID *" 28 40 190))
$deviceTab.Controls.Add($deviceIdBox)
$deviceTab.Controls.Add((New-Label "端末トークン *" 28 90 190))
$deviceTab.Controls.Add($tokenBox)
$deviceTab.Controls.Add((New-Label "Wi-Fi SSID *" 28 180 190))
$deviceTab.Controls.Add($ssidBox)
$deviceTab.Controls.Add((New-Label "Wi-Fi パスワード *" 28 230 190))
$deviceTab.Controls.Add($wifiPasswordBox)
$tokenButton = [System.Windows.Forms.Button]::new()
$tokenButton.Text = "安全なトークンを生成"
$tokenButton.Location = [System.Drawing.Point]::new(545, 37)
$tokenButton.Size = [System.Drawing.Size]::new(190, 35)
$deviceTab.Controls.Add($tokenButton)
$deviceNote = New-Label "同じ端末IDとトークンをRelayとStack-chanの両方へ保存します。ファームウェア書き込み時にRelayのLANアドレスは自動設定されます。" 28 295 700 55
$deviceNote.ForeColor = [System.Drawing.Color]::FromArgb(80, 92, 110)
$deviceTab.Controls.Add($deviceNote)

$saveButton = [System.Windows.Forms.Button]::new()
$saveButton.Text = "設定を保存"
$saveButton.Location = [System.Drawing.Point]::new(25, 687)
$saveButton.Size = [System.Drawing.Size]::new(150, 40)
$saveButton.BackColor = [System.Drawing.Color]::FromArgb(25, 150, 105)
$saveButton.ForeColor = [System.Drawing.Color]::White
$saveButton.FlatStyle = "Flat"
$form.Controls.Add($saveButton)

$managerButton = [System.Windows.Forms.Button]::new()
$managerButton.Text = "保存してManagerを起動"
$managerButton.Location = [System.Drawing.Point]::new(190, 687)
$managerButton.Size = [System.Drawing.Size]::new(230, 40)
$form.Controls.Add($managerButton)

$deployButton = [System.Windows.Forms.Button]::new()
$deployButton.Text = "ファームウェアを書き込む"
$deployButton.Location = [System.Drawing.Point]::new(435, 687)
$deployButton.Size = [System.Drawing.Size]::new(220, 40)
$form.Controls.Add($deployButton)

$messageLabel = New-Label "" 665 691 150 40
$messageLabel.TextAlign = "MiddleRight"
$form.Controls.Add($messageLabel)

function Set-Status {
    param([System.Windows.Forms.Label]$Label, [bool]$Ok, [string]$Text)
    $Label.Text = if ($Ok) { "✓ $Text" } else { "— $Text" }
    $Label.ForeColor = if ($Ok) {
        [System.Drawing.Color]::FromArgb(20, 130, 85)
    } else {
        [System.Drawing.Color]::FromArgb(190, 75, 55)
    }
}

function Update-Status {
    $pythonPath = Get-PythonPath
    $pythonVersion = if ($pythonPath) { & $pythonPath --version 2>&1 } else { "未検出" }
    Set-Status $statusLabels.Python ([bool]$pythonPath) $pythonVersion

    $relayReady = Test-Path -LiteralPath $venvPython
    Set-Status $statusLabels.Relay $relayReady $(if ($relayReady) { "インストール済み" } else { "未インストール" })

    $pioVersion = if (Test-Path -LiteralPath $venvPio) {
        & $venvPio --version 2>$null
    } else {
        Get-CommandVersion "pio" @("--version")
    }
    Set-Status $statusLabels.PlatformIO ([bool]$pioVersion) $(if ($pioVersion) { $pioVersion } else { "未インストール" })

    $gitVersion = Get-CommandVersion "git" @("--version")
    Set-Status $statusLabels.Git ([bool]$gitVersion) $(if ($gitVersion) { $gitVersion } else { "未検出（クローン後の実行には必須ではありません）" })

    $azVersion = Get-CommandVersion "az" @("version", "--output", "tsv")
    Set-Status $statusLabels.AzureCLI ([bool]$azVersion) $(if ($azVersion) { "インストール済み" } else { "未インストール（任意）" })
}

function Import-Configuration {
    $envValues = Read-DotEnv
    $endpointBox.Text = [string]$envValues["AZURE_OPENAI_ENDPOINT"]
    $realtimeBox.Text = [string]$envValues["AZURE_OPENAI_REALTIME_DEPLOYMENT"]
    $responsesBox.Text = [string]$envValues["AZURE_OPENAI_RESPONSES_DEPLOYMENT"]
    $apiKeyBox.Text = [string]$envValues["AZURE_OPENAI_API_KEY"]
    $countryBox.Text = if ($envValues["WEB_SEARCH_COUNTRY"]) { $envValues["WEB_SEARCH_COUNTRY"] } else { "JP" }
    $timezoneBox.Text = if ($envValues["WEB_SEARCH_TIMEZONE"]) { $envValues["WEB_SEARCH_TIMEZONE"] } else { "Asia/Tokyo" }
    $browserCheck.Checked = [string]$envValues["LOCAL_BROWSER_TOOL_ENABLED"] -eq "true"
    $domainsBox.Text = [string]$envValues["LOCAL_BROWSER_ALLOWED_DOMAINS"]

    $deviceIdBox.Text = "stackchan-001"
    try {
        $tokens = [string]$envValues["DEVICE_TOKENS_JSON"] | ConvertFrom-Json
        $first = $tokens.PSObject.Properties | Select-Object -First 1
        if ($first) {
            $deviceIdBox.Text = $first.Name
            $tokenBox.Text = [string]$first.Value
        }
    } catch {}

    if (Test-Path -LiteralPath $secretsPath) {
        $secrets = Get-Content -LiteralPath $secretsPath -Raw
        if ($secrets -match '#define WIFI_SSID "(.*)"') { $ssidBox.Text = $matches[1] }
        if ($secrets -match '#define WIFI_PASSWORD "(.*)"') { $wifiPasswordBox.Text = $matches[1] }
        if ($secrets -match '#define DEVICE_ID "(.*)"') { $deviceIdBox.Text = $matches[1] }
        if ($secrets -match '#define DEVICE_TOKEN "(.*)"') { $tokenBox.Text = $matches[1] }
    }
}

function Save-Configuration {
    $required = @(
        @("Foundry エンドポイント", $endpointBox.Text),
        @("Realtime デプロイ名", $realtimeBox.Text),
        @("Responses デプロイ名", $responsesBox.Text),
        @("端末ID", $deviceIdBox.Text),
        @("端末トークン", $tokenBox.Text),
        @("Wi-Fi SSID", $ssidBox.Text),
        @("Wi-Fi パスワード", $wifiPasswordBox.Text)
    )
    foreach ($item in $required) {
        if ([string]::IsNullOrWhiteSpace($item[1])) {
            throw "$($item[0])を入力してください。"
        }
        if ($item[1] -match "[`r`n]") {
            throw "$($item[0])に改行は使用できません。"
        }
    }
    $uri = $null
    if (-not [uri]::TryCreate($endpointBox.Text.Trim(), [UriKind]::Absolute, [ref]$uri) -or
        $uri.Scheme -ne "https") {
        throw "Foundry エンドポイントには https:// で始まるURLを入力してください。"
    }
    if ($deviceIdBox.Text -notmatch '^[A-Za-z0-9._-]+$') {
        throw "端末IDには英数字、ピリオド、ハイフン、アンダースコアだけを使用してください。"
    }
    if ($endpointBox.Text -match "YOUR-RESOURCE|example\.openai\.azure\.com") {
        throw "実際のFoundryエンドポイントを入力してください。"
    }
    if ($tokenBox.Text -eq "replace-me") {
        throw "端末トークンを生成するか、独自の値を入力してください。"
    }
    if ($ssidBox.Text -eq "YOUR_WIFI_SSID" -or $wifiPasswordBox.Text -eq "YOUR_WIFI_PASSWORD") {
        throw "実際のWi-Fi資格情報を入力してください。"
    }

    $tokens = @{}
    $tokens[$deviceIdBox.Text.Trim()] = $tokenBox.Text
    $values = @{
        AZURE_OPENAI_ENDPOINT = $endpointBox.Text.Trim()
        AZURE_OPENAI_REALTIME_DEPLOYMENT = $realtimeBox.Text.Trim()
        AZURE_OPENAI_RESPONSES_DEPLOYMENT = $responsesBox.Text.Trim()
        AZURE_OPENAI_API_KEY = $apiKeyBox.Text.Trim()
        DEVICE_TOKENS_JSON = ($tokens | ConvertTo-Json -Compress)
        WEB_SEARCH_COUNTRY = $countryBox.Text.Trim()
        WEB_SEARCH_TIMEZONE = $timezoneBox.Text.Trim()
        LOCAL_BROWSER_TOOL_ENABLED = $browserCheck.Checked.ToString().ToLowerInvariant()
        LOCAL_BROWSER_ALLOWED_DOMAINS = $domainsBox.Text.Trim()
    }
    Write-DotEnv $values

    if (-not (Test-Path -LiteralPath $secretsPath)) {
        Copy-Item -LiteralPath $secretsExamplePath -Destination $secretsPath
    }
    $header = @"
#pragma once

#define WIFI_SSID "$(ConvertTo-CppString $ssidBox.Text)"
#define WIFI_PASSWORD "$(ConvertTo-CppString $wifiPasswordBox.Text)"

#define DEVICE_ID "$(ConvertTo-CppString $deviceIdBox.Text.Trim())"
#define DEVICE_TOKEN "$(ConvertTo-CppString $tokenBox.Text)"

// Generated by deploy-from-relay.ps1. It is kept separate so a new Relay
// address never overwrites Wi-Fi credentials or the device token.
#include "relay_endpoint.hpp"
"@
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($secretsPath, $header + [Environment]::NewLine, $utf8NoBom)
}

function Install-Prerequisites {
    $installButton.Enabled = $false
    $form.UseWaitCursor = $true
    try {
        $pythonPath = Get-PythonPath
        if (-not $pythonPath) {
            $winget = Get-Command winget -ErrorAction SilentlyContinue
            if (-not $winget) {
                throw "Pythonが見つからず、wingetも利用できません。Python 3.11以上を手動でインストールしてください。"
            }
            $installLog.AppendText("Python 3.12 をインストールしています...`r`n")
            $process = Start-Process -FilePath $winget.Source -ArgumentList @(
                "install", "--id", "Python.Python.3.12", "--exact",
                "--scope", "user", "--silent",
                "--accept-package-agreements", "--accept-source-agreements"
            ) -PassThru -WindowStyle Hidden
            while (-not $process.HasExited) {
                [System.Windows.Forms.Application]::DoEvents()
                Start-Sleep -Milliseconds 150
            }
            if ($process.ExitCode -ne 0) {
                throw "Pythonのインストールに失敗しました（終了コード $($process.ExitCode)）。"
            }
            $pythonPath = Get-PythonPath
            if (-not $pythonPath) {
                $candidate = Join-Path $env:LocalAppData "Programs\Python\Python312\python.exe"
                if (Test-Path -LiteralPath $candidate) { $pythonPath = $candidate }
            }
        }
        if (-not $pythonPath) {
            throw "インストール後のPythonを検出できませんでした。GUIを開き直してください。"
        }

        $installLog.AppendText("Relay、テスト、PlatformIOをインストールしています...`r`n")
        $arguments = @(
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", (ConvertTo-ProcessArgument (Join-Path $relayRoot "setup-windows.ps1")),
            "-PythonPath", (ConvertTo-ProcessArgument $pythonPath)
        )
        if ($headsetCheck.Checked) { $arguments += "-WithHeadset" }
        $process = Start-Process -FilePath "powershell.exe" -ArgumentList $arguments -WorkingDirectory $relayRoot -PassThru -WindowStyle Hidden
        while (-not $process.HasExited) {
            [System.Windows.Forms.Application]::DoEvents()
            Start-Sleep -Milliseconds 150
        }
        if ($process.ExitCode -ne 0) {
            throw "依存パッケージのインストールに失敗しました（終了コード $($process.ExitCode)）。"
        }
        $installLog.AppendText("インストールが完了しました。`r`n")
        Update-Status
    } finally {
        $form.UseWaitCursor = $false
        $installButton.Enabled = $true
    }
}

function Start-Manager {
    if (-not (Test-Path -LiteralPath $venvPython)) {
        throw "先に「必要項目をインストール」を実行してください。"
    }
    $managerUrl = "http://127.0.0.1:8787/"
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $managerUrl -TimeoutSec 1
        if ($response.StatusCode -eq 200) {
            Start-Process $managerUrl
            return
        }
    } catch {}

    $runManager = Join-Path $relayRoot "run-manager.ps1"
    Start-Process -FilePath "powershell.exe" -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        (ConvertTo-ProcessArgument $runManager)
    ) -WorkingDirectory $relayRoot -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds(12)
    do {
        [System.Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 200
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $managerUrl -TimeoutSec 1
            if ($response.StatusCode -eq 200) {
                Start-Process $managerUrl
                return
            }
        } catch {}
    } while ((Get-Date) -lt $deadline)
    throw "Managerの起動を確認できませんでした。relay\run-manager.ps1 を直接実行してログを確認してください。"
}

$refreshButton.Add_Click({
    try { Update-Status } catch { [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "確認エラー") }
})
$installButton.Add_Click({
    try { Install-Prerequisites } catch { [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "インストールエラー") }
})
$tokenButton.Add_Click({
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $tokenBox.Text = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
})
$saveButton.Add_Click({
    try {
        Save-Configuration
        $messageLabel.Text = "保存しました"
        $messageLabel.ForeColor = [System.Drawing.Color]::FromArgb(20, 130, 85)
    } catch {
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "設定エラー")
    }
})
$managerButton.Add_Click({
    try {
        Save-Configuration
        Start-Manager
        $messageLabel.Text = "起動しました"
    } catch {
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "起動エラー")
    }
})
$deployButton.Add_Click({
    try {
        Save-Configuration
        if (-not (Test-Path -LiteralPath $venvPio) -and -not (Get-Command pio -ErrorAction SilentlyContinue)) {
            throw "先にPlatformIOをインストールしてください。"
        }
        Start-Process -FilePath "powershell.exe" -ArgumentList @(
            "-NoExit", "-ExecutionPolicy", "Bypass", "-File",
            (ConvertTo-ProcessArgument (Join-Path $repoRoot "stackchan\deploy-from-relay.ps1"))
        ) -WorkingDirectory (Join-Path $repoRoot "stackchan")
    } catch {
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "書き込みエラー")
    }
})

Import-Configuration
Update-Status
if (-not $ValidateOnly) {
    [void]$form.ShowDialog()
}
