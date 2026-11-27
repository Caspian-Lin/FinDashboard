$ErrorActionPreference = "Stop"
$Action = $args | Select-Object -First 1
$DesktopHostScript = Join-Path $PSScriptRoot "desktop-host.ps1"
$Options = @{}
for ($i = 1; $i -lt $args.Count; $i += 2) {
    if ($args[$i] -notin @("--client", "--preset", "--port", "--profile", "--bundle", "--mode", "--confirm", "--name") -or
        $i + 1 -ge $args.Count -or $Options.ContainsKey($args[$i])) {
        throw "Invalid arguments. See finboard help."
    }
    $Options[$args[$i]] = $args[$i + 1]
}
if (!$Action -or $Action -in @("help", "--help")) {
    Write-Host @"
finboard setup --client codex|dsh|opencode [--preset readonly|topic-writer] [--port 8176] [--profile desktop]
finboard start | stop | status
finboard import --bundle synthetic-starter-v1.zip
finboard source-configure | source-status
finboard revoke --client codex|dsh|opencode
"@
    if (Test-Path -LiteralPath $DesktopHostScript -PathType Leaf) {
        Write-Host @"
finboard desktop-status | desktop-install [--port 8276] | desktop-stop | desktop-import
finboard desktop-attach --client codex|dsh|opencode --preset readonly|topic-writer [--profile desktop]
finboard desktop-detach --client codex|dsh|opencode
finboard desktop-client-issue --name NAME [--preset readonly|topic-writer]
finboard desktop-client-revoke --name NAME
finboard desktop-uninstall --mode preserve|purge [--confirm DELETE-THIS-INSTANCE]
"@
    }
    exit 0
}

$DesktopAction = $Action -like "desktop-*"
$Utf8 = New-Object Text.UTF8Encoding($false)
$Compose = Join-Path $PSScriptRoot "compose.yaml"

function Write-File([string]$Path, [string]$Value) {
    $directory = Split-Path -Parent $Path
    [void][IO.Directory]::CreateDirectory($directory)
    $temporary = Join-Path $directory ([Guid]::NewGuid().ToString("N") + ".tmp")
    [IO.File]::WriteAllText($temporary, $Value, $Utf8)
    try {
        if ([IO.File]::Exists($Path)) {
            [IO.File]::Replace($temporary, $Path, [System.Management.Automation.Language.NullString]::Value)
        }
        else { [IO.File]::Move($temporary, $Path) }
    } finally {
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
    }
}

function New-RandomBytes([int]$Length) {
    $array = New-Object byte[] $Length
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($array) } finally { $rng.Dispose() }
    return ,$array
}

function To-Hex([byte[]]$Bytes) {
    return [BitConverter]::ToString($Bytes).Replace("-", "").ToLowerInvariant()
}

function Invoke-Stack {
    & docker compose --file $Compose @args
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose failed. Check Docker Desktop and the port above, or run finboard status."
    }
}

function Invoke-Operator([string]$Command, [string]$InputText) {
    if ($DesktopAction) {
        $stream = New-Object IO.MemoryStream(,($Utf8.GetBytes($InputText + [Environment]::NewLine)))
        try { return Invoke-DesktopOperator $Command $stream }
        finally { $stream.Dispose() }
    }
    $process = Start-Operator $Command $true
    $data = $Utf8.GetBytes($InputText + [Environment]::NewLine)
    try { $process.StandardInput.BaseStream.Write($data, 0, $data.Length) }
    finally { $process.StandardInput.Close() }
    $result = $process.StandardOutput.ReadToEnd()
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) { throw "FinDashboard operator $Command failed. $result" }
    return ($result | ConvertFrom-Json)
}

function Initialize-State {
    [void][IO.Directory]::CreateDirectory($Root)
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls.exe $Root /inheritance:r /grant:r "${identity}:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Cannot protect the FinDashboard state folder." }
    if (!(Test-Path -LiteralPath $env:FINBOARD_DB_SECRET_FILE)) {
        Write-File $env:FINBOARD_DB_SECRET_FILE (To-Hex (New-RandomBytes 32))
    }
    Write-File $Bridge ([IO.File]::ReadAllText((Join-Path $PSScriptRoot "mcp-bridge.ps1")))
}

function Start-Operator([string]$Command, [bool]$CaptureOutput) {
    [Console]::InputEncoding = $Utf8
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = (Get-Command docker.exe).Source
    $info.Arguments = 'compose --file "{0}" exec -T gateway python /opt/finboard/entrypoint.py operator {1}' -f $Compose, $Command
    $info.UseShellExecute = $false
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $CaptureOutput
    return [Diagnostics.Process]::Start($info)
}

