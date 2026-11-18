param(
    [Parameter(Mandatory=$true)][string]$InstallRoot,
    [Parameter(Mandatory=$true)][ValidateSet("codex", "opencode", "dsh")][string]$Client,
    [switch]$PrintToken
)
$ErrorActionPreference = "Stop"
try {
    $credential = Get-Content -LiteralPath (Join-Path $InstallRoot "$Client.json") -Raw | ConvertFrom-Json
    Add-Type -AssemblyName System.Security
    $bytes = [Security.Cryptography.ProtectedData]::Unprotect(
        [Convert]::FromBase64String($credential.sealed), $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
    $token = [Text.Encoding]::UTF8.GetString($bytes)
    if ($PrintToken) {
        [Console]::Out.Write($token)
        exit 0
    }
    $state = Get-Content -LiteralPath (Join-Path $InstallRoot "state.json") -Raw | ConvertFrom-Json
    $endpoint = "http://127.0.0.1:$([int]$state.port)/mcp"
    Add-Type -AssemblyName System.Net.Http
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.AllowAutoRedirect = $false
    $handler.UseProxy = $false
    $http = New-Object System.Net.Http.HttpClient($handler)
    $http.Timeout = [TimeSpan]::FromSeconds(360)
    $http.DefaultRequestHeaders.Add("Authorization", "Bearer $token")
    $http.DefaultRequestHeaders.Add("Accept", "application/json, text/event-stream")
    $http.DefaultRequestHeaders.Add("MCP-Protocol-Version", "2025-06-18")
    [Console]::InputEncoding = New-Object Text.UTF8Encoding($false)
    [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
    while ($null -ne ($line = [Console]::ReadLine())) {
        $request = $line | ConvertFrom-Json
        $content = New-Object System.Net.Http.StringContent($line, [Text.Encoding]::UTF8, "application/json")
        try {
            $response = $http.PostAsync($endpoint, $content).GetAwaiter().GetResult()
            try {
                if ($response.IsSuccessStatusCode) {
                    $result = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
                    if ($result) { [Console]::WriteLine($result) }
                    continue
                }
                $failure = "MCP HTTP " + [int]$response.StatusCode + ". Run finboard setup again."
            } finally { $response.Dispose() }
        } catch {
            $failure = "MCP service unreachable. Run finboard start or finboard status."
        } finally { $content.Dispose() }
        if ($null -ne $request.id) {
            $errorReply = @{
                jsonrpc = "2.0"; id = $request.id; error = @{code = -32000; message = $failure}
            } | ConvertTo-Json -Depth 5 -Compress
            [Console]::WriteLine($errorReply)
        }
    }
    $http.Dispose()
} catch {
    [Console]::Error.WriteLine("FinDashboard credential unavailable. Run finboard setup again.")
    exit 1
}
