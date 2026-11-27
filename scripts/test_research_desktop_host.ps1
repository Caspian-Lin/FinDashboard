$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "../docker/research/desktop-host.ps1")

$fixtureProject = "finboard-research-desktop-test-" + [Guid]::NewGuid().ToString("N").Substring(0, 12)
$fixtureRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "../.tmp/$fixtureProject"))
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

function Write-File([string]$Path, [string]$Value) { [IO.File]::WriteAllText($Path, $Value, $Utf8) }

function Find-DesktopDocker { return "fixture-docker.exe" }

function Initialize-State {
    if (!(Test-Path -LiteralPath $env:FINBOARD_DB_SECRET_FILE)) {
        Write-File $env:FINBOARD_DB_SECRET_FILE "isolated-fixture"
    }
}

function Configure-Client {
    Write-File $DesktopCredentialPath (@{sealed = "fixture"; preset = $Options["--preset"]} | ConvertTo-Json)
}

function Get-Plaintext([string]$Sealed) { return $Sealed }
function Test-Credential([string]$Token) { return ($Token -ceq "fixture") }
function Revoke-Client {
    Require $DesktopDetachedOperator "Revocation must work independently of the stopped gateway."
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
            @{tag = "finboard-research-desktop/app:test"; id = "sha256:" + ("a" * 64)},
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
    $fixture.images[$manifest.images[0].tag] = $manifest.images[0].id

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
    Write-Host "Desktop host faults, offline installation and ownership checks passed."
} finally {
    $cwd = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "../.tmp"))
    $resolved = [IO.Path]::GetFullPath($fixtureRoot)
    if ($resolved.StartsWith($cwd.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath $resolved)) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
