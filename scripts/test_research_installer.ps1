$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "../docker/research/installer-clients.ps1")

$Root = Join-Path $PSScriptRoot ("../.tmp/installer-" + [Guid]::NewGuid().ToString("N"))
$Root = [IO.Path]::GetFullPath($Root)
[void][IO.Directory]::CreateDirectory($Root)
$Client = "codex"
$Options = @{"--preset" = "readonly"}
$Endpoint = "http://127.0.0.1:18176"
$Utf8 = New-Object Text.UTF8Encoding($false)
$path = Join-Path $Root "$Client.json"
$operations = [Collections.Generic.List[string]]::new()
$installFails = $true
$revokeFails = $false

function Write-File([string]$Path, [string]$Value) {
    [IO.File]::WriteAllText($Path, $Value, $Utf8)
}

function New-RandomBytes([int]$Length) {
    return ,([byte[]](1..$Length))
}

function To-Hex([byte[]]$Bytes) {
    return [BitConverter]::ToString($Bytes).Replace("-", "").ToLowerInvariant()
}

function Install-Codex {
    $operations.Add("install")
    if ($installFails) { throw "injected installation failure" }
}

function Test-Credential([string]$Token) { return $true }
function Get-Plaintext([string]$Sealed) { return "fixture-token" }

function Invoke-Operator([string]$Command, [string]$InputText) {
    $request = $InputText | ConvertFrom-Json
    $operations.Add("$Command/$($request.client_id)")
    if ($Command -eq "client-revoke" -and $revokeFails) {
        throw "injected revocation failure"
    }
}

function Require-Error([scriptblock]$Operation, [string]$Message) {
    try { & $Operation }
    catch {
        if ($_.Exception.Message -ne $Message) { throw }
        return
    }
    throw "Expected $Message"
}

try {
    $original = '{"client_id":"MC-fixture-1","preset":"topic-writer","sealed":"fixture"}'
    Write-File $path $original
    Require-Error { Configure-Client } "injected installation failure"
    if ([IO.File]::ReadAllText($path) -ne $original -or ($operations -join ",") -ne "install") {
        throw "Client registration failure must preserve the existing credential."
    }

    $installFails = $false
    $revokeFails = $true
    Require-Error { Configure-Client } "injected revocation failure"
    $pending = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    if ($pending.preset -ne "readonly" -or $pending.pending_revoke_id -ne "MC-fixture-1") {
        throw "An interrupted downgrade must retain the original client ID for revocation."
    }

    $revokeFails = $false
    $operations.Clear()
    Configure-Client
    $completed = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    if ($completed.client_id -ne $pending.client_id -or $completed.pending_revoke_id -or
        ($operations -join ",") -ne "client-revoke/MC-fixture-1,install") {
        throw "Repeating setup must complete the revocation without issuing another credential."
    }

    $operations.Clear()
    $completed | Add-Member -NotePropertyName pending_revoke_id -NotePropertyValue "MC-fixture-2"
    Write-File $path ($completed | ConvertTo-Json -Compress)
    Revoke-Client
    if ((Test-Path -LiteralPath $path) -or
        ($operations -join ",") -ne "client-revoke/MC-fixture-2,client-revoke/$($completed.client_id)") {
        throw "Revoke must remove both the active and the pending credential."
    }
    Write-Host "Research installer credential failure recovery passed."

    & {
        function docker.exe { throw "The installer test must use the Docker stub." }
        function docker {
            $global:LASTEXITCODE = 0
            if ($args[0] -eq "info") { return "linux" }
            if ($args[0] -ne "compose") { throw "Unexpected Docker command." }
            if ($args[3] -eq $failedStep) { $global:LASTEXITCODE = 1 }
        }

        $previousExitCode = $global:LASTEXITCODE
        $previousEnvironment = @{}
        foreach ($name in @("FINBOARD_INSTALL_ROOT", "FINBOARD_IMAGE", "FINBOARD_DB_SECRET_FILE", "FINBOARD_PORT")) {
            $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name)
        }
        try {
            $env:FINBOARD_INSTALL_ROOT = $Root
            $env:FINBOARD_IMAGE = "finboard-research:fixture"
            $installer = Join-Path $PSScriptRoot "../docker/research/finboard.ps1"
            $state = Join-Path $Root "state.json"
            Write-File $state '{"port":18176}'
            $message = "Docker Compose failed. Check Docker Desktop and the port above, or run finboard status."
            foreach ($failedStep in @("pull", "up")) {
                Require-Error { & $installer setup --client dsh --port 18198 } $message
                if ((Get-Content -LiteralPath $state -Raw | ConvertFrom-Json).port -ne 18176) {
                    throw "A failed Docker Compose $failedStep must preserve the previous port."
                }
            }
            Write-Host "Research installer port failure recovery passed."
        } finally {
            foreach ($name in $previousEnvironment.Keys) {
                [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name])
            }
            foreach ($name in @("state.json", "postgres-password", "mcp-bridge.ps1")) {
                [IO.File]::Delete((Join-Path $Root $name))
            }
            # GitHub Actions' PowerShell wrapper propagates the last native exit code.
            if ($null -eq $previousExitCode) {
                Remove-Variable -Name LASTEXITCODE -Scope Global -ErrorAction SilentlyContinue
            } else {
                $global:LASTEXITCODE = $previousExitCode
            }
        }
    }
} finally {
    [IO.File]::Delete($path)
    [IO.File]::Delete((Join-Path $Root "$Client.lock"))
    [IO.Directory]::Delete($Root)
}
