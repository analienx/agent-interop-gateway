param(
    [string]$AgentDir = '',
    [string]$Provider = '',
    [string]$Model = '',
    [ValidateSet('off','minimal','low','medium','high','xhigh','max')][string]$Thinking = 'low'
)

$ErrorActionPreference = 'Stop'
$task = [Console]::In.ReadToEnd().Trim()
if ([string]::IsNullOrWhiteSpace($task)) {
    [Console]::Error.WriteLine('AIGW Foundry read agent received an empty task.')
    exit 2
}

if (-not (Get-Command pi -ErrorAction SilentlyContinue)) {
    [Console]::Error.WriteLine('Pi is not installed or not available on PATH.')
    exit 3
}

$extension = Join-Path $PSScriptRoot 'pi-read-extension.mjs'
if (-not (Test-Path -LiteralPath $extension)) {
    [Console]::Error.WriteLine("Missing Foundry Pi extension: $extension")
    exit 4
}

if ([string]::IsNullOrWhiteSpace($AgentDir)) {
    $AgentDir = if ($env:AIGW_PI_AGENT_DIR) { $env:AIGW_PI_AGENT_DIR } else { Join-Path $HOME '.pi\supervisor-accounts\account-3\agent' }
}
if ([string]::IsNullOrWhiteSpace($Provider)) {
    $Provider = if ($env:AIGW_PI_PROVIDER) { $env:AIGW_PI_PROVIDER } else { 'cline' }
}
if ([string]::IsNullOrWhiteSpace($Model)) {
    $Model = if ($env:AIGW_PI_MODEL) { $env:AIGW_PI_MODEL } else { 'z-ai/glm-5.3-flash' }
}
if (-not (Test-Path -LiteralPath $AgentDir)) {
    [Console]::Error.WriteLine("Pi account profile does not exist: $AgentDir")
    exit 5
}

$env:PI_CODING_AGENT_DIR = $AgentDir
if (-not $env:AIGW_FOUNDRY_MCP_STDIO) {
    $env:AIGW_FOUNDRY_MCP_STDIO = 'C:\ProgramData\Analienx\mcp-gateway\bin\mcp-stdio.cmd'
}

$systemPrompt = @'
You are the read-only local inspection agent behind Agent Interop Gateway.
Use Foundry MCP tools for every claim about this machine or its repositories.
You have no authority to modify files, repositories, processes, or machine state.
Start with list_projects when the project id is unclear; otherwise prefer project_context.
Use search/read/repo-status tools only as needed, and snapshot/delta for follow-up inspection.
Return concise factual findings for an upstream ChatGPT conversation. Include project ids and paths when useful.
If Foundry cannot provide the requested evidence, say exactly what is unavailable instead of guessing.
'@
$piArgs = @(
    '--provider', $Provider,
    '--model', $Model,
    '--thinking', $Thinking,
    '--no-builtin-tools',
    '--extension', $extension,
    '--tools', 'foundry_status,list_projects,project_context,read_project_files,list_project_tree,search_project,project_repo_status,create_project_snapshot,read_project_delta',
    '--no-skills',
    '--no-prompt-templates',
    '--no-themes',
    '--no-context-files',
    '--no-session',
    '--system-prompt', $systemPrompt,
    '--print',
    '--', $task
)

& pi @piArgs
exit $LASTEXITCODE
