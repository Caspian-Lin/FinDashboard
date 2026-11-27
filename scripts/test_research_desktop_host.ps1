$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "../docker/research/desktop-host.ps1")
. (Join-Path $PSScriptRoot "../docker/research/installer-clients.ps1")

$fixtureProject = "finboard-research-desktop-test-" + [Guid]::NewGuid().ToString("N").Substring(0, 12)
$fixtureRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "../.tmp/$fixtureProject"))
$previousDshHome = $env:DSH_HOME
$env:DSH_HOME = Join-Path $fixtureRoot "dsh"
$DesktopAction = $true
$DesktopResourceRoot = Join-Path $fixtureRoot "package files"
$instanceRoot = Join-Path $fixtureRoot $fixtureProject
$env:FINBOARD_DESKTOP_TEST_ROOT = $instanceRoot
$env:FINBOARD_DESKTOP_TEST_PROJECT = $fixtureProject
$Compose = Join-Path $DesktopResourceRoot "compose.yaml"
$Utf8 = New-Object Text.UTF8Encoding($false)
$archivePath = Join-Path $DesktopResourceRoot "images.tar"
$stateFile = Join-Path $instanceRoot "state.json"
$credentialFile = Join-Path $instanceRoot "codex.json"
$secondProject = "$fixtureProject-other"
$fixture = @{
    docker = $true; port = 0; running = $false; portBusy = $false; failVolume = $false
    loads = 0; downs = 0; composeArgs = @(); operator = @()
    sourceToken = $null; sourceCode = "credentials_missing"; failRestart = $false; sourceRestarts = 0
    clientRecords = @{}
    container = @{}; network = @{}; volume = @{}; images = @{}; sharedTag = $null
    marketplace = $true; installedPlugin = $true
}

function Require($Condition, [string]$Message) {
    if (!$Condition) { throw $Message }
}

function Require-Failure([scriptblock]$Operation, [string]$Code) {
    try { & $Operation }
    catch {
        if (!$_.Exception.Message.StartsWith($Code + ":")) { throw }
        return
    }
    throw "Expected $Code."
}

function Write-File([string]$Path, [string]$Value) {
    if ($fixture.failWritePath -and $Path -ceq $fixture.failWritePath) {
        throw "fixture_profile_write_failed: simulated DSH file write failure"
    }
    [void][IO.Directory]::CreateDirectory((Split-Path -Parent $Path))
    [IO.File]::WriteAllText($Path, $Value, $Utf8)
}

function Find-DesktopDocker { return "fixture-docker.exe" }

function Initialize-State {
    if (!(Test-Path -LiteralPath $env:FINBOARD_DB_SECRET_FILE)) {
        Write-File $env:FINBOARD_DB_SECRET_FILE "isolated-fixture"
    }
}

function Configure-Client {
    Write-File $DesktopCredentialPath (@{
        sealed = "fixture"; preset = $Options["--preset"]; profile = $Options["--profile"]
    } | ConvertTo-Json)
}

function Get-Plaintext([string]$Sealed) { return $Sealed }
function New-RandomBytes([int]$Length) {
    $bytes = [byte[]]::new($Length)
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $random.GetBytes($bytes) } finally { $random.Dispose() }
    return ,$bytes
}
function To-Hex([byte[]]$Bytes) { return [BitConverter]::ToString($Bytes).Replace("-", "").ToLowerInvariant() }
function Test-Credential([string]$Token) {
    if ($Token -ceq "fixture") { return $true }
    $hash = [Security.Cryptography.SHA256]::Create()
    try { $digest = To-Hex ($hash.ComputeHash($Utf8.GetBytes($Token))) } finally { $hash.Dispose() }
    return [bool]($fixture.clientRecords.Values | Where-Object token_hash -ceq $digest)
}
function Invoke-Operator([string]$Command, [string]$InputText) {
    $request = $InputText | ConvertFrom-Json
    if ($Command -ceq "client-register") {
        $fixture.clientRecords[$request.client_id] = $request
        return @{registered = $true}
    }
    if ($Command -ceq "client-revoke") {
        $present = $fixture.clientRecords.ContainsKey($request.client_id)
        $fixture.clientRecords.Remove($request.client_id)
        return @{revoked = $present}
    }
    throw "Unexpected test operator."
}
function Install-Skill([string]$Skills) { [void][IO.Directory]::CreateDirectory($Skills) }
function Revoke-Client {
    Require ($DesktopDetachedOperator -or $Action -ceq "desktop-detach") "Uninstall must revoke independently of the stopped gateway."
    [IO.File]::Delete($DesktopCredentialPath)
}

