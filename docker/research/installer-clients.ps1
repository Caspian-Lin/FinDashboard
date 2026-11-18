function Test-Credential([string]$Token) {
    try {
        $init = @{
            jsonrpc = "2.0"; id = 1; method = "initialize"
            params = @{protocolVersion = "2025-06-18"; capabilities = @{}; clientInfo = @{name = "finboard-setup"; version = "1"}}
        } | ConvertTo-Json -Depth 6 -Compress
        $headers = @{Authorization = "Bearer $Token"; Accept = "application/json, text/event-stream"}
        $reply = Invoke-RestMethod -Uri "$Endpoint/mcp" -Method Post -TimeoutSec 10 -Headers $headers -ContentType "application/json" -Body $init
        return ($reply.result.serverInfo.name -eq "finboard")
    } catch { return $false }
}

function Get-Plaintext([string]$Sealed) {
    Add-Type -AssemblyName System.Security
    $bytes = [Security.Cryptography.ProtectedData]::Unprotect(
        [Convert]::FromBase64String($Sealed), $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
    return $Utf8.GetString($bytes)
}

function Install-Skill([string]$Skills) {
    [void][IO.Directory]::CreateDirectory($Skills)
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "finboard-research") -Destination $Skills -Force -Recurse
}

function Install-Codex {
    $market = Join-Path $Root "codex-marketplace"
    $plugin = Join-Path $market "plugins/finboard-research"
    $entry = @{
        name = "finboard-research"
        source = @{source = "local"; path = "./plugins/finboard-research"}
        policy = @{installation = "AVAILABLE"; authentication = "ON_INSTALL"}
        category = "Productivity"
    }
    Write-File (Join-Path $market ".agents/plugins/marketplace.json") (@{
        name = "finboard-local"; interface = @{displayName = "FinDashboard"}; plugins = @($entry)
    } | ConvertTo-Json -Depth 8)
    Write-File (Join-Path $plugin ".codex-plugin/plugin.json") (@{
        name = "finboard-research"; version = "1.0.0"; description = "FinDashboard Research"
        skills = "./skills/"; mcpServers = "./.mcp.json"
    } | ConvertTo-Json)
    Write-File (Join-Path $plugin ".mcp.json") (@{mcpServers = @{finboard = @{
        command = "powershell.exe"; args = $BridgeArgs; startup_timeout_sec = 25; tool_timeout_sec = 360
    }}} | ConvertTo-Json -Depth 6)
    Install-Skill (Join-Path $plugin "skills")
    & codex plugin marketplace add $market
    if ($LASTEXITCODE -ne 0) { throw "Update Codex CLI and retry finboard setup." }
    & codex plugin add "finboard-research@finboard-local"
    if ($LASTEXITCODE -ne 0) { throw "Codex plugin install failed. Check codex plugin marketplace list." }
}

function Install-OpenCode {
    $arguments = @("mcp", "add", "finboard-research", "--global", "--", "powershell.exe") + $BridgeArgs
    $shim = Get-Command opencode.cmd -ErrorAction SilentlyContinue
    if ($shim) { & $shim.Source @arguments } else { & opencode @arguments }
    if ($LASTEXITCODE -ne 0) { throw "Update OpenCode and retry finboard setup." }
    $config = if ($env:XDG_CONFIG_HOME) { $env:XDG_CONFIG_HOME } else { Join-Path (Resolve-Path "~").Path ".config" }
    Install-Skill (Join-Path $config "opencode/skills")
}

