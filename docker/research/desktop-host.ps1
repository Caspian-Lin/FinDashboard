# Windows host operations for the MSI. The research WebView never runs these commands.
$DesktopOwnerLabel = "io.findashboard.desktop.instance"
$DesktopDockerHost = "npipe:////./pipe/dockerDesktopLinuxEngine"
$DesktopResourceRoot = $PSScriptRoot

function Test-DesktopPort($Value) {
    return ("$Value" -cmatch '^\d{4,5}$' -and [int]$Value -ge 1024 -and [int]$Value -le 65535)
}

function Assert-DesktopPath([string]$Path) {
    # Check each existing component from the drive down before accessing a child.
    $full = [IO.Path]::GetFullPath($Path)
    $current = [IO.Path]::GetPathRoot($full)
    foreach ($part in ($full.Substring($current.Length) -split '[\\/]')) {
        if (!$part) { continue }
        $current = Join-Path $current $part
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "desktop_unsafe_path: The Desktop state or a parent is a symbolic link or junction."
            }
        }
    }
}

function Resolve-DesktopRoot($Manifest) {
    $script:DesktopProject = "finboard-research-desktop"
    $script:DesktopDefaultPort = 8276
    $root = Join-Path $env:LOCALAPPDATA "FinDashboard/Desktop"
    if ($Manifest.channel -ceq "preview") {
        $script:DesktopProject = "finboard-research-desktop-preview"
        $script:DesktopDefaultPort = 8376
        $root = Join-Path $env:LOCALAPPDATA "FinDashboard/DesktopPreview"
    }
    if ($env:FINBOARD_DESKTOP_TEST_ROOT -or $env:FINBOARD_DESKTOP_TEST_PROJECT) {
        $project = $env:FINBOARD_DESKTOP_TEST_PROJECT
        if ($project -cnotmatch '^finboard-research-desktop-test-[a-z0-9]{8,24}$' -or
            !$env:FINBOARD_DESKTOP_TEST_ROOT) {
            throw "desktop_invalid_instance: Both isolated test instance settings are required."
        }
        $root = [IO.Path]::GetFullPath($env:FINBOARD_DESKTOP_TEST_ROOT)
        if ((Split-Path -Leaf $root) -cne $project) {
            throw "desktop_invalid_instance: The test state directory must match its Compose project."
        }
        $script:DesktopProject = $project
    }
    Assert-DesktopPath $root
    return [IO.Path]::GetFullPath($root)
}

function Assert-DesktopImages($Images, [bool]$RequirePair) {
    if ($RequirePair -and @($Images).Count -ne 2) {
        throw "desktop_invalid_manifest: The offline package must name one app and one PostgreSQL image."
    }
    $roles = @()
    foreach ($image in $Images) {
        if ($image.tag -isnot [string] -or $image.id -isnot [string]) {
            throw "desktop_invalid_manifest: An offline image tag or identity is invalid."
        }
        $tag = [regex]::Match($image.tag, '^finboard-research-desktop/(app|postgres):[a-z0-9][a-z0-9_.-]{0,100}$')
        if (!$tag.Success -or
            $image.id -cnotmatch '^sha256:[0-9a-f]{64}$' -or
            ($null -ne $image.compatible_ids -and $image.compatible_ids -isnot [array])) {
            throw "desktop_invalid_manifest: An offline image tag or identity is invalid."
        }
        foreach ($identity in $image.compatible_ids) {
            if ($identity -isnot [string] -or $identity -cnotmatch '^sha256:[0-9a-f]{64}$') {
                throw "desktop_invalid_manifest: A compatible offline image identity is invalid."
            }
        }
        $roles += $tag.Groups[1].Value
    }
    if ($RequirePair -and ($roles -join ",") -cne "app,postgres") {
        throw "desktop_invalid_manifest: The offline package image roles are invalid."
    }
}

function Read-DesktopJson([string]$Path, [string]$Failure) {
    Assert-DesktopPath $Path
    try { return (Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json) }
    catch { throw $Failure }
}

function Read-DesktopState {
    if (!(Test-Path -LiteralPath $StatePath)) { return $null }
    $state = Read-DesktopJson $StatePath "desktop_invalid_state: The Desktop state could not be read."
    if ($state.schema_version -ne 1 -or $state.project -cne $DesktopProject -or
        $state.instance_id -cnotmatch '^[0-9a-f]{32}$' -or !(Test-DesktopPort $state.port) -or
        $state.lifecycle -cnotin @("initializing", "ready", "retained", "preserving", "purging") -or
        $state.archive_sha256 -cnotmatch '^[0-9a-f]{64}$') {
        throw "desktop_invalid_state: The Desktop state does not match this Compose project."
    }
    Assert-DesktopImages $state.images $true
    Assert-DesktopImages $state.owned_images $false
    return $state
}

function Read-DesktopManifest {
    $manifest = Read-DesktopJson (Join-Path $DesktopResourceRoot "offline-manifest.json") (
        "desktop_invalid_manifest: The offline package manifest is missing or unreadable."
    )
    if ($manifest.schema_version -ne 1 -or $manifest.archive_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        ($null -ne $manifest.channel -and $manifest.channel -cne "preview")) {
        throw "desktop_invalid_manifest: Reinstall the matching offline MSI."
    }
    Assert-DesktopImages $manifest.images $true
    return $manifest
}