function Invoke-DesktopCodex {
    if ($args[1] -ceq "marketplace" -and $args[2] -ceq "list") {
        $markets = @()
        if ($fixture.marketplace) {
            $root = if ($fixture.foreignMarketplace) { $fixtureRoot } else { Join-Path $instanceRoot "codex-marketplace" }
            $markets += @{name = $fixtureProject; root = $root}
        }
        return (@{marketplaces = $markets} | ConvertTo-Json -Depth 6 -Compress)
    }
    if ($args[1] -ceq "list") {
        $plugins = @()
        if ($fixture.installedPlugin) { $plugins += @{name = "finboard-research"; marketplaceName = $fixtureProject} }
        return (@{installed = $plugins} | ConvertTo-Json -Depth 6 -Compress)
    }
    if ($args[1] -ceq "remove") { $fixture.installedPlugin = $false; return "{}" }
    if ($args[1] -ceq "marketplace" -and $args[2] -ceq "remove") { $fixture.marketplace = $false; return "{}" }
    throw "Unexpected Codex command."
}

function Result([string]$Stdout = "", [int]$ExitCode = 0, [string]$Stderr = "") {
    return [PSCustomObject]@{exitCode = $ExitCode; stdout = $Stdout; stderr = $Stderr}
}

function Add-FixtureResource([string]$Kind, [string]$Name, [string]$Project, [string]$Owner) {
    $fixture[$Kind][$Name] = @{name = $Name; project = $Project; owner = $Owner}
}

