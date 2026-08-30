# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Maintenance — Publish-Production (Publish-Production.ps1)
# Date: 2025-12-10
# ---------------------------------------------------------------------------
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string[]]$ChangedFiles,

    # Rebuild every frontend and restart every watchdog-managed runtime. This is
    # used by the automatic change publisher so a shared/root-level change cannot
    # leave one of the applications running an older version.
    [switch]$PublishAll
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogFile = Join-Path $PSScriptRoot 'production-publish.log'

# Function: Write-PublishLog
function Write-PublishLog {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') | $Message"
    Add-Content -LiteralPath $LogFile -Value $line
    Write-Host $line
}

# Function: Test-ChangedPath
function Test-ChangedPath {
    param([string]$Prefix)
    return [bool]($ChangedFiles | Where-Object {
        $_.Replace('\', '/').StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase)
    } | Select-Object -First 1)
}

# Function: Sync-NovastraPortalAuthSecret
function Sync-NovastraPortalAuthSecret {
    $portalEnvPath = Join-Path $RepoRoot 'AppRationalization\backend\.env'
    $novastraEnvPath = Join-Path $RepoRoot 'Novastra-ITSM\.env'
    if (-not (Test-Path -LiteralPath $portalEnvPath)) {
        Write-PublishLog "Portal .env not present; retaining the watchdog/service-managed Novastra SSO secret."
        return
    }
    if (-not (Test-Path -LiteralPath $novastraEnvPath)) {
        Write-PublishLog "Novastra .env not present; retaining the watchdog/service-managed SSO secret."
        return
    }

    $portalSecretLine = Get-Content -LiteralPath $portalEnvPath |
        Where-Object { $_ -match '^AUTH_TOKEN_SECRET=' } |
        Select-Object -First 1
    if (-not $portalSecretLine) {
        throw 'AUTH_TOKEN_SECRET is missing from the portal environment.'
    }

    $portalSecret = $portalSecretLine.Substring('AUTH_TOKEN_SECRET='.Length)
    if ([string]::IsNullOrWhiteSpace($portalSecret)) {
        throw 'AUTH_TOKEN_SECRET is empty in the portal environment.'
    }

    $novastraLines = @(Get-Content -LiteralPath $novastraEnvPath)
    $setting = "PORTAL_AUTH_TOKEN_SECRET=$portalSecret"
    $found = $false
    for ($index = 0; $index -lt $novastraLines.Count; $index++) {
        if ($novastraLines[$index] -match '^PORTAL_AUTH_TOKEN_SECRET=') {
            $novastraLines[$index] = $setting
            $found = $true
        }
    }
    if (-not $found) {
        $novastraLines += $setting
    }
    Set-Content -LiteralPath $novastraEnvPath -Value $novastraLines -Encoding UTF8
    Write-PublishLog 'Synchronized the Novastra portal SSO verification secret.'
}

# Function: Wait-BackendPort
function Wait-BackendPort {
    param(
        [string]$Name,
        [int]$Port,
        # 50s was observed too tight under sustained load (six frontends rebuilding
        # + nine backends restarting every cycle during a large backlog catch-up on
        # 2026-07-24) - Dashboard alone needed ~80s several times, causing the whole
        # publish to fail and retry the same growing backlog every 5 minutes for ~2h.
        [int]$TimeoutSeconds = 120
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($listener) {
            Write-PublishLog "$Name is listening on port $Port after restart."
            return
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw "$Name did not resume listening on port $Port within $TimeoutSeconds seconds."
}

# Function: Request-BackendRestart
function Request-BackendRestart {
    param([hashtable]$Backend)
    $requestDir = Join-Path $RepoRoot '.runtime\restart-requests'
    New-Item -ItemType Directory -Force -Path $requestDir | Out-Null
    $listener = Get-NetTCPConnection -LocalPort $Backend.Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    $Backend['PreviousPid'] = if ($listener) { [int]$listener.OwningProcess } else { 0 }
    $requestFile = Join-Path $requestDir "$($Backend.Name).request"
    Set-Content -LiteralPath $requestFile -Value (
        "publish=$(Get-Date -Format o); previous_pid=$($Backend.PreviousPid)"
    ) -Encoding ASCII
    Write-PublishLog "Requested watchdog restart for $($Backend.DisplayName) on port $($Backend.Port)."
}

# Function: Wait-BackendReplacement
function Wait-BackendReplacement {
    param(
        [hashtable]$Backend,
        [int]$TimeoutSeconds = 120
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $listener = Get-NetTCPConnection -LocalPort $Backend.Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
        $requestFile = Join-Path $RepoRoot ".runtime\restart-requests\$($Backend.Name).request"
        $isReplacement = $listener -and (
            $Backend.PreviousPid -eq 0 -or
            [int]$listener.OwningProcess -ne [int]$Backend.PreviousPid
        )
        if ($isReplacement -and -not (Test-Path -LiteralPath $requestFile)) {
            Write-PublishLog "$($Backend.DisplayName) replacement is listening on port $($Backend.Port)."
            return
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw "$($Backend.DisplayName) was not replaced on port $($Backend.Port) within $TimeoutSeconds seconds."
}

# Function: Wait-RestartRequestConsumed
function Wait-RestartRequestConsumed {
    param(
        [string]$ServiceName,
        [int]$TimeoutSeconds = 120
    )
    $requestFile = Join-Path $RepoRoot ".runtime\restart-requests\$ServiceName.request"
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Test-Path -LiteralPath $requestFile) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
    }
    if (Test-Path -LiteralPath $requestFile) {
        throw "$ServiceName restart request was not consumed within $TimeoutSeconds seconds."
    }
    Write-PublishLog "$ServiceName restart request was consumed by the production watchdog."
}

# Function: Invoke-TraceForgeMigrations
function Invoke-TraceForgeMigrations {
    $directory = Join-Path $RepoRoot 'TraceForge\api'
    $python = Join-Path $directory '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        throw "TraceForge Python was not found at $python"
    }

    Write-PublishLog 'Applying TraceForge database migrations.'
    Push-Location $directory
    $prevEap = $ErrorActionPreference
    try {
        # Native commands routinely write informational/warning text to stderr; under
        # ErrorActionPreference='Stop' PowerShell 5.1 treats each such line as a
        # terminating NativeCommandError before the real exit-code check below even
        # runs. Rely solely on $LASTEXITCODE for pass/fail, same as everywhere else
        # in this script.
        $ErrorActionPreference = 'Continue'
        & $python -m alembic upgrade head
        $ErrorActionPreference = $prevEap
        if ($LASTEXITCODE -ne 0) {
            throw "TraceForge database migration failed with exit code $LASTEXITCODE"
        }
    } finally {
        $ErrorActionPreference = $prevEap
        Pop-Location
    }
    Write-PublishLog 'TraceForge database migrations applied successfully.'
}

# Function: Ensure-ModernizationTypeScriptValidator
function Ensure-ModernizationTypeScriptValidator {
    $directory = Join-Path $RepoRoot 'Modernization\tools\ts-validate'
    $compilerPath = Join-Path $directory 'node_modules\typescript\lib\tsc.js'
    if (Test-Path -LiteralPath $compilerPath -PathType Leaf) { return }

    Write-PublishLog 'Modernization TypeScript validator is missing; restoring locked dependencies.'
    Push-Location -LiteralPath $directory
    $prevEap = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & npm ci --ignore-scripts
        $ErrorActionPreference = $prevEap
        if ($LASTEXITCODE -ne 0) {
            throw "Modernization TypeScript validator restore failed with exit code $LASTEXITCODE"
        }
    } finally {
        $ErrorActionPreference = $prevEap
        Pop-Location
    }
    if (-not (Test-Path -LiteralPath $compilerPath -PathType Leaf)) {
        throw "Modernization TypeScript validator was not installed at $compilerPath"
    }
    Write-PublishLog 'Modernization TypeScript validator restored successfully.'
}

$frontends = @(
    @{ Prefix = 'AppRationalization/frontend/'; Directory = 'AppRationalization/frontend'; Name = 'AppRationalization' },
    @{ Prefix = 'CodeAnalysis/frontend/'; Directory = 'CodeAnalysis/frontend'; Name = 'CodeAnalysis' },
    @{ Prefix = 'InfraRationalization/frontend/'; Directory = 'InfraRationalization/frontend'; Name = 'InfraRationalization' },
    @{ Prefix = 'Novastra-ITSM/frontend/'; Directory = 'Novastra-ITSM/frontend'; Name = 'Novastra-ITSM' },
    @{ Prefix = 'Dashboard/frontend/'; Directory = 'Dashboard/frontend'; Name = 'Dashboard' },
    @{ Prefix = 'SSDLC_Process_Assessment/frontend/'; Directory = 'SSDLC_Process_Assessment/frontend'; Name = 'SSDLC' },
    @{ Prefix = 'Modernization/frontend/'; Directory = 'Modernization/frontend'; Name = 'Modernization' },
    @{ Prefix = 'LabRobot/frontend/'; Directory = 'LabRobot/frontend'; Name = 'LabRobot' },
    @{ Prefix = 'OpportunityTracker/frontend/'; Directory = 'OpportunityTracker/frontend'; Name = 'OpportunityTracker' },
    @{ Prefix = 'AI_Reman_Core/'; Directory = 'AI_Reman_Core'; Name = 'AI_Reman_Core' },
    @{ Prefix = 'AI_Vehicle_Loan/frontend/'; Directory = 'AI_Vehicle_Loan/frontend'; Name = 'AI_Vehicle_Loan' },
    @{ Prefix = 'Microsite_Data_Analysis/'; Directory = 'Microsite_Data_Analysis'; Name = 'MicrositeDataAnalysis' },
    @{ Prefix = 'supply-chain-disruption-manager/apps/web-ui/'; Directory = 'supply-chain-disruption-manager/apps/web-ui'; Name = 'SupplyChain' },
    @{ Prefix = 'TraceForge/ui/'; Directory = 'TraceForge/ui'; Name = 'TraceForge' }
)

$backends = @(
    @{ Prefix = 'AppRationalization/backend/'; Name = 'AppRationalization'; DisplayName = 'AppRationalization'; Port = 5001 },
    @{ Prefix = 'CodeAnalysis/'; Name = 'CodeAnalysis'; DisplayName = 'CodeAnalysis'; Port = 8082 },
    @{ Prefix = 'InfraRationalization/'; Name = 'InfraRationalization'; DisplayName = 'InfraRationalization'; Port = 8083 },
    @{ Prefix = 'Modernization/'; Name = 'Modernization'; DisplayName = 'Modernization'; Port = 8084 },
    @{ Prefix = 'Novastra-ITSM/backend/'; Name = 'Novastra-ITSM'; DisplayName = 'Novastra-ITSM'; Port = 8086 },
    @{ Prefix = 'Dashboard/backend/'; Name = 'Dashboard'; DisplayName = 'Dashboard'; Port = 8087 },
    @{ Prefix = 'SSDLC_Process_Assessment/backend/'; Name = 'SSDLC'; DisplayName = 'SSDLC'; Port = 8091 },
    @{ Prefix = 'OpportunityTracker/backend/'; Name = 'OpportunityTracker'; DisplayName = 'OpportunityTracker'; Port = 8092 },
    @{ Prefix = 'LabRobot/backend/'; Name = 'LabRobot'; DisplayName = 'LabRobot'; Port = 8000 },
    @{ Prefix = 'LabRobot/mqtt-broker/'; Name = 'LabRobot-MqttBroker'; DisplayName = 'LabRobot MQTT broker'; Port = 1883 },
    @{ Prefix = 'AI_Reman_Core/backend/'; Name = 'AI_Reman_Core'; DisplayName = 'AI_Reman_Core'; Port = 8093 },
    @{ Prefix = 'AI_Vehicle_Loan/'; Name = 'AI_Vehicle_Loan'; DisplayName = 'AI_Vehicle_Loan'; Port = 8094 },
    @{ Prefix = 'TraceForge/api/'; Name = 'TraceForge-API'; DisplayName = 'TraceForge'; Port = 8095 },
    @{ Prefix = 'supply-chain-disruption-manager/services/kg-service/'; Name = 'SCM-KG-Service'; DisplayName = 'SCM KG service'; Port = 8001 },
    @{ Prefix = 'supply-chain-disruption-manager/services/agent-service/'; Name = 'SCM-Agent-Service'; DisplayName = 'SCM agent service'; Port = 8002 },
    @{ Prefix = 'supply-chain-disruption-manager/services/signal-inspector/'; Name = 'SCM-Signal-Inspector'; DisplayName = 'SCM signal inspector'; Port = 8003 },
    @{ Prefix = 'supply-chain-disruption-manager/'; Name = 'SCM-Redis'; DisplayName = 'SCM Redis'; Port = 6379 }
)

if ($PublishAll) {
    Write-PublishLog "Full publish requested after $($ChangedFiles.Count) changed file(s): rebuilding all frontends and restarting all services."
}

if (Test-ChangedPath 'Novastra-ITSM/backend/') {
    Sync-NovastraPortalAuthSecret
}

if ($PublishAll -or (Test-ChangedPath 'Modernization/')) {
    Ensure-ModernizationTypeScriptValidator
}

$builtAny = $false
foreach ($frontend in $frontends) {
    if (-not $PublishAll -and -not (Test-ChangedPath $frontend.Prefix)) { continue }
    $directory = Join-Path $RepoRoot $frontend.Directory
    if (-not (Test-Path -LiteralPath (Join-Path $directory 'package.json'))) { continue }
    Write-PublishLog "Building $($frontend.Name)."
    Push-Location $directory
    $prevEap = $ErrorActionPreference
    try {
        # See Invoke-TraceForgeMigrations for why this must not run under 'Stop' —
        # Vite's own build warnings (e.g. chunk-size) go to stderr and would otherwise
        # abort the build before the real $LASTEXITCODE check below runs.
        $ErrorActionPreference = 'Continue'
        & npm run build
        $ErrorActionPreference = $prevEap
        if ($LASTEXITCODE -ne 0) { throw "$($frontend.Name) build failed with exit code $LASTEXITCODE" }
        $builtAny = $true
        Write-PublishLog "Built $($frontend.Name) successfully."
    } finally {
        $ErrorActionPreference = $prevEap
        Pop-Location
    }
}

$restartTargets = @()
foreach ($backend in $backends) {
    if (-not $PublishAll -and -not (Test-ChangedPath $backend.Prefix)) { continue }
    $restartTargets += $backend
    Request-BackendRestart -Backend $backend

    if ($backend.Name -eq 'TraceForge-API') {
        Invoke-TraceForgeMigrations
        Write-PublishLog 'TraceForge restart is delegated to the shared-auth production watchdog.'
    }
}

foreach ($backend in $restartTargets) {
    Wait-BackendReplacement -Backend $backend
}

# TraceForge's Arq worker has no listening port. Restart it only for code imported
# by the worker; router/schema-only changes need an API restart but must not abort a
# long extraction that is already running.
$traceForgeWorkerPrefixes = @(
    'TraceForge/api/traceforge/workers/',
    'TraceForge/api/traceforge/agents/',
    'TraceForge/api/traceforge/llm/',
    'TraceForge/api/traceforge/indexing/',
    'TraceForge/api/traceforge/parsing/',
    'TraceForge/api/traceforge/connectors/',
    'TraceForge/api/traceforge/db/'
)
$restartTraceForgeWorker = $PublishAll -or
    (Test-ChangedPath 'TraceForge/api/traceforge/config.py') -or
    (Test-ChangedPath 'TraceForge/api/traceforge/orchestration/gates.py') -or
    [bool]($traceForgeWorkerPrefixes | Where-Object { Test-ChangedPath $_ } | Select-Object -First 1)
if ($restartTraceForgeWorker) {
    $requestDir = Join-Path $RepoRoot '.runtime\restart-requests'
    New-Item -ItemType Directory -Force -Path $requestDir | Out-Null
    Set-Content -LiteralPath (Join-Path $requestDir 'TraceForge-Worker.request') `
        -Value "publish=$(Get-Date -Format o)" -Encoding ASCII
    Write-PublishLog 'Requested watchdog restart for the TraceForge worker.'
    Wait-RestartRequestConsumed -ServiceName 'TraceForge-Worker'
}

if ($builtAny) {
    # Every frontend is served as static files. Recycling the shared application
    # pool after a build is unnecessary and causes Azure Relay requests to see a
    # transient 502 while IIS spins the pool back up. Vite writes hashed assets
    # and then replaces index.html, so the new build is visible without a recycle.
    Write-PublishLog 'Frontend assets published without recycling the shared IIS application pool.'
}

Write-PublishLog 'Production publish completed.'
