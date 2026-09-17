$ErrorActionPreference = 'Stop'
if (-not $env:AIGW_TOKEN) {
    Write-Warning 'AIGW_TOKEN is not set. The gateway will accept unauthenticated local requests.'
}
aigw serve @args