function Invoke-DesktopDocker(
    [string[]]$Arguments, [int]$Timeout = 30, [IO.Stream]$InputData = $null
) {
    if ($Arguments[0] -ceq "info") {
        if ($fixture.docker) { return Result "linux" }
        return Result "" 1 "permission denied"
    }
    if ($Arguments[0] -ceq "compose") {
        Require ($Arguments[2] -ceq $fixtureProject) "The Compose project is missing or belongs to another instance."
        $operation = $Arguments[9]
        $fixture.composeArgs += ($Arguments -join " ")
        Require ($operation -cne "pull") "Offline desktop must not call Compose pull."
        switch ($operation) {
            "up" {
                Require ("--pull" -cin $Arguments -and "never" -cin $Arguments) "Compose up must prohibit pulls."
                if ($fixture.portBusy) { return Result "" 1 "address already in use" }
                $parts = if ($Arguments[-1] -ceq "postgres") { @("postgres") } else { @("postgres", "migrate", "worker", "gateway") }
                foreach ($part in $parts) {
                    Add-FixtureResource "container" "$fixtureProject-$part-1" $fixtureProject $env:FINBOARD_DESKTOP_INSTANCE
                }
                Add-FixtureResource "network" "$($fixtureProject)_default" $fixtureProject $env:FINBOARD_DESKTOP_INSTANCE
                Add-FixtureResource "volume" "$($fixtureProject)_postgres" $fixtureProject $env:FINBOARD_DESKTOP_INSTANCE
                Add-FixtureResource "volume" "$($fixtureProject)_research" $fixtureProject $env:FINBOARD_DESKTOP_INSTANCE
                if ($Arguments[-1] -cne "postgres") { $fixture.port = [int]$env:FINBOARD_PORT; $fixture.running = $true }
                return Result
            }
            "stop" { $fixture.running = $false; return Result }
            "restart" {
                Require ($Arguments[-2] -ceq "gateway" -and $Arguments[-1] -ceq "worker") "Tushare may restart the research services only."
                $fixture.sourceRestarts += 1
                if ($fixture.failRestart) { return Result "" 1 "injected restart error" }
                return Result
            }
            "down" {
                $fixture.downs += 1
                foreach ($kind in @("container", "network")) {
                    foreach ($resource in @($fixture[$kind].Values | Where-Object { $_.project -ceq $fixtureProject })) {
                        $fixture[$kind].Remove($resource.name)
                    }
                }
                $fixture.running = $false
                return Result
            }
            "ps" {
                if (!$fixture.running) { return Result }
                $lines = @("postgres", "worker", "gateway" | ForEach-Object {
                    @{Service = $_; State = "running"; Health = "healthy"} | ConvertTo-Json -Compress
                })
                return Result ($lines -join [Environment]::NewLine)
            }
            "exec" {
                if ($Arguments[-1] -cin @("source-configure", "source-status")) {
                    $checks = @{daily = $fixture.sourceCode}
                    $exitCode = 0
                    if ($Arguments[-1] -ceq "source-configure") {
                        $reader = New-Object IO.StreamReader($InputData)
                        try { $fixture.sourceToken = $reader.ReadToEnd() } finally { $reader.Dispose() }
                        if ($fixture.sourceCode -ceq "available") { $checks.trade_cal = "available" }
                        else { $exitCode = 1 }
                    }
                    $reply = @{
                        source = "tushare"; capabilities = $checks
                        arbitrary_log = $fixture.sourceToken
                    } | ConvertTo-Json -Compress
                    return Result $reply $exitCode
                }
                Require ($Arguments[-1] -ceq "import" -and $null -ne $InputData) "Import bypassed the existing operator."
                $reader = New-Object IO.StreamReader($InputData)
                try { $fixture.operator += $reader.ReadToEnd() } finally { $reader.Dispose() }
                return Result '{"release_id":"synthetic-starter-v1"}'
            }
        }
        throw "Unexpected Compose operation."
    }
    $kind, $verb = $Arguments
    if ($kind -ceq "image") {
        switch ($verb) {
            "inspect" {
                if (!$fixture.images.ContainsKey($Arguments[-1])) { return Result "" 1 "Error: No such image: missing" }
                return Result "$($fixture.images[$Arguments[-1]]) linux amd64"
            }
            "load" {
                $fixture.loads += 1
                foreach ($image in $manifest.images) { $fixture.images[$image.tag] = $image.id }
                return Result
            }
            "rm" { $fixture.images.Remove($Arguments[-1]); return Result }
        }
    }
    if ($verb -ceq "ls") {
        $filter = $Arguments[([Array]::IndexOf($Arguments, "--filter") + 1)]
        if ($filter.StartsWith("ancestor=")) {
            if ($filter -ceq "ancestor=$($fixture.sharedTag)") { return Result "$secondProject-gateway-1" }
            return Result
        }
        $value = $filter.Split("=")[-1]
        $items = if ($filter.StartsWith("label=")) {
            $fixture[$kind].Values | Where-Object { $_.project -ceq $value }
        } else { $fixture[$kind].Values | Where-Object { $_.name.Contains($value) } }
        return Result (($items | ForEach-Object { $_.name }) -join [Environment]::NewLine)
    }
    if ($verb -ceq "inspect") {
        if ($Arguments[2] -ceq "--format") {
            if ($Arguments[3] -ceq "{{.Config.Image}}") { return Result $fixture.sharedTag }
            return Result ('[{"HostIp":"127.0.0.1","HostPort":"' + $fixture.port + '"}]')
        }
        $items = foreach ($name in $Arguments[2..($Arguments.Count - 1)]) {
            $entry = $fixture[$kind][$name]
            $labels = @{"com.docker.compose.project" = $entry.project; "io.findashboard.desktop.instance" = $entry.owner}
            @{Name = $entry.name; Labels = $labels; Config = @{Labels = $labels}}
        }
        return Result (ConvertTo-Json -InputObject @($items) -Depth 6 -Compress)
    }
    if ($kind -ceq "volume" -and $verb -ceq "rm") {
        if ($fixture.failVolume -and $Arguments[-1].EndsWith("_research")) {
            return Result "" 1 "injected interruption"
        }
        $fixture.volume.Remove($Arguments[-1])
        return Result
    }
    throw "Unexpected Docker operation: $($Arguments -join ' ')"
}

function Run-Desktop([string]$Name, [hashtable]$Arguments = @{}) {
    $Action = "desktop-$Name"
    $Options = $Arguments
    Invoke-DesktopAction
}