function Install-Dsh {
    $dsh = Join-Path (Resolve-Path "~").Path ".dsh"
    if ($env:DSH_HOME) { $dsh = $env:DSH_HOME }
    $profile = if ($Options["--profile"]) { $Options["--profile"] } else { "desktop" }
    if ($profile -notmatch "^[a-zA-Z0-9_-]+$") { throw "Invalid DSH profile name." }
    $profileRoot = Join-Path $dsh "profiles/$profile"
    if (!(Test-Path -LiteralPath $profileRoot)) { throw "Open DSH once to create profile '$profile', then run setup again." }
    $path = Join-Path $profileRoot "cordis.patch.yml"
    $existing = if (Test-Path -LiteralPath $path) { [IO.File]::ReadAllText($path) } else { "" }
    $existing = [regex]::Replace($existing, '(?ms)^# BEGIN FINDASHBOARD\r?\n.*?^# END FINDASHBOARD\r?\n?', "")
    $withoutComments = [regex]::Replace($existing, '(?m)^[ \t]*#.*$', "").Trim()
    if ($withoutComments -eq "[]") {
        $existing = [regex]::Replace($existing, '(?m)^[ \t]*\[\][ \t]*\r?$', "")
        $withoutComments = ""
    }
    if ($withoutComments -ne "" -and $existing -notmatch '(?m)^-\s') {
        throw "DSH patch must be a YAML list. See research edition documentation."
    }
    $cliArgs = ConvertTo-Json -Compress -InputObject @($BridgeArgs + "-PrintToken")
    $auth = ('(function(){return "Bearer "+process.getBuiltinModule("node:child_process").execFileSync("powershell.exe",' + $cliArgs + ',{encoding:"utf8",windowsHide:true}).trim()})()').Replace("'", "''")
    $skillEntry = ""
    if ($existing -match '(?m)^- id: skill-filesystem\s*$') {
        $existing = [regex]::Replace($existing, '(?m)(^- id: skill-filesystem\s*\r?\n[ \t]+disabled:)\s*true\b', '$1 false')
    } else {
        $skillEntry = @"
- id: skill-filesystem
  disabled: false
"@
    }
    $block = @"
# BEGIN FINDASHBOARD
- insert:
    - id: finboard-research-mcp
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: finboard-research
        transport: streamable-http
        url: $Endpoint/mcp
        headers:
          Authorization: !!js '$auth'
        maxInstructionBytes: 262144
        toolCallTimeoutMs: 360000
        failOnStartupError: false
$skillEntry
# END FINDASHBOARD
"@
    Write-File $path ($existing.TrimEnd() + [Environment]::NewLine + $block + [Environment]::NewLine)
    $workspace = Join-Path $Root "workspace"
    Install-Skill (Join-Path $workspace ".agents/skills")
    Write-Host "For DSH skills, open this project folder: $workspace"
}

function Configure-Client {
    $path = Join-Path $Root "$Client.json"
    $old = if (Test-Path -LiteralPath $path) { Get-Content -LiteralPath $path -Raw | ConvertFrom-Json } else { $null }
    $preset = if ($Options["--preset"]) { $Options["--preset"] } elseif ($old) { $old.preset } else { "readonly" }
    if ($preset -notin @("readonly", "topic-writer")) { throw "Invalid credential preset." }
    $valid = $false
    if ($old -and $old.preset -eq $preset) {
        try { $valid = Test-Credential (Get-Plaintext $old.sealed) } catch { $valid = $false }
    }
    if (!$valid) {
        $id = "MC-" + (To-Hex (New-RandomBytes 6))
        $secret = [Convert]::ToBase64String((New-RandomBytes 32)).TrimEnd("=").Replace("+", "-").Replace("/", "_")
        $token = "fbmc_${id}_$secret"
        $hash = [Security.Cryptography.SHA256]::Create()
        try { $digest = To-Hex ($hash.ComputeHash($Utf8.GetBytes($token))) }
        finally { $hash.Dispose() }
        $registration = @{
            client_id = $id; token_hash = $digest; token_prefix = $secret.Substring(0, 16)
            client = $Client; preset = $preset
        } | ConvertTo-Json -Compress
        [void](Invoke-Operator "client-register" $registration)
        try {
            if (!(Test-Credential $token)) { throw "MCP initialization failed. Check finboard status and rerun setup." }
            Add-Type -AssemblyName System.Security
            $sealed = [Convert]::ToBase64String([Security.Cryptography.ProtectedData]::Protect(
                $Utf8.GetBytes($token), $null, [Security.Cryptography.DataProtectionScope]::CurrentUser))
            Write-File $path (@{client_id = $id; preset = $preset; sealed = $sealed} | ConvertTo-Json -Compress)
        } catch {
            [void](Invoke-Operator "client-revoke" (@{client_id = $id} | ConvertTo-Json -Compress))
            throw
        }
        if ($old) {
            [void](Invoke-Operator "client-revoke" (@{client_id = $old.client_id} | ConvertTo-Json -Compress))
        }
    }
    switch ($Client) {
        "codex" { Install-Codex }
        "dsh" { Install-Dsh }
        "opencode" { Install-OpenCode }
    }
    Write-Host "FinDashboard is ready at $Endpoint. Reopen $Client to use the $preset credential."
}