function Import-Bundle([string]$Bundle) {
    $archive = (Resolve-Path -LiteralPath $Bundle).Path
    $process = Start-Operator "import" $false
    $inputFile = [IO.File]::OpenRead($archive)
    try { $inputFile.CopyTo($process.StandardInput.BaseStream) }
    finally { $inputFile.Dispose(); $process.StandardInput.Close() }
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) { throw "Starter package failed validation or conflicts with an installed version." }
}

. (Join-Path $PSScriptRoot "installer-clients.ps1")
if ($DesktopAction) {
    try {
        if (!(Test-Path -LiteralPath $DesktopHostScript -PathType Leaf)) {
            throw "desktop_offline_package_required: Run this command from the offline Desktop package."
        }
        . $DesktopHostScript
        Invoke-DesktopAction
    }
    catch {
        $failure = $_.Exception.Message
        if ($failure -notmatch '^desktop_[a-z_]+: ') {
            $failure = "desktop_operation_failed: Check Desktop status, Docker Desktop and Codex, then retry."
        }
        [Console]::Error.WriteLine($failure)
        exit 1
    }
    return
}

$Root = Join-Path $env:LOCALAPPDATA "FinDashboard"
if ($env:FINBOARD_INSTALL_ROOT) { $Root = [IO.Path]::GetFullPath($env:FINBOARD_INSTALL_ROOT) }
$Client = $Options["--client"]
$StatePath = Join-Path $Root "state.json"
$State = if (Test-Path -LiteralPath $StatePath) {
    Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
} else { [PSCustomObject]@{port = 8176} }
$Bridge = Join-Path $Root "mcp-bridge.ps1"

if (!(Get-Command docker.exe -ErrorAction SilentlyContinue)) { throw "Install Docker Desktop and reopen the terminal." }
$dockerType = & docker info --format "{{.OSType}}" 2>$null
if ($LASTEXITCODE -ne 0 -or $dockerType -ne "linux") { throw "Start Docker Desktop in Linux containers mode and retry." }
if (!$env:FINBOARD_IMAGE) { $env:FINBOARD_IMAGE = ([IO.File]::ReadAllText((Join-Path $PSScriptRoot "image.txt"))).Trim() }
$env:FINBOARD_DB_SECRET_FILE = Join-Path $Root "postgres-password"
if ($Options["--port"]) {
    if ($Options["--port"] -notmatch "^\d{1,5}$" -or [int]$Options["--port"] -lt 1024 -or [int]$Options["--port"] -gt 65535) {
        throw "Invalid port."
    }
    $State.port = [int]$Options["--port"]
}
$env:FINBOARD_PORT = [string]$State.port
$Endpoint = "http://127.0.0.1:$env:FINBOARD_PORT"
$BridgeArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $Bridge, "-InstallRoot", $Root, "-Client", $Client)
if ($Action -in @("setup", "revoke") -and $Client -notin @("codex", "opencode", "dsh")) { throw "Specify --client codex, dsh, or opencode." }
if ($Action -ne "setup" -and !(Test-Path -LiteralPath $StatePath)) { throw "Run finboard setup first." }
switch ($Action) {
    "setup" {
        if ($Client -ne "dsh" -and !(Get-Command $Client -ErrorAction SilentlyContinue)) {
            throw "Install the $Client CLI and reopen the terminal."
        }
        Initialize-State
        if ($env:FINBOARD_IMAGE -ne "finboard-research:local") { Invoke-Stack pull }
        Invoke-Stack up -d --wait
        Write-File $StatePath ($State | ConvertTo-Json -Compress)
        Configure-Client
    }
    "start" { Invoke-Stack up -d --wait; Write-Host $Endpoint }
    "stop" { Invoke-Stack stop }
    "status" { Invoke-Stack ps --all }
    "import" {
        if (!$Options["--bundle"]) { throw "Specify --bundle synthetic-starter-v1.zip." }
        Import-Bundle $Options["--bundle"]
    }
    "source-configure" {
        $secure = Read-Host "Tushare token" -AsSecureString
        $handle = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($handle) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($handle) }
        Invoke-Operator "source-configure" $token | ConvertTo-Json -Depth 6
        Invoke-Stack restart gateway worker
        Invoke-Stack up -d --wait
    }
    "source-status" { Invoke-Operator "source-status" "" | ConvertTo-Json -Depth 6 }
    "revoke" { Revoke-Client }
    default { throw "Unknown command. See finboard help." }
}