try {
    [void][IO.Directory]::CreateDirectory($DesktopResourceRoot)
    Write-File $archivePath "test-image-archive"
    Write-File (Join-Path $DesktopResourceRoot "starter.zip") "synthetic-bundle"
    $manifest = @{
        schema_version = 1
        archive_sha256 = (Get-FileHash -LiteralPath $archivePath).Hash.ToLowerInvariant()
        images = @(
            @{tag = "finboard-research-desktop/app:test"; id = "sha256:" + ("a" * 64); compatible_ids = @("sha256:" + ("d" * 64))},
            @{tag = "finboard-research-desktop/postgres:test"; id = "sha256:" + ("b" * 64)}
        )
    }
    Write-File (Join-Path $DesktopResourceRoot "offline-manifest.json") ($manifest | ConvertTo-Json -Depth 6)
    $originalAppData = $env:LOCALAPPDATA
    try {
        $env:LOCALAPPDATA = $fixtureRoot
        $env:FINBOARD_DESKTOP_TEST_ROOT = $null
        $env:FINBOARD_DESKTOP_TEST_PROJECT = $null
        $previewRoot = Resolve-DesktopRoot @{channel = "preview"}
        Require ($previewRoot -ceq (Join-Path $fixtureRoot "FinDashboard\DesktopPreview")) "Preview must have separate local state."
        Require ($DesktopProject -ceq "finboard-research-desktop-preview" -and $DesktopDefaultPort -eq 8376) "Preview must have a separate Compose project and port."
        $releaseRoot = Resolve-DesktopRoot $manifest
        Require ($releaseRoot -ceq (Join-Path $fixtureRoot "FinDashboard\Desktop")) "Release state must remain in its normal location."
        Require ($DesktopProject -ceq "finboard-research-desktop" -and $DesktopDefaultPort -eq 8276) "Release must use its own Compose project and port."
    } finally {
        $env:LOCALAPPDATA = $originalAppData
        $env:FINBOARD_DESKTOP_TEST_ROOT = $instanceRoot
        $env:FINBOARD_DESKTOP_TEST_PROJECT = $fixtureProject
    }
    Add-FixtureResource "volume" "$($secondProject)_research" $secondProject "foreign-owner"
    Add-FixtureResource "container" "$secondProject-gateway-1" $secondProject "foreign-owner"

    $fixture.docker = $false
    $status = Run-Desktop "status" | ConvertFrom-Json
    Require ($status.docker -ceq "unavailable" -and $status.repair) "Docker unavailability needs a fix hint."
    Require-Failure { Run-Desktop "uninstall" @{"--mode" = "purge"} } "desktop_confirmation_required"
    Require-Failure { Run-Desktop "install" } "desktop_docker_unavailable"
    $fixture.docker = $true

    Add-FixtureResource "volume" "$($fixtureProject)_research" $secondProject "foreign-owner"
    Require-Failure { Run-Desktop "install" } "desktop_resource_conflict"
    $fixture.volume.Remove("$($fixtureProject)_research")
    Write-File $archivePath "damaged"
    Require-Failure { Run-Desktop "install" } "desktop_archive_checksum"
    Require ($fixture.loads -eq 0 -and !$fixture.composeArgs) "An invalid archive reached Docker image loading."
    Write-File $archivePath "test-image-archive"

    Run-Desktop "install" @{"--port" = "18198"}
    $unconfigured = Run-Desktop "source-status" | ConvertFrom-Json
    Require ($unconfigured.capabilities.daily -ceq "credentials_missing") "An empty instance must report the missing Tushare credential."
    $fixtureSourceToken = "tushare-fixture-not-a-real-token"
    foreach ($check in @("invalid_credentials", "permission_denied", "quota_exhausted", "connection_failed", "available")) {
        $fixture.sourceCode = $check
        $inputData = [IO.MemoryStream]::new($Utf8.GetBytes($fixtureSourceToken))
        try { $report = Invoke-DesktopSource $true 18198 $inputData }
        finally { $inputData.Dispose() }
        Require ($fixture.sourceToken -ceq $fixtureSourceToken) "Tushare credential must travel via stdin."
        Require ($report.capabilities.daily -ceq $check) "An interface result was lost."
        Require ($report.saved -eq ($check -ceq 'available')) "Only verified setup can report a saved credential."
        Require (($report | ConvertTo-Json -Depth 4) -notmatch $fixtureSourceToken) "Tushare output leaked a credential."
    }
    Require ($report.restart -ceq "ready" -and $fixture.sourceRestarts -eq 1) "Only a saved credential should restart the services."
    $fixture.failRestart = $true
    $inputData = [IO.MemoryStream]::new($Utf8.GetBytes($fixtureSourceToken))
    try { $report = Invoke-DesktopSource $true 18198 $inputData }
    finally { $inputData.Dispose() }
    Require ($report.saved -and $report.restart -ceq "failed") "A restart failure must distinguish an already saved credential."
    $fixture.failRestart = $false
    Require-Failure { Invoke-DesktopSource $false 18199 } "desktop_gateway_unavailable"
    $issued = Run-Desktop "client-issue" @{"--name" = "report-reader"} | ConvertFrom-Json
    Require ($issued.connection.url -ceq "http://127.0.0.1:18198/mcp") "Generic connection must use the current verified port."
    Require (($issued.connection.scopes -join ",") -ceq "research.read") "Generic issuance must default to read-only."
    $readerToken = $issued.connection.credential
    Require (Test-Credential $readerToken) "Issued credential was not validated."
    $writer = Run-Desktop "client-issue" @{"--name" = "report-reader"; "--preset" = "topic-writer"} | ConvertFrom-Json
    Require (!(Test-Credential $readerToken)) "Rotation must revoke the former client credential."
    Require ("research.run" -cin $writer.connection.scopes) "Writer must include the selected research scopes."
    $statusText = Run-Desktop "status"
    $status = $statusText | ConvertFrom-Json
    Require ($status.generic_clients.Count -eq 1 -and $status.generic_clients[0].name -ceq "report-reader") "List the named client independently."
    Require (!$statusText.Contains($writer.connection.credential)) "Generic credential leaked into regular status."
    Require-Failure { Run-Desktop "client-revoke" @{"--name" = "../other"} } "desktop_invalid_arguments"
    Run-Desktop "client-revoke" @{"--name" = "report-reader"}
    Require (!(Test-Credential $writer.connection.credential)) "Explicit revoke must disable the selected client."

    $profilePath = Join-Path $env:DSH_HOME "profiles/desk-test/cordis.patch.yml"
    $otherDsh = "- id: skill-filesystem" + [Environment]::NewLine + "  disabled: true"
    Write-File $profilePath $otherDsh
    $Root = $instanceRoot
    $DesktopProject = $fixtureProject
    $Endpoint = "http://127.0.0.1:18198"
    $BridgeArgs = @("-NoProfile", "-File", (Join-Path $Root "mcp-bridge.ps1"), "-InstallRoot", $Root, "-Client", "dsh")
    $Options = @{"--profile" = "desk-test"}
    Install-Dsh
    $profileText = [IO.File]::ReadAllText($profilePath)
    Require ($profileText.Contains($otherDsh) -and $profileText.Contains("# BEGIN FINDASHBOARD $fixtureProject")) "DSH setup overwrote an unrelated setting."
    Install-Dsh
    Require ([IO.File]::ReadAllText($profilePath) -ceq $profileText) "DSH setup must be repeatable."
    $replacementPatch = Join-Path $env:DSH_HOME "profiles/desk-new/cordis.patch.yml"
    Write-File $replacementPatch $otherDsh
    $dshCredential = Join-Path $instanceRoot "dsh.json"
    $Client = "dsh"
    $Options = @{"--profile" = "desk-new"; "--preset" = "readonly"}
    Write-File $dshCredential (@{ client_id = "MC-fixture"; sealed = "fixture"; preset = "readonly"; profile = "desk-test" } | ConvertTo-Json)
    $fixture.failWritePath = $profilePath
    Require-Failure { Set-ClientCredential } "fixture_profile_write_failed"
    $pending = [IO.File]::ReadAllText($dshCredential) | ConvertFrom-Json
    Require ($pending.profile -ceq "desk-new" -and $pending.pending_remove_profile -ceq "desk-test") "A failed DSH profile cleanup must retain its old profile identity."
    Require ((Get-DesktopHarnessStatus "dsh" $false).repair_required) "The desktop status must mark a pending DSH cleanup."
    Require ([IO.File]::ReadAllText($profilePath) -ceq $profileText) "A failed cleanup must keep the existing DSH profile untouched."
    $fixture.failWritePath = $null
    Set-ClientCredential
    $state = [IO.File]::ReadAllText($dshCredential) | ConvertFrom-Json
    Require (!$state.pending_remove_profile) "Retry must finish the saved DSH cleanup."
    Require ([IO.File]::ReadAllText($profilePath).TrimEnd() -ceq $otherDsh) "Retry must preserve DSH settings outside this installation."
    [IO.File]::Delete($dshCredential)
    $workspace = Run-Desktop "client-workspace" @{"--client" = "dsh"; "--mode" = "location"} | ConvertFrom-Json
    Require ($workspace.workspace -ceq (Join-Path $instanceRoot "workspace")) "DSH workspace must be owned by the current instance."
    Run-Desktop "attach" @{"--client" = "dsh"; "--preset" = "readonly"; "--profile" = "desk-test"}
    Run-Desktop "detach" @{"--client" = "dsh"}
    Require ([IO.File]::ReadAllText($profilePath).TrimEnd() -ceq $otherDsh) "Revocation must only remove the active instance's DSH configuration."
    $versionShim = Join-Path $fixtureRoot "bin/opencode.cmd"
    $openCodePath = Join-Path $instanceRoot "opencode-workspace/opencode.jsonc"
    $previousPath = $env:PATH
    try {
        $env:PATH = (Split-Path -Parent $versionShim) + ";" + $previousPath
        Write-File $versionShim "@echo off`r`necho 1.9.0`r`n"
        Install-OpenCode
        Require (Test-DesktopManagedOpenCode $openCodePath) "OpenCode 1 should get a scoped project configuration."
        Write-File $versionShim "@echo off`r`necho 2.0.0`r`n"
        Install-OpenCode
        $openCodeConfig = (Get-Content -LiteralPath $openCodePath | Select-Object -Skip 1) -join "`n" | ConvertFrom-Json
        Require ($openCodeConfig.mcp.servers.$fixtureProject.type -ceq "local") "OpenCode 2 needs an instance-specific stdio server."
        Write-File $openCodePath "// FinDashboard Desktop $fixtureProject`r`n{""model"":""custom""}"
        Require-Failure { Install-OpenCode } "desktop_opencode_config"
    } finally { $env:PATH = $previousPath }
    $state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
    Require ($state.port -eq 18198 -and $fixture.loads -eq 1) "The first install did not commit the expected port and image load."
    $savedOwner = $state.instance_id
    Run-Desktop "install"
    Require ($fixture.loads -eq 1) "A regular restart must reuse verified images."
    $fixture.portBusy = $true
    Require-Failure { Run-Desktop "install" @{"--port" = "18199"} } "desktop_port_in_use"
    Require ((Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json).port -eq 18198) "The failed port was committed."
    $fixture.portBusy = $false
    $fixture.images[$manifest.images[0].tag] = "sha256:" + ("c" * 64)
    Require-Failure { Run-Desktop "install" } "desktop_image_conflict"
    $fixture.images[$manifest.images[0].tag] = $manifest.images[0].compatible_ids[0]
    Run-Desktop "install"
    Require ($fixture.loads -eq 1) "The same archived image on another Docker storage engine should be reused."

    $receipt = Run-Desktop "import" | ConvertFrom-Json
    Require ($receipt.release_id -ceq "synthetic-starter-v1" -and $fixture.operator[-1] -ceq "synthetic-bundle") "The included starter was not imported."
    $fixture.port = 18199
    Require-Failure { Run-Desktop "attach" @{"--preset" = "readonly"} } "desktop_gateway_unavailable"
    $fixture.port = 18198
    Run-Desktop "attach" @{"--preset" = "readonly"}
    Run-Desktop "attach" @{"--preset" = "topic-writer"}
    $status = Run-Desktop "status" | ConvertFrom-Json
    Require ($status.running -and $status.credential_active -and $status.preset -ceq "topic-writer" -and
        "research.run" -cin $status.scopes) "The live credential status did not expose its actual preset."
    Run-Desktop "stop"
    Require (!(Run-Desktop "status" | ConvertFrom-Json).running) "A stopped project is reported running."

    $fixture.foreignMarketplace = $true
    Require-Failure { Run-Desktop "uninstall" @{"--mode" = "preserve"} } "desktop_codex_conflict"
    Require ($fixture.downs -eq 0 -and (Test-Path -LiteralPath $credentialFile)) "Foreign Codex ownership was ignored."
    $fixture.foreignMarketplace = $false
    $fixture.sharedTag = $manifest.images[1].tag
    Run-Desktop "uninstall" @{"--mode" = "preserve"}
    Require (!$fixture.marketplace -and !$fixture.installedPlugin -and !(Test-Path -LiteralPath $credentialFile)) "The original Codex access remains."
    Require ($fixture.volume.ContainsKey("$($fixtureProject)_postgres") -and $fixture.images.ContainsKey($fixture.sharedTag)) "Preserve deleted protected data or a shared Docker image."
    Require (!$fixture.container.ContainsKey("$fixtureProject-gateway-1") -and $fixture.container.ContainsKey("$secondProject-gateway-1")) "Preserve affected the wrong Docker project."
    $status = Run-Desktop "status" | ConvertFrom-Json
    Require ($status.has_instance -and $status.phase -ceq "retained" -and !$status.installed) "Retained data cannot be managed from status."
    $pgVolumeName = "$($fixtureProject)_postgres"
    $originalVolume = $fixture.volume[$pgVolumeName]
    $fixture.volume.Remove($pgVolumeName)
    $operationsBeforeMissingVolume = $fixture.composeArgs.Count
    Require-Failure { Run-Desktop "install" } "desktop_missing_volume"
    Require ($fixture.composeArgs.Count -eq $operationsBeforeMissingVolume) "A missing database silently started Compose."
    $fixture.volume[$pgVolumeName] = $originalVolume

    Run-Desktop "install"
    $state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
    Require ($state.instance_id -ceq $savedOwner -and $state.port -eq 18198) "Reinstall failed to restore the retained identity."
    $legacy = @{tag = "finboard-research-desktop/app:old-preview"; id = "sha256:" + ("e" * 64)}
    $state.owned_images = @($state.owned_images) + @($legacy)
    Write-File $stateFile ($state | ConvertTo-Json -Depth 8 -Compress)
    $fixture.images[$legacy.tag] = "sha256:" + ("f" * 64)
    $fixture.failVolume = $true
    Require-Failure { Run-Desktop "uninstall" @{"--mode" = "purge"; "--confirm" = "DELETE-THIS-INSTANCE"} } "desktop_cleanup_pending"
    Require ((Test-Path -LiteralPath $stateFile) -and $fixture.volume.ContainsKey("$($fixtureProject)_research")) "An interrupted purge lost its retry state."
    $status = Run-Desktop "status" | ConvertFrom-Json
    Require ($status.has_instance -and $status.phase -ceq "purging") "Interrupted purge is not visible in status."
    Require-Failure { Run-Desktop "install" } "desktop_cleanup_pending"
    Require-Failure { Run-Desktop "uninstall" @{"--mode" = "preserve"} } "desktop_cleanup_pending"
    $fixture.failVolume = $false
    Run-Desktop "uninstall" @{"--mode" = "purge"; "--confirm" = "DELETE-THIS-INSTANCE"}
    Require (!(Test-Path -LiteralPath $stateFile) -and !$fixture.volume.ContainsKey("$($fixtureProject)_research") -and
        $fixture.volume.ContainsKey("$($secondProject)_research")) "Purge did not obey instance ownership."
    Require ($fixture.images.ContainsKey($legacy.tag)) "An old tag with unverified identity was removed."
    Write-Host "Desktop host faults, offline installation and ownership checks passed."
} finally {
    $env:DSH_HOME = $previousDshHome
    $cwd = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "../.tmp"))
    $resolved = [IO.Path]::GetFullPath($fixtureRoot)
    if ($resolved.StartsWith($cwd.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath $resolved)) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