function ConvertTo-DesktopArgument([string]$Value) {
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    return '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}

function Invoke-DesktopNative(
    [string]$Program, [string[]]$Arguments, [int]$Timeout = 30, [IO.Stream]$InputData = $null
) {
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $Program
    $info.Arguments = ($Arguments | ForEach-Object { ConvertTo-DesktopArgument $_ }) -join " "
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.RedirectStandardInput = $true
    $info.StandardOutputEncoding = $Utf8
    $info.StandardErrorEncoding = $Utf8
    # Retain the caller's Docker CLI configuration, but force the local engine argument.
    foreach ($name in @("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
            "COMPOSE_FILE", "COMPOSE_ENV_FILES", "COMPOSE_PROFILES")) {
        $info.EnvironmentVariables.Remove($name)
    }
    $process = [Diagnostics.Process]::Start($info)
    try {
        $timer = [Diagnostics.Stopwatch]::StartNew()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if ($InputData) {
            $send = $InputData.CopyToAsync($process.StandardInput.BaseStream)
            if (!$send.Wait($Timeout * 1000)) {
                throw "desktop_timeout: The local operation timed out while passing input. Retry from Desktop."
            }
        }
        $process.StandardInput.Close()
        $remaining = [Math]::Max(1, $Timeout * 1000 - [int]$timer.ElapsedMilliseconds)
        if (!$process.WaitForExit($remaining)) {
            throw "desktop_timeout: The local operation timed out. Check Docker Desktop and retry."
        }
        return [PSCustomObject]@{
            exitCode = $process.ExitCode
            stdout = $stdout.GetAwaiter().GetResult().Trim()
            stderr = $stderr.GetAwaiter().GetResult().Trim()
        }
    } finally {
        if (!$process.HasExited) {
            try { $process.Kill() } catch [InvalidOperationException] { }
        }
        $process.Dispose()
    }
}

function Invoke-DesktopDocker(
    [string[]]$Arguments, [int]$Timeout = 30, [IO.Stream]$InputData = $null
) {
    return Invoke-DesktopNative $DesktopDocker (@("--host", $DesktopDockerHost) + $Arguments) $Timeout $InputData
}

function Get-DesktopDockerKind {
    if (!$DesktopDocker) { return "missing" }
    try { $result = Invoke-DesktopDocker @("info", "--format", "{{.OSType}}") 15 }
    catch { return "unavailable" }
    if ($result.exitCode -ne 0) { return "unavailable" }
    if ($result.stdout -cne "linux") { return "wrong_engine" }
    return "linux"
}

function Set-DesktopEnvironment($State, $Images, [int]$Port) {
    $env:FINBOARD_PORT = [string]$Port
    $env:FINBOARD_IMAGE = $Images[0].tag
    $env:FINBOARD_DESKTOP_POSTGRES_IMAGE = $Images[1].tag
    $env:FINBOARD_DESKTOP_INSTANCE = $State.instance_id
    $env:FINBOARD_DB_SECRET_FILE = Join-Path $Root "postgres-password"
}

function Invoke-DesktopComposeResult(
    [string[]]$Arguments, [int]$Timeout = 150, [IO.Stream]$InputData = $null
) {
    $composeArgs = @("compose", "--project-name", $DesktopProject, "--project-directory", $DesktopResourceRoot,
        "--file", $Compose, "--file", (Join-Path $DesktopResourceRoot "compose.desktop.yaml"))
    return Invoke-DesktopDocker ($composeArgs + $Arguments) $Timeout $InputData
}

function Invoke-DesktopCompose(
    [string[]]$Arguments, [int]$Timeout = 150, [IO.Stream]$InputData = $null
) {
    $result = Invoke-DesktopComposeResult $Arguments $Timeout $InputData
    if ($result.exitCode -ne 0) {
        $diagnostic = $result.stderr
        if ($diagnostic -match 'port is already allocated|ports are not available|address already in use') {
            throw "desktop_port_in_use: Choose an unused loopback port and retry the installation."
        }
        if ($diagnostic -match 'starter_bundle_[a-z_]+') {
            throw "desktop_starter_invalid: The bundled starter failed validation or conflicts with existing data."
        }
        throw "desktop_compose_failed: Docker Compose failed to $($Arguments[0]). Check Docker Desktop and retry."
    }
    return $result.stdout
}

function Invoke-DesktopOperator([string]$Command, [IO.Stream]$InputData) {
    $arguments = if ($DesktopDetachedOperator) {
        @("run", "--rm", "--no-deps", "--pull", "never", "-T", "gateway", "operator", $Command)
    } else {
        @("exec", "-T", "gateway", "python", "/opt/finboard/entrypoint.py", "operator", $Command)
    }
    return (Invoke-DesktopCompose $arguments 120 $InputData | ConvertFrom-Json)
}

function Invoke-DesktopSource([bool]$Configure, [int]$Port, [IO.Stream]$InputData) {
    if (!(Test-DesktopGateway $Port)) {
        throw "desktop_gateway_unavailable: Start this Desktop instance before checking Tushare."
    }
    $operation = if ($Configure) { "source-configure" } else { "source-status" }
    $result = Invoke-DesktopComposeResult @("exec", "-T", "gateway", "python", "/opt/finboard/entrypoint.py",
        "operator", $operation) 180 $InputData
    try { $reply = $result.stdout | ConvertFrom-Json }
    catch { throw "desktop_tushare_unavailable: Tushare checks did not return a result. Retry after checking the service." }
    $allowed = @("available", "credentials_missing", "invalid_credentials", "permission_denied",
        "quota_exhausted", "connection_failed", "coverage_insufficient")
    if ($reply.source -cne "tushare" -or $reply.capabilities.daily -cnotin $allowed -or
        (!$Configure -and $result.exitCode -ne 0) -or $result.exitCode -notin @(0, 1)) {
        throw "desktop_tushare_unavailable: Tushare checks did not return a result. Retry after checking the service."
    }
    $checks = [ordered]@{}
    foreach ($name in @("daily", "adj_factor", "index_daily", "daily_basic", "fina_indicator", "trade_cal")) {
        $value = $reply.capabilities.$name
        if ($value -is [string] -and $value -cin $allowed) { $checks[$name] = $value }
    }
    $saved = $Configure -and $result.exitCode -eq 0 -and $checks.trade_cal -ceq "available"
    $restart = "not_requested"
    if ($saved) {
        $restart = "failed"
        try {
            [void](Invoke-DesktopCompose @("restart", "gateway", "worker") 60)
            [void](Invoke-DesktopCompose @("up", "-d", "--wait", "--wait-timeout", "120", "--pull",
                "never", "--no-build", "gateway", "worker"))
            if (Test-DesktopGateway $Port) { $restart = "ready" }
        } catch {
            # 凭据已经保存。恢复服务启动，不重发凭据。
        }
    }
    return [ordered]@{source = "tushare"; saved = $saved; restart = $restart; capabilities = $checks}
}

function Get-DesktopOwnedResources($State) {
    $resources = @{}
    foreach ($kind in @("container", "network", "volume")) {
        $listArgs = @($kind, "ls")
        if ($kind -eq "container") { $listArgs += "--all" }
        $format = if ($kind -eq "container") { "{{.Names}}" } else { "{{.Name}}" }
        $byProject = Invoke-DesktopDocker ($listArgs + @("--filter", "label=com.docker.compose.project=$DesktopProject", "--format", $format))
        $byName = Invoke-DesktopDocker ($listArgs + @("--filter", "name=$DesktopProject", "--format", $format))
        if ($byProject.exitCode -ne 0 -or $byName.exitCode -ne 0) {
            throw "desktop_docker_unavailable: Docker resource ownership could not be checked. Retry after Docker Desktop is ready."
        }
        $expectedNames = switch ($kind) {
            "container" { "postgres", "migrate", "gateway", "worker" | ForEach-Object { "$DesktopProject-$_-1" } }
            "network" { "$($DesktopProject)_default" }
            "volume" { "$($DesktopProject)_postgres"; "$($DesktopProject)_research" }
        }
        $names = @(
            ($byProject.stdout -split "\r?\n")
            ($byName.stdout -split "\r?\n") | Where-Object { $_ -cin $expectedNames }
        ) | Where-Object { $_ } | Sort-Object -Unique
        $resources[$kind] = @($names)
        if (!$names) { continue }
        $info = Invoke-DesktopDocker (@($kind, "inspect") + @($names))
        if ($info.exitCode -ne 0) {
            throw "desktop_docker_unavailable: Docker resources changed during inspection. Retry Desktop status."
        }
        $objects = $info.stdout | ConvertFrom-Json
        if (@($objects).Count -ne @($names).Count) {
            throw "desktop_resource_conflict: Docker resource ownership could not be verified."
        }
        foreach ($item in $objects) {
            $labels = if ($kind -eq "container") { $item.Config.Labels } else { $item.Labels }
            if (!$State -or $labels.'com.docker.compose.project' -cne $DesktopProject -or
                $labels.$DesktopOwnerLabel -cne $State.instance_id) {
                throw "desktop_resource_conflict: A resource with this Desktop project name belongs to another installation."
            }
            if ($kind -ne "container" -and $item.Name -cnotin $expectedNames) {
                throw "desktop_resource_conflict: An unexpected network or volume is registered under the Desktop project."
            }
        }
    }
    return $resources
}

function Get-DesktopImage($Image, [switch]$RetainUnverifiedTag) {
    $result = Invoke-DesktopDocker @("image", "inspect", "--format", "{{.Id}} {{.Os}} {{.Architecture}}", $Image.tag)
    if ($result.exitCode -ne 0) {
        if ($result.stderr -match '(?im)^Error(?: response from daemon)?:\s*No such image(?:\s|:)') {
            return $false
        }
        throw "desktop_docker_unavailable: Docker could not check the offline image. Check the Linux engine and retry."
    }
    $allowedIds = @($Image.id) + @($Image.compatible_ids)
    $identity = [regex]::Match($result.stdout, '^(sha256:[0-9a-f]{64}) linux amd64$')
    if (!$identity.Success -or $identity.Groups[1].Value -cnotin $allowedIds) {
        if ($RetainUnverifiedTag) {
            Write-Host "Retaining Desktop image tag $($Image.tag) because its current identity cannot be confirmed."
            return $false
        }
        throw "desktop_image_conflict: A local Desktop image has a different identity. Verify the installed package and Docker tags."
    }
    return $true
}

function Get-DesktopArchiveHash([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    try {
        $sha256 = [Security.Cryptography.SHA256]::Create()
        try { return [BitConverter]::ToString($sha256.ComputeHash($stream)).Replace("-", "").ToLowerInvariant() }
        finally { $sha256.Dispose() }
    } finally { $stream.Dispose() }
}

function Install-DesktopImages($State, $Manifest) {
    $missing = @($Manifest.images | Where-Object { !(Get-DesktopImage $_) })
    if ($missing.Count -gt 0 -or $State.lifecycle -cne "ready" -or
        $State.archive_sha256 -cne $Manifest.archive_sha256) {
        Write-Host "Verifying the offline Docker image archive..."
        $archive = Join-Path $DesktopResourceRoot "images.tar"
        Assert-DesktopPath $archive
        if (!(Test-Path -LiteralPath $archive -PathType Leaf) -or
            (Get-DesktopArchiveHash $archive) -cne $Manifest.archive_sha256) {
            throw "desktop_archive_checksum: The offline image archive is missing or damaged. Reinstall the matching MSI."
        }
        if ($missing.Count -gt 0) {
            foreach ($image in $missing) {
                if ($image.tag -cnotin @($State.owned_images | ForEach-Object { $_.tag })) {
                    $State.owned_images += $image
                }
            }
            Write-File $StatePath ($State | ConvertTo-Json -Depth 8 -Compress)
            Write-Host "Loading verified Docker images from the MSI..."
            $loaded = Invoke-DesktopDocker @("image", "load", "--quiet", "--input", $archive) 900
            if ($loaded.exitCode -ne 0) {
                throw "desktop_image_load: Docker could not load the offline images. Check Docker Desktop disk space and retry."
            }
        }
    }
    foreach ($image in $Manifest.images) {
        if (!(Get-DesktopImage $image)) {
            throw "desktop_image_load: An expected image tag is missing after import. Reinstall the matching MSI."
        }
    }
}

function Invoke-DesktopCodex {
    $codex = Get-Command codex -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (!$codex) {
        throw "desktop_codex_missing: Install or update Codex so its CLI is on PATH, then retry."
    }
    $result = Invoke-DesktopNative $codex.Source $args
    if ($result.exitCode -ne 0) {
        throw "desktop_codex_failed: Codex could not update its local plugin registration. Update Codex and retry."
    }
    return $result.stdout
}

function Get-DesktopCodexMarketplace {
    $response = Invoke-DesktopCodex "plugin" "marketplace" "list" "--json" | ConvertFrom-Json
    $matching = @($response.marketplaces | Where-Object { $_.name -ceq $DesktopProject })
    $expected = Join-Path $Root "codex-marketplace"
    if ($matching.Count -gt 1 -or ($matching.Count -eq 1 -and
            [IO.Path]::GetFullPath($matching[0].root) -ne $expected)) {
        throw "desktop_codex_conflict: This Desktop marketplace name is registered from another location."
    }
    return ($matching | Select-Object -First 1)
}

function Unregister-DesktopCodex {
    if (!(Test-Path -LiteralPath $DesktopCredentialPath) -and
        !(Test-Path -LiteralPath (Join-Path $Root "codex-marketplace/.agents/plugins/marketplace.json"))) {
        return
    }
    if (!(Get-DesktopCodexMarketplace)) { return }
    $plugins = Invoke-DesktopCodex "plugin" "list" "--marketplace" $DesktopProject "--json" | ConvertFrom-Json
    if ($plugins.installed | Where-Object { $_.name -ceq "finboard-research" -and $_.marketplaceName -ceq $DesktopProject }) {
        [void](Invoke-DesktopCodex "plugin" "remove" "finboard-research@$DesktopProject" "--json")
    }
    [void](Invoke-DesktopCodex "plugin" "marketplace" "remove" $DesktopProject "--json")
}

function Test-DesktopGateway([int]$Port) {
    $result = Invoke-DesktopDocker @("container", "inspect", "--format",
        '{{json (index .NetworkSettings.Ports "8000/tcp")}}', "$DesktopProject-gateway-1")
    if ($result.exitCode -ne 0) { return $false }
    $bindings = $result.stdout | ConvertFrom-Json
    return (@($bindings).Count -eq 1 -and @($bindings)[0].HostIp -ceq "127.0.0.1" -and
        @($bindings)[0].HostPort -ceq [string]$Port)
}

function Get-DesktopRuntime($State, [string]$DockerKind) {
    $result = [ordered]@{
        docker = $DockerKind
        installed = [bool]($State -and $State.lifecycle -ceq "ready")
        has_instance = [bool]$State
        phase = if ($State) { $State.lifecycle } else { "absent" }
        running = $false
        port = if ($State) { [int]$State.port } else { $DesktopDefaultPort }
        preset = $null
        scopes = @()
        credential_active = $null
        clients = @()
        generic_clients = @()
        dsh_profiles = @()
        pending_revocation = $false
        repair = $null
        project = $DesktopProject
        volumes = @("$($DesktopProject)_postgres", "$($DesktopProject)_research")
    }
    if ($DockerKind -cne "linux") {
        $result.repair = if ($DockerKind -ceq "missing") { "Install Docker Desktop." } else {
            "Start Docker Desktop in Linux containers mode and check access to the local engine."
        }
    } elseif ($State -and $State.lifecycle -cin @("preserving", "purging")) {
        $result.repair = "Resume the interrupted uninstall before starting Desktop again."
    } elseif ($State -and $State.lifecycle -ceq "ready") {
        Set-DesktopEnvironment $State $State.images $State.port
        try {
            [void](Get-DesktopOwnedResources $State)
            $rows = @()
            foreach ($line in (Invoke-DesktopCompose @("ps", "--all", "--format", "json") 25) -split "\r?\n") {
                if ($line) {
                    $parsed = $line | ConvertFrom-Json
                    $rows += @($parsed)
                }
            }
            $services = @($rows | Where-Object { $_.State -ceq "running" -and
                ($_.Service -ceq "worker" -or $_.Health -ceq "healthy") } | ForEach-Object { $_.Service })
            $result.running = "gateway" -cin $services -and "postgres" -cin $services -and
                "worker" -cin $services -and (Test-DesktopGateway $State.port)
        } catch {
            $result.repair = "Check Docker Desktop and verify this instance's project resources."
        }
    }
    $result.clients = @(foreach ($name in @("codex", "dsh", "opencode")) {
        Get-DesktopHarnessStatus $name $result.running
    })
    $codexStatus = $result.clients[0]
    $result.preset = $codexStatus.preset
    $result.scopes = $codexStatus.scopes
    $result.pending_revocation = $codexStatus.pending_revocation
    $result.credential_active = $codexStatus.credential_active
    try {
        $result.generic_clients = @(foreach ($record in (Get-DesktopGenericRecords)) {
            @{
                name = $record.name; preset = $record.preset
                scopes = @(Get-DesktopClientScopes $record.preset)
                pending_revocation = [bool]$record.pending_revoke_id
            }
        })
    } catch {
        if (!$result.repair) { $result.repair = "Check the local generic MCP client setup before changing its credentials." }
    }
    $profileRoot = Get-DshProfileRoot
    if (Test-Path -LiteralPath $profileRoot -PathType Container) {
        $result.dsh_profiles = @(Get-ChildItem -LiteralPath $profileRoot -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -cmatch '^[a-zA-Z0-9_-]{1,64}$' -and
                !($_.Attributes -band [IO.FileAttributes]::ReparsePoint) } |
            Select-Object -ExpandProperty Name | Sort-Object)
    }
    return $result
}

function Get-DesktopClientScopes([string]$Preset) {
    if ($Preset -ceq "readonly") { return "research.read" }
    if ($Preset -ceq "topic-writer") { return @("research.read", "research.write", "research.run") }
}

function Get-DesktopClientWorkspace([string]$ClientName) {
    $directory = switch -CaseSensitive ($ClientName) {
        "dsh" { "workspace" }
        "opencode" { "opencode-workspace" }
        default { throw "desktop_invalid_arguments: Only DSH and OpenCode have a Desktop workspace." }
    }
    $workspace = Join-Path $Root $directory
    Assert-DesktopPath $workspace
    return $workspace
}

function Get-DesktopHarnessStatus([string]$Name, [bool]$Running) {
    $entry = [ordered]@{name = $Name; preset = $null; scopes = @(); profile = $null
        credential_active = $null; pending_revocation = $false; repair_required = $false}
    try {
        $path = Join-Path $Root "$Name.json"
        Assert-DesktopPath $path
        if (Test-Path -LiteralPath $path) {
            $credential = Read-DesktopJson $path "desktop_client_setup: The local client setup could not be read."
            if ($credential.preset -cnotin @("readonly", "topic-writer")) { throw "invalid preset" }
            $entry.preset = $credential.preset
            $entry.scopes = @(Get-DesktopClientScopes $credential.preset)
            $entry.pending_revocation = [bool]$credential.pending_revoke_id
            if ($Name -ceq "dsh" -and $credential.profile -cmatch '^[a-zA-Z0-9_-]{1,64}$') {
                $entry.profile = $credential.profile
            }
            if ($Running) { $entry.credential_active = Test-Credential (Get-Plaintext $credential.sealed) }
        }
    } catch { $entry.repair_required = $true }
    return $entry
}

function Get-DesktopGenericPath([string]$Name) {
    if ($Name -cnotmatch '^[a-z0-9][a-z0-9_-]{0,31}$') {
        throw "desktop_invalid_arguments: Use 1 to 32 lowercase letters, digits, hyphens or underscores for the client name."
    }
    $path = Join-Path $Root "generic-clients/client-$Name.json"
    Assert-DesktopPath $path
    return $path
}

function Read-DesktopGenericRecord([string]$Name) {
    $path = Get-DesktopGenericPath $Name
    if (!(Test-Path -LiteralPath $path)) { return }
    $state = Read-DesktopJson $path "desktop_client_setup: The local client setup could not be read."
    if ($state.name -cne $Name -or $state.client_id -cnotmatch '^MC-[0-9a-f]{12}$' -or
        $state.preset -cnotin @("readonly", "topic-writer") -or
        ($state.pending_revoke_id -and $state.pending_revoke_id -cnotmatch '^MC-[0-9a-f]{12}$')) {
        throw "desktop_client_setup: The local client setup could not be verified."
    }
    return $state
}

function Get-DesktopGenericRecords {
    $directory = Join-Path $Root "generic-clients"
    Assert-DesktopPath $directory
    if (Test-Path -LiteralPath $directory -PathType Container) {
        foreach ($file in (Get-ChildItem -LiteralPath $directory -Filter "client-*.json" -File)) {
            Read-DesktopGenericRecord $file.BaseName.Substring(7)
        }
    }
}

function Issue-DesktopGenericClient([string]$Name, [string]$Preset) {
    $path = Get-DesktopGenericPath $Name
    $previous = Read-DesktopGenericRecord $Name
    Complete-ClientRotation $path $previous
    $issued = Register-ClientCredential "generic-$Name" $Preset
    try {
        if (!(Test-Credential $issued.token)) { throw "desktop_client_setup: MCP initialization did not succeed." }
        $state = [PSCustomObject]@{name = $Name; client_id = $issued.client_id; preset = $Preset}
        if ($previous) {
            $state | Add-Member -NotePropertyName pending_revoke_id -NotePropertyValue $previous.client_id
        }
        Write-File $path ($state | ConvertTo-Json -Compress)
    } catch {
        $failure = $_
        try { [void](Invoke-Operator "client-revoke" (@{client_id = $issued.client_id} | ConvertTo-Json -Compress)) }
        catch { Write-Warning "An unused credential could not be revoked. Its token was not returned." }
        throw $failure
    }
    Complete-ClientRotation $path $state
    return @{connection = @{name = $Name; url = "$Endpoint/mcp"; credential = $issued.token
        scopes = @(Get-DesktopClientScopes $Preset)}}
}

function Revoke-DesktopGenericClient([string]$Name) {
    $path = Get-DesktopGenericPath $Name
    $state = Read-DesktopGenericRecord $Name
    if (!$state) { return }
    Complete-ClientRotation $path $state
    [void](Invoke-Operator "client-revoke" (@{client_id = $state.client_id} | ConvertTo-Json -Compress))
    [IO.File]::Delete($path)
}

function Revoke-DesktopHarness([string]$Name) {
    $Client = $Name
    $DesktopCredentialPath = Join-Path $Root "$Name.json"
    Assert-DesktopPath $DesktopCredentialPath
    switch -CaseSensitive ($Name) {
        "codex" { Unregister-DesktopCodex }
        "dsh" {
            if (Test-Path -LiteralPath $DesktopCredentialPath) {
                $credential = Read-DesktopJson $DesktopCredentialPath "desktop_client_setup: The DSH setup could not be read."
                Remove-DesktopDshPatch $credential.profile
            }
        }
        "opencode" {
            $workspace = Get-DesktopClientWorkspace $Name
            $path = Join-Path $workspace "opencode.jsonc"
            Assert-DesktopPath $path
            if (Test-DesktopManagedOpenCode $path) { [IO.File]::Delete($path) }
        }
    }
    if (Test-Path -LiteralPath $DesktopCredentialPath) { Revoke-Client }
}

function Test-DesktopVolumePair($Resources) {
    return ("$($DesktopProject)_postgres" -cin $Resources.volume -and
        "$($DesktopProject)_research" -cin $Resources.volume)
}

function Remove-DesktopHostEntry([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if (!$full.StartsWith($Root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "desktop_unsafe_path: Refused to delete a path outside this Desktop instance."
    }
    $item = Get-Item -LiteralPath $full -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "desktop_unsafe_path: Remove the link inside the Desktop state directory manually and retry."
    }
    if ($item.PSIsContainer) {
        Get-ChildItem -LiteralPath $full -Force | ForEach-Object { Remove-DesktopHostEntry $_.FullName }
        [IO.Directory]::Delete($full)
    } else {
        [IO.File]::Delete($full)
    }
}

function Remove-DesktopInstallation($State, $Resources) {
    if ($State.lifecycle -cnotin @("preserving", "purging")) {
        $State | Add-Member -NotePropertyName cleanup_initializing -NotePropertyValue ($State.lifecycle -ceq "initializing") -Force
        $State.lifecycle = if ($Options["--mode"] -ceq "purge") { "purging" } else { "preserving" }
        Write-File $StatePath ($State | ConvertTo-Json -Depth 8 -Compress)
    }
    Write-Host "Checking this Desktop instance's client credentials and Docker resources..."
    $harnesses = @("codex", "dsh", "opencode" | Where-Object {
        $path = Join-Path $Root "$_.json"
        Assert-DesktopPath $path
        Test-Path -LiteralPath $path
    })
    $generic = @(Get-DesktopGenericRecords)
    if ($harnesses.Count -or $generic.Count) {
        if ("$($DesktopProject)_postgres" -notin $Resources.volume) {
            throw "desktop_missing_volume: The original database volume is missing. Restore it before revoking clients."
        }
        [void](Invoke-DesktopCompose @("up", "-d", "--wait", "--wait-timeout", "90", "--pull", "never", "--no-build", "postgres"))
        $DesktopDetachedOperator = $true
        foreach ($name in $harnesses) { Revoke-DesktopHarness $name }
        foreach ($record in $generic) { Revoke-DesktopGenericClient $record.name }
        $Resources = Get-DesktopOwnedResources $State
    }
    if ("codex" -cnotin $harnesses) { Unregister-DesktopCodex }
    if ($Resources.container.Count -or $Resources.network.Count) {
        Write-Host "Removing this Desktop instance's containers and network..."
        [void](Invoke-DesktopCompose @("down", "--remove-orphans"))
    }
    $remaining = Get-DesktopOwnedResources $State
    if ($remaining.container.Count -or $remaining.network.Count) {
        throw "desktop_cleanup_pending: Some Desktop containers or networks remain. Close their users in Docker Desktop and retry."
    }
    foreach ($image in $State.owned_images) {
        if (!(Get-DesktopImage $image -RetainUnverifiedTag)) { continue }
        $consumers = Invoke-DesktopDocker @("container", "ls", "--all", "--filter", "ancestor=$($image.tag)", "--quiet")
        if ($consumers.exitCode -ne 0) {
            throw "desktop_cleanup_pending: Docker image use could not be checked. Retry the uninstall."
        }
        if ($consumers.stdout) {
            $references = Invoke-DesktopDocker (@("container", "inspect", "--format", "{{.Config.Image}}") +
                @($consumers.stdout -split "\r?\n"))
            if ($references.exitCode -ne 0) {
                throw "desktop_cleanup_pending: Docker image use changed during inspection. Retry the uninstall."
            }
            if ($image.tag -cin @($references.stdout -split "\r?\n")) {
                Write-Host "Retaining a Desktop image tag used by another container."
                continue
            }
        }
        $removed = Invoke-DesktopDocker @("image", "rm", $image.tag)
        if ($removed.exitCode -ne 0) {
            throw "desktop_cleanup_pending: An offline image tag is in use. Check Docker Desktop and retry."
        }
    }
    if ($Options["--mode"] -ceq "preserve") {
        $State.lifecycle = if ($State.cleanup_initializing -and !(Test-DesktopVolumePair $remaining)) {
            "initializing"
        } else { "retained" }
        $State.PSObject.Properties.Remove("cleanup_initializing")
        Write-File $StatePath ($State | ConvertTo-Json -Depth 8 -Compress)
        Write-Host "Desktop services removed. Research volumes and recovery state retained."
        return
    }
    foreach ($volume in $remaining.volume) {
        $removed = Invoke-DesktopDocker @("volume", "rm", $volume)
        if ($removed.exitCode -ne 0) {
            throw "desktop_cleanup_pending: A Desktop volume is in use. Stop its consumers and retry the purge."
        }
    }
    $remaining = Get-DesktopOwnedResources $State
    if ($remaining.volume.Count) {
        throw "desktop_cleanup_pending: A Desktop volume remains. Check Docker Desktop and retry the purge."
    }
    Assert-DesktopPath $Root
    Get-ChildItem -LiteralPath $Root -Force |
        Where-Object { $_.Name -notin @("desktop-operation.lock", "state.json") } |
        ForEach-Object { Remove-DesktopHostEntry $_.FullName }
    Remove-DesktopHostEntry $StatePath
    Write-Host "Desktop services, volumes and local instance data removed."
}

function Invoke-DesktopAction {
    $allowedOptions = switch -CaseSensitive ($Action) {
        "desktop-install" { "--port" }
        "desktop-attach" { "--preset"; "--client"; "--profile" }
        "desktop-detach" { "--client" }
        "desktop-client-workspace" { "--client"; "--mode" }
        "desktop-client-issue" { "--name"; "--preset" }
        "desktop-client-revoke" { "--name" }
        "desktop-uninstall" { "--mode"; "--confirm" }
        { $_ -cin @("desktop-status", "desktop-stop", "desktop-import",
            "desktop-source-status", "desktop-source-configure") } { }
        default { throw "desktop_invalid_action: Unknown Desktop operation." }
    }
    if ($Options.Keys | Where-Object { $_ -cnotin $allowedOptions }) {
        throw "desktop_invalid_arguments: Unsupported option for this Desktop operation."
    }
    if ($Action -ceq "desktop-client-issue" -and !$Options.ContainsKey("--preset")) {
        $Options["--preset"] = "readonly"
    }
    if ($Action -cin @("desktop-attach", "desktop-client-issue") -and
        $Options["--preset"] -cnotin @("readonly", "topic-writer")) {
        throw "desktop_invalid_arguments: Choose readonly or topic-writer."
    }
    if ($Action -ceq "desktop-attach" -and !$Options.ContainsKey("--client")) { $Options["--client"] = "codex" }
    if ($Action -cin @("desktop-attach", "desktop-detach") -and $Options["--client"] -cnotin @("codex", "dsh", "opencode")) {
        throw "desktop_invalid_arguments: Choose Codex, DSH, or OpenCode."
    }
    if ($Action -ceq "desktop-attach" -and (($Options["--client"] -ceq "dsh" -and
            $Options["--profile"] -cnotmatch '^[a-zA-Z0-9_-]{1,64}$') -or
            ($Options["--client"] -cne "dsh" -and $Options.ContainsKey("--profile")))) {
        throw "desktop_invalid_arguments: Choose an existing DSH profile for this setup."
    }
    if ($Action -ceq "desktop-uninstall" -and ($Options["--mode"] -cnotin @("preserve", "purge") -or
            ($Options["--mode"] -ceq "purge" -and $Options["--confirm"] -cne "DELETE-THIS-INSTANCE") -or
            ($Options["--mode"] -ceq "preserve" -and $Options.ContainsKey("--confirm")))) {
        throw "desktop_confirmation_required: Choose preserve or explicitly confirm deletion of this instance's data."
    }
    if ($Options.ContainsKey("--port") -and !(Test-DesktopPort $Options["--port"])) {
        throw "desktop_invalid_arguments: Choose a port between 1024 and 65535."
    }
    $manifest = Read-DesktopManifest
    $Root = Resolve-DesktopRoot $manifest
    if ($Action -ceq "desktop-client-workspace") {
        $workspace = Get-DesktopClientWorkspace $Options["--client"]
        if (!(Test-Path -LiteralPath $workspace -PathType Container) -or
            $Options["--mode"] -cnotin @("open", "location")) {
            throw "desktop_client_setup: Configure the client before opening its Desktop workspace."
        }
        if ($Options["--mode"] -ceq "open") { Start-Process -FilePath $workspace -ErrorAction Stop }
        @{workspace = $workspace} | ConvertTo-Json -Compress
        return
    }
    $StatePath = Join-Path $Root "state.json"
    $Bridge = Join-Path $Root "mcp-bridge.ps1"
    $Client = if ($Action -cin @("desktop-attach", "desktop-detach")) { $Options["--client"] } else { "codex" }
    $DesktopCredentialPath = Join-Path $Root "$Client.json"
    $DesktopDocker = Find-DesktopDocker
    $DockerKind = Get-DesktopDockerKind
    if ($Action -ceq "desktop-status") {
        $State = Read-DesktopState
        $Endpoint = "http://127.0.0.1:$(if ($State) { $State.port } else { $DesktopDefaultPort })"
        Get-DesktopRuntime $State $DockerKind | ConvertTo-Json -Depth 5 -Compress
        return
    }
    if ($DockerKind -cne "linux") {
        throw "desktop_docker_unavailable: Start Docker Desktop in Linux containers mode and retry this operation."
    }
    [void][IO.Directory]::CreateDirectory($Root)
    $lockPath = Join-Path $Root "desktop-operation.lock"
    Assert-DesktopPath $lockPath
    try { $lock = [IO.File]::Open($lockPath, "OpenOrCreate", "ReadWrite", "None") }
    catch [IO.IOException] { throw "desktop_busy: A Desktop operation is in progress. Retry when it completes." }
    try {
        Assert-DesktopPath $DesktopCredentialPath
        $State = Read-DesktopState
        $resources = Get-DesktopOwnedResources $State
        if ($State -and $State.lifecycle -cin @("preserving", "purging")) {
            $retryPhase = if ($Options["--mode"] -ceq "purge") { "purging" } else { "preserving" }
            if ($Action -cne "desktop-uninstall" -or $State.lifecycle -cne $retryPhase) {
                throw "desktop_cleanup_pending: Resume the existing uninstall mode before starting Desktop again."
            }
        }
        if (!$State -and $Action -ceq "desktop-uninstall") {
            Write-Host "This Desktop instance is already removed."
            return
        }
        if ($Action -ceq "desktop-install") {
            if ($State -and $State.lifecycle -cin @("ready", "retained") -and !(Test-DesktopVolumePair $resources)) {
                throw "desktop_missing_volume: Restore both original Desktop data volumes from backup before starting."
            }
            $port = if ($Options["--port"]) { [int]$Options["--port"] } elseif ($State) { $State.port } else { $DesktopDefaultPort }
            if (!$State) {
                $State = [PSCustomObject]@{
                    schema_version = 1; project = $DesktopProject; instance_id = [Guid]::NewGuid().ToString("N")
                    port = $port; lifecycle = "initializing"; archive_sha256 = $manifest.archive_sha256
                    images = @($manifest.images); owned_images = @()
                }
            }
            Set-DesktopEnvironment $State $manifest.images $port
            Assert-DesktopPath $env:FINBOARD_DB_SECRET_FILE
            Assert-DesktopPath $Bridge
            if ($resources.volume.Count -and !(Test-Path -LiteralPath $env:FINBOARD_DB_SECRET_FILE)) {
                throw "desktop_missing_secret: Restore this Desktop instance's PostgreSQL secret from backup before starting."
            }
            Initialize-State
            Write-File $StatePath ($State | ConvertTo-Json -Depth 8 -Compress)
            Install-DesktopImages $State $manifest
            Write-Host "Starting PostgreSQL, migrating the database, and waiting for the research services..."
            [void](Invoke-DesktopCompose @("up", "-d", "--wait", "--wait-timeout", "120", "--pull", "never", "--no-build"))
            if (!(Test-DesktopVolumePair (Get-DesktopOwnedResources $State))) {
                throw "desktop_missing_volume: Docker did not register both Desktop data volumes. Check the project and retry."
            }
            $State.images = @($manifest.images)
            $State.archive_sha256 = $manifest.archive_sha256
            $State.port = $port
            $State.lifecycle = "ready"
            Write-File $StatePath ($State | ConvertTo-Json -Depth 8 -Compress)
            Write-Host "FinDashboard Research is ready at http://127.0.0.1:$port."
            return
        }
        if (!$State) { throw "desktop_not_installed: Install the offline research services first." }
        Set-DesktopEnvironment $State $State.images $State.port
        Assert-DesktopPath $env:FINBOARD_DB_SECRET_FILE
        $Endpoint = "http://127.0.0.1:$($State.port)"
        $BridgeArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $Bridge, "-InstallRoot", $Root, "-Client", $Client)
        if ($Action -cne "desktop-uninstall" -and $State.lifecycle -cne "ready") {
            throw "desktop_not_installed: Start or reinstall the offline research services first."
        }
        switch -CaseSensitive ($Action) {
            "desktop-source-status" {
                Invoke-DesktopSource $false $State.port | ConvertTo-Json -Depth 4 -Compress
            }
            "desktop-source-configure" {
                $inputData = [Console]::OpenStandardInput()
                try { Invoke-DesktopSource $true $State.port $inputData | ConvertTo-Json -Depth 4 -Compress }
                finally { $inputData.Dispose() }
            }
            "desktop-stop" {
                [void](Invoke-DesktopCompose @("stop"))
                Write-Host "Desktop research services stopped."
            }
            "desktop-attach" {
                if (!(Test-DesktopGateway $State.port)) {
                    throw "desktop_gateway_unavailable: Start this Desktop instance before connecting a client."
                }
                Write-Host "Configuring the selected client and checking its research credential..."
                try { Configure-Client }
                catch {
                    if ($_.Exception.Message -match '^desktop_[a-z_]+: ') { throw }
                    throw "desktop_client_setup: Setup did not complete. Check the client and, for DSH, its profile."
                }
            }
            "desktop-detach" { Revoke-DesktopHarness $Client }
            "desktop-client-issue" {
                if (!(Test-DesktopGateway $State.port)) {
                    throw "desktop_gateway_unavailable: Start this Desktop instance before connecting a client."
                }
                Issue-DesktopGenericClient $Options["--name"] $Options["--preset"] | ConvertTo-Json -Depth 4 -Compress
            }
            "desktop-client-revoke" { Revoke-DesktopGenericClient $Options["--name"] }
            "desktop-import" {
                Write-Host "Verifying and importing the synthetic starter data..."
                $bundle = Join-Path $DesktopResourceRoot "starter.zip"
                Assert-DesktopPath $bundle
                if (!(Test-Path -LiteralPath $bundle -PathType Leaf)) {
                    throw "desktop_starter_invalid: The bundled synthetic starter is missing. Reinstall the offline MSI."
                }
                $inputData = [IO.File]::OpenRead($bundle)
                try { Invoke-DesktopOperator "import" $inputData | ConvertTo-Json -Compress }
                finally { $inputData.Dispose() }
            }
            "desktop-uninstall" { Remove-DesktopInstallation $State $resources }
        }
    } finally { $lock.Dispose() }
}

function Find-DesktopDocker {
    return (Get-Command docker.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
}
