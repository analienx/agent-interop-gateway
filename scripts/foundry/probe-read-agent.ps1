param(
    [string]$AgentDir = ''
)

$ErrorActionPreference = 'Stop'

if (-not (Get-Command pi -ErrorAction SilentlyContinue)) {
    [Console]::Error.WriteLine('Pi is not installed or not available on PATH.')
    exit 3
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    [Console]::Error.WriteLine('Node.js is not installed or not available on PATH.')
    exit 4
}

$extension = Join-Path $PSScriptRoot 'pi-read-extension.mjs'
$probe = Join-Path $PSScriptRoot 'probe.mjs'
if (-not (Test-Path -LiteralPath $extension) -or -not (Test-Path -LiteralPath $probe)) {
    [Console]::Error.WriteLine('Foundry read adapter files are incomplete.')
    exit 5
}

if ([string]::IsNullOrWhiteSpace($AgentDir)) {
    $AgentDir = if ($env:AIGW_PI_AGENT_DIR) { $env:AIGW_PI_AGENT_DIR } else { Join-Path $HOME '.pi\supervisor-accounts\account-3\agent' }
}
if (-not (Test-Path -LiteralPath $AgentDir)) {
    [Console]::Error.WriteLine("Pi account profile does not exist: $AgentDir")
    exit 6
}
if (-not $env:AIGW_FOUNDRY_MCP_STDIO) {
    $env:AIGW_FOUNDRY_MCP_STDIO = 'C:\ProgramData\Analienx\mcp-gateway\bin\mcp-stdio.cmd'
}
if (-not (Test-Path -LiteralPath $env:AIGW_FOUNDRY_MCP_STDIO)) {
    [Console]::Error.WriteLine("Foundry MCP stdio launcher does not exist: $env:AIGW_FOUNDRY_MCP_STDIO")
    exit 7
}

& node $probe
exit $LASTEXITCODE
