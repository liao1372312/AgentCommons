param(
    [string]$Port = "8765",
    [string]$ApiKey = ""
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is not installed or not available in PATH."
}

if (-not $ApiKey) {
    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $ApiKey = [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

$envFile = Join-Path $Root ".env.deploy"
@"
AGENTCOMMONS_PUBLIC_PORT=$Port
AGENTCOMMONS_API_KEY=$ApiKey
"@ | Set-Content -Path $envFile -Encoding UTF8

docker compose --env-file .env.deploy up -d --build

Write-Host ""
Write-Host "AgentCommons is starting."
Write-Host "Health: http://localhost:$Port/health"
Write-Host "MCP JSON-RPC endpoint: http://localhost:$Port/mcp"
Write-Host "API key: $ApiKey"
Write-Host ""
Write-Host "Use this header from agents:"
Write-Host "Authorization: Bearer $ApiKey"
