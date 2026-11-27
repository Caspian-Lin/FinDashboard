function Test-Credential([string]$Token) {
    try {
        $init = @{
            jsonrpc = "2.0"; id = 1; method = "initialize"
            params = @{protocolVersion = "2025-06-18"; capabilities = @{}; clientInfo = @{name = "finboard-setup"; version = "1"}}
        } | ConvertTo-Json -Depth 6 -Compress
        Add-Type -AssemblyName System.Net.Http
        $handler = New-Object System.Net.Http.HttpClientHandler
        $handler.AllowAutoRedirect = $false
        $handler.UseProxy = $false
        $http = New-Object System.Net.Http.HttpClient($handler)
        $http.Timeout = [TimeSpan]::FromSeconds(10)
        $http.DefaultRequestHeaders.Add("Authorization", "Bearer $Token")
        $http.DefaultRequestHeaders.Add("Accept", "application/json, text/event-stream")
        $content = New-Object System.Net.Http.StringContent($init, [Text.Encoding]::UTF8, "application/json")
        try {
            $response = $http.PostAsync("$Endpoint/mcp", $content).GetAwaiter().GetResult()
            try {
                if (!$response.IsSuccessStatusCode) { return $false }
                $reply = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult() | ConvertFrom-Json
                return ($reply.result.serverInfo.name -eq "finboard")
            } finally { $response.Dispose() }
        } finally {
            $content.Dispose()
            $http.Dispose()
        }
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
    $marketplaceName = if ($DesktopAction) { $DesktopProject } else { "finboard-local" }
    $serverName = if ($DesktopAction) { $DesktopProject } else { "finboard" }
    if ($DesktopAction) {
        Assert-DesktopPath (Join-Path $plugin ".codex-plugin/plugin.json")
        Assert-DesktopPath (Join-Path $plugin ".mcp.json")
        Assert-DesktopPath (Join-Path $plugin "skills/finboard-research")
        Assert-DesktopPath (Join-Path $market ".agents/plugins/marketplace.json")
        [void](Get-DesktopCodexMarketplace)
    }
    $entry = @{
        name = "finboard-research"
        source = @{source = "local"; path = "./plugins/finboard-research"}
        policy = @{installation = "AVAILABLE"; authentication = "ON_INSTALL"}
        category = "Productivity"
    }
    Write-File (Join-Path $market ".agents/plugins/marketplace.json") (@{
        name = $marketplaceName; interface = @{displayName = "FinDashboard"}; plugins = @($entry)
    } | ConvertTo-Json -Depth 8)
    Write-File (Join-Path $plugin ".codex-plugin/plugin.json") (@{
        name = "finboard-research"; version = "1.0.0"; description = "FinDashboard Research"
        skills = "./skills/"; mcpServers = "./.mcp.json"
    } | ConvertTo-Json)
    Write-File (Join-Path $plugin ".mcp.json") (@{mcpServers = @{$serverName = @{
        command = "powershell.exe"; args = $BridgeArgs; startup_timeout_sec = 25; tool_timeout_sec = 360
    }}} | ConvertTo-Json -Depth 6)
    Install-Skill (Join-Path $plugin "skills")
    if ($DesktopAction) {
        [void](Invoke-DesktopCodex "plugin" "marketplace" "add" $market)
        [void](Invoke-DesktopCodex "plugin" "add" "finboard-research@$marketplaceName")
        return
    }
    & codex plugin marketplace add $market
    if ($LASTEXITCODE -ne 0) { throw "Update Codex CLI and retry finboard setup." }
    & codex plugin add "finboard-research@finboard-local"
    if ($LASTEXITCODE -ne 0) { throw "Codex plugin install failed. Check codex plugin marketplace list." }
}

function Test-DesktopManagedOpenCode([string]$Path) {
    if (!(Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    $owner = "// FinDashboard Desktop $DesktopProject" + [Environment]::NewLine
    $content = [IO.File]::ReadAllText($Path)
    if (!$content.StartsWith($owner, [StringComparison]::Ordinal)) { return $false }
    try { $config = $content.Substring($owner.Length) | ConvertFrom-Json }
    catch { return $false }
    if (($config.PSObject.Properties.Name -join ",") -cne "mcp") { return $false }
    if (($config.mcp.PSObject.Properties.Name -join ",") -ceq "servers") {
        return (($config.mcp.servers.PSObject.Properties.Name -join ",") -ceq $DesktopProject)
    }
    return (($config.mcp.PSObject.Properties.Name -join ",") -ceq $DesktopProject)
}

function Install-OpenCode {
    if ($DesktopAction) {
        $executable = Get-Command opencode.cmd, opencode.exe -ErrorAction SilentlyContinue | Select-Object -First 1
        if (!$executable) { throw "desktop_client_missing: Install OpenCode and retry its Desktop setup." }
        $version = (& $executable.Source --version 2>$null | Select-Object -Last 1)
        if ($LASTEXITCODE -ne 0 -or $version -notmatch '^v?([12])\.') {
            throw "desktop_opencode_version: Desktop requires a supported OpenCode CLI version."
        }
        $generation = $Matches[1]
        $workspace = Get-DesktopClientWorkspace "opencode"
        $name = "$DesktopProject"
        $entry = @{type = "local"; command = @("powershell.exe") + $BridgeArgs}
        $mcp = if ($generation -ceq "2") { @{servers = @{$name = $entry}} }
            else { @{$name = $entry} }
        $path = Join-Path $workspace "opencode.jsonc"
        $owner = "// FinDashboard Desktop $DesktopProject"
        Assert-DesktopPath (Join-Path $workspace ".opencode/skills")
        Assert-DesktopPath $path
        if ((Test-Path -LiteralPath $path) -and !(Test-DesktopManagedOpenCode $path)) {
            throw "desktop_opencode_config: Desktop will only replace its own OpenCode workspace configuration."
        }
        # 保存项目级接入，避免改写 OpenCode 的全局模型、MCP 或用户项目设置。
        Write-File $path ($owner + [Environment]::NewLine + (@{mcp = $mcp} | ConvertTo-Json -Depth 8))
        Install-Skill (Join-Path $workspace ".opencode/skills")
        return
    }
    $arguments = @("mcp", "add", "finboard-research", "--global", "--", "powershell.exe") + $BridgeArgs
    $shim = Get-Command opencode.cmd -ErrorAction SilentlyContinue
    if ($shim) { & $shim.Source @arguments } else { & opencode @arguments }
    if ($LASTEXITCODE -ne 0) { throw "Update OpenCode and retry finboard setup." }
    $config = if ($env:XDG_CONFIG_HOME) { $env:XDG_CONFIG_HOME } else { Join-Path (Resolve-Path "~").Path ".config" }
    Install-Skill (Join-Path $config "opencode/skills")
}

function Get-DshProfileRoot {
    $dsh = Join-Path (Resolve-Path "~").Path ".dsh"
    if ($env:DSH_HOME) { $dsh = $env:DSH_HOME }
    return (Join-Path $dsh "profiles")
}

function Get-DshProfilePatch([string]$Profile) {
    if ($Profile -notmatch "^[a-zA-Z0-9_-]{1,64}$") { throw "Invalid DSH profile name." }
    return (Join-Path (Get-DshProfileRoot) "$Profile/cordis.patch.yml")
}

function Get-DesktopDshMarker {
    $name = [regex]::Escape($DesktopProject)
    return "(?ms)^# BEGIN FINDASHBOARD $name\r?\n.*?^# END FINDASHBOARD $name\r?\n?"
}

function Remove-DesktopDshPatch([string]$Profile) {
    if (!$Profile) { return }
    $path = Get-DshProfilePatch $Profile
    Assert-DesktopPath $path
    if (Test-Path -LiteralPath $path) {
        $old = [IO.File]::ReadAllText($path)
        $next = [regex]::Replace($old, (Get-DesktopDshMarker), "")
        if ($next -cne $old) { Write-File $path $next }
    }
}

function Install-Dsh {
    $profile = if ($Options["--profile"]) { $Options["--profile"] } else { "desktop" }
    $path = Get-DshProfilePatch $profile
    $profileRoot = Split-Path -Parent $path
    if (!(Test-Path -LiteralPath $profileRoot)) { throw "Open DSH once to create profile '$profile', then run setup again." }
    if ($DesktopAction) { Assert-DesktopPath $path }
    $existing = if (Test-Path -LiteralPath $path) { [IO.File]::ReadAllText($path) } else { "" }
    $marker = if ($DesktopAction) { Get-DesktopDshMarker }
        else { '(?ms)^# BEGIN FINDASHBOARD\r?\n.*?^# END FINDASHBOARD\r?\n?' }
    $existing = [regex]::Replace($existing, $marker, "")
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
    if (!$DesktopAction) {
        if ($existing -match '(?m)^- id: skill-filesystem\s*$') {
            $existing = [regex]::Replace($existing, '(?m)(^- id: skill-filesystem\s*\r?\n[ \t]+disabled:)\s*true\b', '$1 false')
        } else {
            $skillEntry = @"
- id: skill-filesystem
  disabled: false
"@
        }
    }
    $ownership = if ($DesktopAction) { " $DesktopProject" } else { "" }
    $serverName = if ($DesktopAction) { $DesktopProject } else { "finboard-research" }
    $block = @"
# BEGIN FINDASHBOARD$ownership
- insert:
    - id: $serverName-mcp
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: $serverName
        transport: streamable-http
        url: $Endpoint/mcp
        headers:
          Authorization: !!js '$auth'
        maxInstructionBytes: 262144
        toolCallTimeoutMs: 360000
        failOnStartupError: false
$skillEntry
# END FINDASHBOARD$ownership
"@
    Write-File $path ($existing.TrimEnd() + [Environment]::NewLine + $block + [Environment]::NewLine)
    $workspace = if ($DesktopAction) { Get-DesktopClientWorkspace "dsh" } else { Join-Path $Root "workspace" }
    if ($DesktopAction) { Assert-DesktopPath (Join-Path $workspace ".agents/skills") }
    Install-Skill (Join-Path $workspace ".agents/skills")
    Write-Host "For DSH skills, open this project folder: $workspace"
}

function Invoke-ClientChange([scriptblock]$Action) {
    $lockPath = Join-Path $Root "$Client.lock"
    try {
        $lock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } catch [IO.IOException] {
        throw "Another $Client credential operation is in progress. Retry after it completes."
    }
    try { & $Action } finally { $lock.Dispose() }
}

function Complete-ClientRotation([string]$Path, $Credential) {
    if ($Credential.pending_revoke_id) {
        [void](Invoke-Operator "client-revoke" (@{client_id = $Credential.pending_revoke_id} | ConvertTo-Json -Compress))
        $Credential.PSObject.Properties.Remove("pending_revoke_id")
        Write-File $Path ($Credential | ConvertTo-Json -Compress)
    }
    if ($Credential.pending_remove_profile) {
        Remove-DesktopDshPatch $Credential.pending_remove_profile
        $Credential.PSObject.Properties.Remove("pending_remove_profile")
        Write-File $Path ($Credential | ConvertTo-Json -Compress)
    }
}

function Configure-Client {
    Invoke-ClientChange { Set-ClientCredential }
}

function Register-ClientCredential([string]$ClientName, [string]$Preset) {
    $id = "MC-" + (To-Hex (New-RandomBytes 6))
    $secret = [Convert]::ToBase64String((New-RandomBytes 32)).TrimEnd("=").Replace("+", "-").Replace("/", "_")
    $token = "fbmc_${id}_$secret"
    $hash = [Security.Cryptography.SHA256]::Create()
    try { $digest = To-Hex ($hash.ComputeHash($Utf8.GetBytes($token))) }
    finally { $hash.Dispose() }
    $registration = @{
        client_id = $id; token_hash = $digest; token_prefix = $secret.Substring(0, 16)
        client = $ClientName; preset = $Preset
    } | ConvertTo-Json -Compress
    [void](Invoke-Operator "client-register" $registration)
    return @{client_id = $id; token = $token}
}

function Set-ClientCredential {
    $path = Join-Path $Root "$Client.json"
    $old = if (Test-Path -LiteralPath $path) { Get-Content -LiteralPath $path -Raw | ConvertFrom-Json } else { $null }
    $preset = if ($Options["--preset"]) { $Options["--preset"] } elseif ($old) { $old.preset } else { "readonly" }
    if ($preset -notin @("readonly", "topic-writer")) { throw "Invalid credential preset." }
    Complete-ClientRotation $path $old
    # Finish client registration before changing the credential read by existing bridges.
    switch ($Client) {
        "codex" { Install-Codex }
        "dsh" { Install-Dsh }
        "opencode" { Install-OpenCode }
    }
    $valid = $false
    if ($old -and $old.preset -eq $preset) {
        try { $valid = Test-Credential (Get-Plaintext $old.sealed) } catch { $valid = $false }
    }
    $profile = if ($DesktopAction -and $Client -ceq "dsh") { $Options["--profile"] }
    $oldProfile = if ($old) { $old.profile }
    $movedProfile = $profile -and $oldProfile -and $profile -ine $oldProfile
    if (!$valid) {
        $registered = Register-ClientCredential $Client $preset
        $id = $registered.client_id
        $token = $registered.token
        try {
            if (!(Test-Credential $token)) { throw "MCP initialization failed. Check finboard status and rerun setup." }
            Add-Type -AssemblyName System.Security
            $sealed = [Convert]::ToBase64String([Security.Cryptography.ProtectedData]::Protect(
                $Utf8.GetBytes($token), $null, [Security.Cryptography.DataProtectionScope]::CurrentUser))
            $credential = [PSCustomObject]@{client_id = $id; preset = $preset; sealed = $sealed}
            if ($profile) { $credential | Add-Member -NotePropertyName profile -NotePropertyValue $profile }
            if ($old) {
                $credential | Add-Member -NotePropertyName pending_revoke_id -NotePropertyValue $old.client_id
            }
            if ($movedProfile) {
                $credential | Add-Member -NotePropertyName pending_remove_profile -NotePropertyValue $oldProfile
            }
            Write-File $path ($credential | ConvertTo-Json -Compress)
        } catch {
            $failure = $_
            try {
                [void](Invoke-Operator "client-revoke" (@{client_id = $id} | ConvertTo-Json -Compress))
            } catch {
                Write-Warning "Could not revoke the unused new credential. Its token was not installed."
            }
            throw $failure
        }
        Complete-ClientRotation $path $credential
    } elseif ($profile -and $profile -ine $oldProfile) {
        if ($movedProfile) {
            $old | Add-Member -NotePropertyName pending_remove_profile -NotePropertyValue $oldProfile
        }
        $old | Add-Member -NotePropertyName profile -NotePropertyValue $profile -Force
        Write-File $path ($old | ConvertTo-Json -Compress)
        Complete-ClientRotation $path $old
    }
    Write-Host "FinDashboard is ready at $Endpoint. Reopen $Client to use the $preset credential."
}

function Revoke-Client {
    Invoke-ClientChange {
        $path = Join-Path $Root "$Client.json"
        if (Test-Path -LiteralPath $path) {
            $credential = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
            Complete-ClientRotation $path $credential
            [void](Invoke-Operator "client-revoke" (@{client_id = $credential.client_id} | ConvertTo-Json -Compress))
            [IO.File]::Delete($path)
        }
    }
}
