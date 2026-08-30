# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Start-AllServices.ps1 — Start-AllServices (Start-AllServices.ps1)
# Date: 2025-10-02
# ---------------------------------------------------------------------------
[CmdletBinding()]
param(
    [switch]$InstallDeps,
    [switch]$DryRun,
    [switch]$BuildFirst
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Function: Write-Step
function Write-Step {
    param([string]$Message)
    Write-Host "[start-all] $Message" -ForegroundColor Cyan
}

# Function: Assert-Command
function Assert-Command {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found in PATH."
    }
}

# Function: Ensure-Directory
function Ensure-Directory {
    param([string]$Path, [string]$Label)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "$Label directory not found: $Path"
    }
}

# Function: Invoke-InDirectory
function Invoke-InDirectory {
    param([string]$Directory, [scriptblock]$ScriptBlock)
    Push-Location -LiteralPath $Directory
    try {
        & $ScriptBlock
    } finally {
        Pop-Location
    }
}

# Function: Ensure-PythonEnv
function Ensure-PythonEnv {
    param([string]$ProjectDir, [switch]$ForceInstall, [switch]$IsDryRun)

    $venvPython = Join-Path $ProjectDir '.venv\Scripts\python.exe'
    if ($IsDryRun) {
        if (Test-Path -LiteralPath $venvPython) { return $venvPython }
        return 'python'
    }

    if (-not (Test-Path -LiteralPath $venvPython)) {
        Write-Step "Creating virtual environment in $ProjectDir"
        Invoke-InDirectory -Directory $ProjectDir -ScriptBlock { python -m venv .venv | Out-Null }
        $ForceInstall = $true
    }

    if ($ForceInstall) {
        $requirements = Join-Path $ProjectDir 'requirements.txt'
        $pyproject = Join-Path $ProjectDir 'pyproject.toml'
        if (Test-Path -LiteralPath $requirements) {
            Write-Step "Installing Python dependencies in $ProjectDir"
            & $venvPython -m pip install --upgrade pip | Out-Host
            if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip in $ProjectDir" }
            & $venvPython -m pip install -r $requirements | Out-Host
            if ($LASTEXITCODE -ne 0) { throw "Failed to install Python dependencies in $ProjectDir" }
        } elseif (Test-Path -LiteralPath $pyproject) {
            Write-Step "Installing Python package in $ProjectDir"
            & $venvPython -m pip install --upgrade pip | Out-Host
            if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip in $ProjectDir" }
            Invoke-InDirectory -Directory $ProjectDir -ScriptBlock { & $venvPython -m pip install -e . | Out-Host }
            if ($LASTEXITCODE -ne 0) { throw "Failed to install Python package in $ProjectDir" }
        }
    }

    return $venvPython
}

# Function: Ensure-NodeModules
function Ensure-NodeModules {
    param([string]$ProjectDir, [switch]$ForceInstall, [switch]$IsDryRun)
    if ($IsDryRun) { return }

    $modulesPath = Join-Path $ProjectDir 'node_modules'
    if ($ForceInstall -or -not (Test-Path -LiteralPath $modulesPath)) {
        Write-Step "Installing Node dependencies in $ProjectDir"
        Invoke-InDirectory -Directory $ProjectDir -ScriptBlock { npm install }
        if ($LASTEXITCODE -ne 0) { throw "npm install failed in $ProjectDir" }
    }
}

# Function: Ensure-ModernizationTypeScriptValidator
function Ensure-ModernizationTypeScriptValidator {
    param([string]$ProjectDir, [switch]$ForceInstall, [switch]$IsDryRun)

    $compilerPath = Join-Path $ProjectDir 'node_modules\typescript\lib\tsc.js'
    if ($IsDryRun) { return }
    if ($ForceInstall -or -not (Test-Path -LiteralPath $compilerPath -PathType Leaf)) {
        Write-Step 'Installing the Modernization TypeScript validator'
        Invoke-InDirectory -Directory $ProjectDir -ScriptBlock { npm ci --ignore-scripts }
        if ($LASTEXITCODE -ne 0) { throw "npm ci failed in $ProjectDir" }
        if (-not (Test-Path -LiteralPath $compilerPath -PathType Leaf)) {
            throw "Modernization TypeScript validator was not installed at $compilerPath"
        }
    }
}

# Function: Invoke-FrontendBuild
function Invoke-FrontendBuild {
    param([string]$Label, [string]$Directory, [switch]$IsDryRun)
    if ($IsDryRun) {
        Write-Host "DRY-RUN: [$Label] npm run build" -ForegroundColor Yellow
        return
    }

    Write-Step "Building $Label"
    Invoke-InDirectory -Directory $Directory -ScriptBlock { npm run build }
    if ($LASTEXITCODE -ne 0) { throw "$Label build failed with exit code $LASTEXITCODE" }
}

# Function: Stop-ProcessTree
function Stop-ProcessTree {
    param([int]$ProcessId)

    $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $proc) { return }

    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessId" -ErrorAction SilentlyContinue
    foreach ($child in $children) {
        Stop-ProcessTree -ProcessId ([int]$child.ProcessId)
    }

    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

# Function: Ensure-PortFree
function Ensure-PortFree {
    param([int]$Port, [string]$Label, [switch]$IsDryRun)

    $listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
    if (-not $listeners) { return }

    if ($IsDryRun) {
        Write-Host "DRY-RUN: would free port $Port for $Label" -ForegroundColor Yellow
        return
    }

    foreach ($procId in $listeners) {
        $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Step "Stopping $($proc.ProcessName) (PID $procId) on port $Port for $Label"
            Stop-ProcessTree -ProcessId $procId
        }
    }
}

# Function: Ensure-Neo4jDesktopDbmsRunning
function Ensure-Neo4jDesktopDbmsRunning {
    param([switch]$IsDryRun)

    $listener = Get-NetTCPConnection -LocalPort 7687 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
        Write-Step 'Neo4j Bolt (7687) is already listening.'
        return
    }

    if ($IsDryRun) {
        Write-Host 'DRY-RUN: would start the Neo4j Desktop DBMS' -ForegroundColor Yellow
        return
    }

    $dbmsRoot = Join-Path $env:USERPROFILE '.Neo4jDesktop\relate-data\dbmss'
    $dbms = Get-ChildItem -Path $dbmsRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-Path (Join-Path $_.FullName 'bin\neo4j.bat') } |
        Select-Object -First 1

    if (-not $dbms) {
        Write-Warning "No Neo4j DBMS found under $dbmsRoot. Create and start a Local DBMS in Neo4j Desktop once, then rerun this script."
        return
    }

    $neo4jBat = Join-Path $dbms.FullName 'bin\neo4j.bat'
    Write-Step "Starting Neo4j DBMS at $($dbms.FullName)"
    Start-Process -FilePath $neo4jBat -ArgumentList 'start' -WindowStyle Hidden | Out-Null
    Wait-ForPort -Port 7687 -Label 'Neo4j Bolt' -TimeoutSeconds 60 -IsDryRun:$IsDryRun
}

# Function: Wait-ForPort
function Wait-ForPort {
    param([int]$Port, [string]$Label, [int]$TimeoutSeconds = 60, [switch]$IsDryRun)
    if ($IsDryRun) {
        Write-Host "DRY-RUN: would wait for $Label on port $Port" -ForegroundColor Yellow
        return
    }

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($listener) {
            Write-Step "$Label is listening on port $Port"
            return
        }
        Start-Sleep -Milliseconds 500
    }

    Write-Warning "$Label did not open port $Port within $TimeoutSeconds seconds."
}

# Function: Start-ServiceWindow
function Start-ServiceWindow {
    param([string]$Title, [string]$Directory, [string]$Command, [switch]$IsDryRun)

    if ($IsDryRun) {
        Write-Host "DRY-RUN: [$Title] $Command" -ForegroundColor Yellow
        return
    }

    $existingWindows = Get-Process -Name 'powershell' -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowTitle -eq $Title }
    foreach ($existing in $existingWindows) {
        Write-Step "Closing existing service window '$Title' (PID $($existing.Id))"
        Stop-ProcessTree -ProcessId $existing.Id
    }

    $safeTitle = $Title.Replace("'", "''")
    $safeDirectory = $Directory.Replace("'", "''")
    $payload = @"
`$Host.UI.RawUI.WindowTitle = '$safeTitle'
Set-Location -LiteralPath '$safeDirectory'
Write-Host '[$safeTitle] Starting in $safeDirectory' -ForegroundColor Green
$Command
if (`$LASTEXITCODE -ne `$null -and `$LASTEXITCODE -ne 0) {
    Write-Host '[$safeTitle] Exited with code' `$LASTEXITCODE -ForegroundColor Red
}
"@

    $tmpFile = [System.IO.Path]::ChangeExtension([System.IO.Path]::GetTempFileName(), '.ps1')
    Set-Content -LiteralPath $tmpFile -Value $payload -Encoding UTF8
    Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoExit', '-ExecutionPolicy', 'Bypass', '-File', $tmpFile) | Out-Null
}

$repoRoot = Split-Path -Parent $PSCommandPath
. (Join-Path $repoRoot 'Maintenance\Shared-Auth.ps1')
$sharedAuthSecret = Get-Strat-AqorynthSharedAuthSecret -RepoRoot $repoRoot

$appRatBackendDir = Join-Path $repoRoot 'AppRationalization\backend'
$appRatFrontendDir = Join-Path $repoRoot 'AppRationalization\frontend'
$infraBackendDir = Join-Path $repoRoot 'InfraRationalization'
$infraFrontendDir = Join-Path $repoRoot 'InfraRationalization\frontend'
$codeBackendDir = Join-Path $repoRoot 'CodeAnalysis'
$codeFrontendDir = Join-Path $repoRoot 'CodeAnalysis\frontend'
$kgBackendDir = Join-Path $repoRoot 'Novastra-ITSM'
$kgFrontendDir = Join-Path $repoRoot 'Novastra-ITSM\frontend'
$dashboardBackendDir = Join-Path $repoRoot 'Dashboard\backend'
$dashboardFrontendDir = Join-Path $repoRoot 'Dashboard\frontend'
$ssdlcBackendDir = Join-Path $repoRoot 'SSDLC_Process_Assessment\backend'
$ssdlcFrontendDir = Join-Path $repoRoot 'SSDLC_Process_Assessment\frontend'
$modernizationBackendDir = Join-Path $repoRoot 'Modernization'
$modernizationFrontendDir = Join-Path $repoRoot 'Modernization\frontend'
$modernizationTypeScriptValidatorDir = Join-Path $modernizationBackendDir 'tools\ts-validate'
$labRobotBackendDir = Join-Path $repoRoot 'LabRobot\backend'
$labRobotFrontendDir = Join-Path $repoRoot 'LabRobot\frontend'
$otBackendDir = Join-Path $repoRoot 'OpportunityTracker\backend'
$otFrontendDir = Join-Path $repoRoot 'OpportunityTracker\frontend'
$aiRemanBackendDir = Join-Path $repoRoot 'AI_Reman_Core\backend'
$aiRemanFrontendDir = Join-Path $repoRoot 'AI_Reman_Core'
$aiVehicleBackendDir = Join-Path $repoRoot 'AI_Vehicle_Loan'
$aiVehicleFrontendDir = Join-Path $repoRoot 'AI_Vehicle_Loan\frontend'
$micrositeDir = Join-Path $repoRoot 'Microsite_Data_Analysis'
$scmRoot = Join-Path $repoRoot 'supply-chain-disruption-manager'
$scmKgDir = Join-Path $scmRoot 'services\kg-service'
$scmInspectorDir = Join-Path $scmRoot 'services\signal-inspector'
$scmAgentDir = Join-Path $scmRoot 'services\agent-service'
$scmFrontendDir = Join-Path $scmRoot 'apps\web-ui'

$directories = @(
    @{ Path = $appRatBackendDir; Label = 'AppRationalization backend' },
    @{ Path = $appRatFrontendDir; Label = 'AppRationalization frontend' },
    @{ Path = $infraBackendDir; Label = 'InfraRationalization backend' },
    @{ Path = $infraFrontendDir; Label = 'InfraRationalization frontend' },
    @{ Path = $codeBackendDir; Label = 'CodeAnalysis backend' },
    @{ Path = $codeFrontendDir; Label = 'CodeAnalysis frontend' },
    @{ Path = $kgBackendDir; Label = 'Novastra-ITSM backend' },
    @{ Path = $kgFrontendDir; Label = 'Novastra-ITSM frontend' },
    @{ Path = $dashboardBackendDir; Label = 'Dashboard backend' },
    @{ Path = $dashboardFrontendDir; Label = 'Dashboard frontend' },
    @{ Path = $ssdlcBackendDir; Label = 'SSDLC_Process_Assessment backend' },
    @{ Path = $ssdlcFrontendDir; Label = 'SSDLC_Process_Assessment frontend' },
    @{ Path = $modernizationBackendDir; Label = 'Modernization backend' },
    @{ Path = $modernizationFrontendDir; Label = 'Modernization frontend' },
    @{ Path = $labRobotBackendDir; Label = 'LabRobot backend' },
    @{ Path = $labRobotFrontendDir; Label = 'LabRobot frontend' },
    @{ Path = $otBackendDir; Label = 'OpportunityTracker backend' },
    @{ Path = $otFrontendDir; Label = 'OpportunityTracker frontend' },
    @{ Path = $aiRemanBackendDir; Label = 'AI_Reman_Core backend' },
    @{ Path = $aiRemanFrontendDir; Label = 'AI_Reman_Core frontend' },
    @{ Path = $aiVehicleBackendDir; Label = 'AI_Vehicle_Loan backend' },
    @{ Path = $aiVehicleFrontendDir; Label = 'AI_Vehicle_Loan frontend' },
    @{ Path = $micrositeDir; Label = 'Microsite_Data_Analysis frontend' },
    @{ Path = $scmKgDir; Label = 'SupplyChainDisruptionManager kg-service' },
    @{ Path = $scmInspectorDir; Label = 'SupplyChainDisruptionManager signal-inspector' },
    @{ Path = $scmAgentDir; Label = 'SupplyChainDisruptionManager agent-service' },
    @{ Path = $scmFrontendDir; Label = 'SupplyChainDisruptionManager web-ui' }
)
foreach ($entry in $directories) {
    Ensure-Directory -Path $entry.Path -Label $entry.Label
}

Assert-Command -Name 'python'
Assert-Command -Name 'npm'

$appRatPython = Ensure-PythonEnv -ProjectDir $appRatBackendDir -ForceInstall:$InstallDeps -IsDryRun:$DryRun
$infraPython = Ensure-PythonEnv -ProjectDir $infraBackendDir -ForceInstall:$InstallDeps -IsDryRun:$DryRun
$codePython = Ensure-PythonEnv -ProjectDir $codeBackendDir -ForceInstall:$InstallDeps -IsDryRun:$DryRun
$kgPython = Ensure-PythonEnv -ProjectDir $kgBackendDir -ForceInstall:$InstallDeps -IsDryRun:$DryRun
$dashboardPython = Ensure-PythonEnv -ProjectDir $dashboardBackendDir -ForceInstall:$InstallDeps -IsDryRun:$DryRun
$ssdlcPython = Ensure-PythonEnv -ProjectDir $ssdlcBackendDir -ForceInstall:$InstallDeps -IsDryRun:$DryRun
$modernizationPython = Ensure-PythonEnv -ProjectDir $modernizationBackendDir -ForceInstall:$InstallDeps -IsDryRun:$DryRun
$labRobotPython = Ensure-PythonEnv -ProjectDir $labRobotBackendDir -ForceInstall:$InstallDeps -IsDryRun:$DryRun
$otPython = Ensure-PythonEnv -ProjectDir $otBackendDir -ForceInstall:$InstallDeps -IsDryRun:$DryRun
$aiRemanPython = Ensure-PythonEnv -ProjectDir $aiRemanBackendDir -ForceInstall:$InstallDeps -IsDryRun:$DryRun
$aiVehiclePython = Ensure-PythonEnv -ProjectDir $aiVehicleBackendDir -ForceInstall:$InstallDeps -IsDryRun:$DryRun
$scmKgPython = Ensure-PythonEnv -ProjectDir $scmKgDir -ForceInstall:$InstallDeps -IsDryRun:$DryRun
$scmInspectorPython = Ensure-PythonEnv -ProjectDir $scmInspectorDir -ForceInstall:$InstallDeps -IsDryRun:$DryRun
$scmAgentPython = Ensure-PythonEnv -ProjectDir $scmAgentDir -ForceInstall:$InstallDeps -IsDryRun:$DryRun

foreach ($frontend in @($appRatFrontendDir, $infraFrontendDir, $codeFrontendDir, $kgFrontendDir, $dashboardFrontendDir, $ssdlcFrontendDir, $modernizationFrontendDir, $labRobotFrontendDir, $otFrontendDir, $aiRemanFrontendDir, $aiVehicleFrontendDir, $micrositeDir, $scmFrontendDir)) {
    Ensure-NodeModules -ProjectDir $frontend -ForceInstall:$InstallDeps -IsDryRun:$DryRun
}
Ensure-ModernizationTypeScriptValidator -ProjectDir $modernizationTypeScriptValidatorDir -ForceInstall:$InstallDeps -IsDryRun:$DryRun

Ensure-Neo4jDesktopDbmsRunning -IsDryRun:$DryRun

if (-not $DryRun) {
    foreach ($infraPort in @(
        @{ Port = 5432; Label = 'PostgreSQL (SupplyChainDisruptionManager signal-inspector/agent-service)' },
        @{ Port = 6379; Label = 'Redis (SupplyChainDisruptionManager signal-inspector/agent-service)' }
    )) {
        $listener = Get-NetTCPConnection -LocalPort $infraPort.Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $listener) {
            Write-Warning "$($infraPort.Label) is not reachable on port $($infraPort.Port). Start it before using the module."
        }
    }
}

$appRatBackendCommand = "`$env:FLASK_DEBUG='false'; `$env:FLASK_ENV='production'; `$env:FLASK_HOST='0.0.0.0'; `$env:DATABASE_PROVIDER='sqlite'; `$env:CORS_ORIGINS='http://localhost,http://127.0.0.1,http://localhost:8090,http://127.0.0.1:8090,http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001,http://127.0.0.1:3001'; `$env:INCLUDE_LOCALHOST_CORS_ORIGINS='true'; `$env:AUTH_TOKEN_SECRET='$sharedAuthSecret'; & '$appRatPython' run.py"
$appRatFrontendCommand = "`$env:BROWSER='none'; `$env:PORT='3000'; `$env:HOST='0.0.0.0'; `$env:REACT_APP_API_URL='http://localhost:5001/api'; `$env:REACT_APP_CODE_ANALYSIS_URL='http://localhost:5173/ca/'; `$env:REACT_APP_INFRA_SCAN_URL='http://localhost:5174/infra/'; `$env:REACT_APP_DASHBOARD_URL='http://localhost:5178/dash/connect?autostart=1'; `$env:REACT_APP_NOVASTRA_ITSM_URL='http://localhost:5177/novastra-itsm/ticket-analysis'; `$env:REACT_APP_SSDLC_PROCESS_ASSESSMENT_URL='http://localhost:5182/ssdlc/'; `$env:REACT_APP_MODERNIZATION_URL='http://localhost:5175/'; `$env:REACT_APP_LAB_ROBOT_URL='http://localhost:7000/'; `$env:REACT_APP_OPPORTUNITY_TRACKER_URL='http://localhost:5183/ot/'; `$env:REACT_APP_AI_REMAN_CORE_URL='http://localhost:5184/'; `$env:REACT_APP_AI_VEHICLE_LOAN_URL='http://localhost:5185/'; `$env:REACT_APP_MICROSITE_DATA_ANALYSIS_URL='http://localhost:5187/mda/'; `$env:REACT_APP_SUPPLY_CHAIN_DISRUPTION_MANAGER_URL='http://localhost:5188/'; npm start"
$infraBackendCommand = "& '$infraPython' -m uvicorn api.server:app --host 0.0.0.0 --port 8083"
$infraFrontendCommand = "`$env:VITE_INFRA_API_URL='http://localhost:8083'; `$env:VITE_PORTAL_HOME_URL='http://localhost:3000/launch-modules'; `$env:VITE_PORTAL_LOGIN_URL='http://localhost:3000/login'; npm run dev -- --host 0.0.0.0 --port 5174"
$codeBackendCommand = "`$env:GIT_PYTHON_REFRESH='quiet'; `$env:AUTH_REQUIRED='true'; `$env:AUTH_TOKEN_SECRET='$sharedAuthSecret'; & '$codePython' -m uvicorn api.server:app --host 0.0.0.0 --port 8082"
$codeFrontendCommand = "`$env:VITE_PORTAL_API_URL='http://localhost:5001/api'; `$env:VITE_PORTAL_LOGIN_URL='http://localhost:3000/login'; `$env:VITE_PORTAL_HOME_URL='http://localhost:3000/launch-modules'; npm run dev -- --host 0.0.0.0 --port 5173"
$kgBackendCommand = "`$env:DB_BACKEND='sqlite'; `$env:VECTOR_BACKEND='lancedb'; `$env:SYNC_REQUIRE_DUAL_WRITE='false'; `$env:ALLOWED_ORIGINS='http://localhost,http://localhost:5177,http://localhost:8090,http://127.0.0.1:8090'; `$env:CORS_ORIGINS='http://localhost,http://localhost:5177,http://localhost:8090,http://127.0.0.1:8090'; & '$kgPython' -m uvicorn backend.main:app --host 0.0.0.0 --port 8086"
$kgFrontendCommand = "`$env:VITE_PORTAL_LOGIN_URL='http://localhost:3000/login'; `$env:VITE_PORTAL_HOME_URL='http://localhost:3000/launch-modules'; `$env:VITE_ALLOW_LOCAL_AUTH_BYPASS='true'; npm run dev -- --host 0.0.0.0 --port 5177"
$dashboardBackendCommand = "& '$dashboardPython' -m uvicorn main:app --host 0.0.0.0 --port 8087"
$dashboardFrontendCommand = "`$env:VITE_PORTAL_HOME_URL='http://localhost:3000/launch-modules'; `$env:VITE_PORTAL_ADMIN_URL='http://localhost:3000/admin'; npm run dev -- --host 0.0.0.0 --port 5178"
$ssdlcBackendCommand = "`$env:CORS_ORIGINS='http://localhost:3000,http://localhost:5182,http://localhost:8090,http://127.0.0.1:8090'; `$env:AUTH_TOKEN_SECRET='$sharedAuthSecret'; `$env:ALLOW_LOCAL_AUTH_BYPASS='true'; `$env:STRATIQ_RUNTIME_MODE='development'; & '$ssdlcPython' -m uvicorn app.main:app --host 0.0.0.0 --port 8091"
$ssdlcFrontendCommand = "`$env:VITE_PORTAL_LOGIN_URL='http://localhost:3000/login'; `$env:VITE_PORTAL_HOME_URL='http://localhost:3000/launch-modules'; npm run dev -- --host 0.0.0.0 --port 5182"
$modernizationBackendCommand = "`$env:CORS_ORIGINS='http://localhost,http://127.0.0.1,http://localhost:3000,http://127.0.0.1:3000,http://localhost:5175,http://127.0.0.1:5175,http://localhost:8090,http://127.0.0.1:8090'; `$env:AUTH_TOKEN_SECRET='$sharedAuthSecret'; & '$modernizationPython' -m uvicorn api.server:app --host 0.0.0.0 --port 8084"
$modernizationFrontendCommand = "`$env:VITE_PORTAL_HOME_URL='http://localhost:3000/launch-modules'; `$env:VITE_PORTAL_LOGIN_URL='http://localhost:3000/login'; npm run dev -- --host 0.0.0.0 --port 5175"
$labRobotBackendCommand = "& '$labRobotPython' -m uvicorn main:app --host 0.0.0.0 --port 8000"
$labRobotFrontendCommand = "npm run dev"
$otWatchdogScript = Join-Path $repoRoot 'OpportunityTracker\watchdog_backend.ps1'
$otBackendCommand = "`$env:CORS_ORIGINS='http://localhost:5183,http://localhost:3000,http://localhost:5177,http://localhost:8090,http://127.0.0.1:8090'; & '$otWatchdogScript'"
$otFrontendCommand = "`$env:VITE_PORTAL_HOME_URL='http://localhost:3000/home'; `$env:VITE_PORTAL_LOGIN_URL='http://localhost:3000/login'; npm run dev -- --host 0.0.0.0 --port 5183"
$aiRemanBackendCommand = "& '$aiRemanPython' -m uvicorn main:app --host 0.0.0.0 --port 8093"
$aiRemanFrontendCommand = "`$env:BROWSER='none'; `$env:PORT='5184'; `$env:HOST='0.0.0.0'; `$env:REACT_APP_API_URL='http://localhost:8093'; `$env:REACT_APP_PORTAL_LOGIN_URL='http://localhost:3000/login'; `$env:REACT_APP_PORTAL_HOME_URL='http://localhost:3000/launch-modules'; npm start"
$aiVehicleBackendCommand = "& '$aiVehiclePython' -m uvicorn api.main:app --host 0.0.0.0 --port 8094"
$aiVehicleFrontendCommand = "`$env:BROWSER='none'; `$env:PORT='5185'; `$env:HOST='0.0.0.0'; `$env:REACT_APP_API_URL='http://localhost:8094'; npm start"
$micrositeFrontendCommand = "npm run dev -- --host 0.0.0.0 --port 5187"
$scmKgSrc = Join-Path $scmKgDir 'src'
$scmInspectorSrc = Join-Path $scmInspectorDir 'src'
$scmAgentSrc = Join-Path $scmAgentDir 'src'
$scmKgBackendCommand = "`$env:PYTHONPATH='$scmKgSrc'; `$env:NEO4J_URI='bolt://localhost:7687'; `$env:NEO4J_USER='neo4j'; `$env:NEO4J_PASSWORD='disruption123'; `$env:KG_API_KEY='kg-dev-key-change-in-prod'; `$env:CORS_ORIGINS='http://localhost,http://localhost:5188,http://localhost:8090,http://127.0.0.1:8090'; & '$scmKgPython' -m uvicorn kg.main:app --host 0.0.0.0 --port 8001"
$scmInspectorBackendCommand = "`$env:PYTHONPATH='$scmInspectorSrc'; `$env:POSTGRES_URL='postgresql+asyncpg://sc_admin:sc_secret@localhost:5432/disruption_mgr'; `$env:REDIS_URL='redis://localhost:6379/0'; `$env:INSPECTOR_ERP_HMAC_SECRET='erp-hmac-secret-change-in-prod'; `$env:CORS_ORIGINS='http://localhost,http://localhost:5188,http://localhost:8090,http://127.0.0.1:8090'; & '$scmInspectorPython' -m uvicorn inspector.main:app --host 0.0.0.0 --port 8003"
$scmAgentBackendCommand = "`$env:PYTHONPATH='$scmAgentSrc'; `$env:POSTGRES_URL='postgresql+asyncpg://sc_admin:sc_secret@localhost:5432/disruption_mgr'; `$env:REDIS_URL='redis://localhost:6379/0'; `$env:AGENT_API_KEY='agent-dev-key-change-in-prod'; `$env:KG_BASE_URL='http://localhost:8001'; `$env:KG_API_KEY='kg-dev-key-change-in-prod'; `$env:OLLAMA_BASE_URL='http://localhost:11434'; `$env:ORCHESTRATOR_MODEL='llama3.1:8b'; `$env:SPECIALIST_MODEL='llama3.1:8b'; `$env:MOCK_AGENTS='false'; & '$scmAgentPython' -m uvicorn agents.main:app --host 0.0.0.0 --port 8002"
$scmFrontendCommand = "`$env:VITE_KG_API_URL='http://localhost:8001'; `$env:VITE_KG_API_KEY='kg-dev-key-change-in-prod'; `$env:VITE_INSPECTOR_API_URL='http://localhost:8003'; `$env:VITE_INSPECTOR_API_KEY='inspector-dev-key'; `$env:VITE_AGENT_API_URL='http://localhost:8002'; `$env:VITE_AGENT_API_KEY='agent-dev-key-change-in-prod'; `$env:VITE_PORTAL_HOME_URL='http://localhost:3000/launch-modules'; `$env:VITE_PORTAL_LOGIN_URL='http://localhost:3000/login'; npm run dev -- --host 0.0.0.0 --port 5188"

$frontendBuilds = @(
    @{ Label = 'AppRationalization'; Directory = $appRatFrontendDir },
    @{ Label = 'InfraRationalization'; Directory = $infraFrontendDir },
    @{ Label = 'CodeAnalysis'; Directory = $codeFrontendDir },
    @{ Label = 'Novastra-ITSM'; Directory = $kgFrontendDir },
    @{ Label = 'Dashboard'; Directory = $dashboardFrontendDir },
    @{ Label = 'SSDLC_Process_Assessment'; Directory = $ssdlcFrontendDir },
    @{ Label = 'Modernization'; Directory = $modernizationFrontendDir },
    @{ Label = 'LabRobot'; Directory = $labRobotFrontendDir },
    @{ Label = 'OpportunityTracker'; Directory = $otFrontendDir },
    @{ Label = 'AI_Reman_Core'; Directory = $aiRemanFrontendDir },
    @{ Label = 'AI_Vehicle_Loan'; Directory = $aiVehicleFrontendDir },
    @{ Label = 'Microsite_Data_Analysis'; Directory = $micrositeDir },
    @{ Label = 'SupplyChainDisruptionManager'; Directory = $scmFrontendDir }
)

if ($BuildFirst) {
    Write-Host ''
    Write-Step 'Building existing module frontends'
    foreach ($build in $frontendBuilds) {
        Invoke-FrontendBuild -Label $build.Label -Directory $build.Directory -IsDryRun:$DryRun
    }
    Write-Host ''
}

$services = @(
    @{ Title = 'AppRationalization Backend (port 5001)'; Directory = $appRatBackendDir; Command = $appRatBackendCommand; Port = 5001; Label = 'AppRationalization Backend'; Wait = 90 },
    @{ Title = 'AppRationalization Frontend (port 3000)'; Directory = $appRatFrontendDir; Command = $appRatFrontendCommand; Port = 3000; Label = 'AppRationalization Frontend'; Wait = 0 },
    @{ Title = 'InfraRationalization Backend (port 8083)'; Directory = $infraBackendDir; Command = $infraBackendCommand; Port = 8083; Label = 'InfraRationalization Backend'; Wait = 90 },
    @{ Title = 'InfraRationalization Frontend (port 5174)'; Directory = $infraFrontendDir; Command = $infraFrontendCommand; Port = 5174; Label = 'InfraRationalization Frontend'; Wait = 0 },
    @{ Title = 'CodeAnalysis Backend (port 8082)'; Directory = $codeBackendDir; Command = $codeBackendCommand; Port = 8082; Label = 'CodeAnalysis Backend'; Wait = 0 },
    @{ Title = 'CodeAnalysis Frontend (port 5173)'; Directory = $codeFrontendDir; Command = $codeFrontendCommand; Port = 5173; Label = 'CodeAnalysis Frontend'; Wait = 0 },
    @{ Title = 'Novastra-ITSM Backend (port 8086)'; Directory = $kgBackendDir; Command = $kgBackendCommand; Port = 8086; Label = 'Novastra-ITSM Backend'; Wait = 0 },
    @{ Title = 'Novastra-ITSM Frontend (port 5177)'; Directory = $kgFrontendDir; Command = $kgFrontendCommand; Port = 5177; Label = 'Novastra-ITSM Frontend'; Wait = 0 },
    @{ Title = 'Dashboard Backend (port 8087)'; Directory = $dashboardBackendDir; Command = $dashboardBackendCommand; Port = 8087; Label = 'Dashboard Backend'; Wait = 90 },
    @{ Title = 'Dashboard Frontend (port 5178)'; Directory = $dashboardFrontendDir; Command = $dashboardFrontendCommand; Port = 5178; Label = 'Dashboard Frontend'; Wait = 0 },
    @{ Title = 'SSDLC_Process_Assessment Backend (port 8091)'; Directory = $ssdlcBackendDir; Command = $ssdlcBackendCommand; Port = 8091; Label = 'SSDLC_Process_Assessment Backend'; Wait = 90 },
    @{ Title = 'SSDLC_Process_Assessment Frontend (port 5182)'; Directory = $ssdlcFrontendDir; Command = $ssdlcFrontendCommand; Port = 5182; Label = 'SSDLC_Process_Assessment Frontend'; Wait = 0 },
    @{ Title = 'Modernization Backend (port 8084)'; Directory = $modernizationBackendDir; Command = $modernizationBackendCommand; Port = 8084; Label = 'Modernization Backend'; Wait = 90 },
    @{ Title = 'Modernization Frontend (port 5175)'; Directory = $modernizationFrontendDir; Command = $modernizationFrontendCommand; Port = 5175; Label = 'Modernization Frontend'; Wait = 0 },
    @{ Title = 'LabRobot Backend (port 8000)'; Directory = $labRobotBackendDir; Command = $labRobotBackendCommand; Port = 8000; Label = 'LabRobot Backend'; Wait = 90 },
    @{ Title = 'LabRobot Frontend (port 7000)'; Directory = $labRobotFrontendDir; Command = $labRobotFrontendCommand; Port = 7000; Label = 'LabRobot Frontend'; Wait = 0 },
    @{ Title = 'OpportunityTracker Backend (port 8092)'; Directory = $otBackendDir; Command = $otBackendCommand; Port = 8092; Label = 'OpportunityTracker Backend'; Wait = 90 },
    @{ Title = 'OpportunityTracker Frontend (port 5183)'; Directory = $otFrontendDir; Command = $otFrontendCommand; Port = 5183; Label = 'OpportunityTracker Frontend'; Wait = 0 },
    @{ Title = 'AI_Reman_Core Backend (port 8093)'; Directory = $aiRemanBackendDir; Command = $aiRemanBackendCommand; Port = 8093; Label = 'AI_Reman_Core Backend'; Wait = 60 },
    @{ Title = 'AI_Reman_Core Frontend (port 5184)'; Directory = $aiRemanFrontendDir; Command = $aiRemanFrontendCommand; Port = 5184; Label = 'AI_Reman_Core Frontend'; Wait = 0 },
    @{ Title = 'AI_Vehicle_Loan Backend (port 8094)'; Directory = $aiVehicleBackendDir; Command = $aiVehicleBackendCommand; Port = 8094; Label = 'AI_Vehicle_Loan Backend'; Wait = 60 },
    @{ Title = 'AI_Vehicle_Loan Frontend (port 5185)'; Directory = $aiVehicleFrontendDir; Command = $aiVehicleFrontendCommand; Port = 5185; Label = 'AI_Vehicle_Loan Frontend'; Wait = 0 },
    @{ Title = 'Microsite_Data_Analysis Frontend (port 5187)'; Directory = $micrositeDir; Command = $micrositeFrontendCommand; Port = 5187; Label = 'Microsite_Data_Analysis Frontend'; Wait = 0 },
    @{ Title = 'SupplyChainDisruptionManager KG Service (port 8001)'; Directory = $scmKgDir; Command = $scmKgBackendCommand; Port = 8001; Label = 'SupplyChainDisruptionManager KG Service'; Wait = 60 },
    @{ Title = 'SupplyChainDisruptionManager Signal Inspector (port 8003)'; Directory = $scmInspectorDir; Command = $scmInspectorBackendCommand; Port = 8003; Label = 'SupplyChainDisruptionManager Signal Inspector'; Wait = 60 },
    @{ Title = 'SupplyChainDisruptionManager Agent Service (port 8002)'; Directory = $scmAgentDir; Command = $scmAgentBackendCommand; Port = 8002; Label = 'SupplyChainDisruptionManager Agent Service'; Wait = 60 },
    @{ Title = 'SupplyChainDisruptionManager Frontend (port 5188)'; Directory = $scmFrontendDir; Command = $scmFrontendCommand; Port = 5188; Label = 'SupplyChainDisruptionManager Frontend'; Wait = 0 }
)

foreach ($svc in $services) {
    Ensure-PortFree -Port $svc.Port -Label $svc.Label -IsDryRun:$DryRun
}

foreach ($svc in $services) {
    Start-ServiceWindow -Title $svc.Title -Directory $svc.Directory -Command $svc.Command -IsDryRun:$DryRun
    Start-Sleep -Milliseconds 400
    if ($svc.Wait -gt 0) {
        Wait-ForPort -Port $svc.Port -Label $svc.Label -TimeoutSeconds $svc.Wait -IsDryRun:$DryRun
    }
}

$launchUrl = 'http://localhost:3000/login'
if ($DryRun) {
    Write-Host "DRY-RUN: would open $launchUrl" -ForegroundColor Yellow
} else {
    Write-Step "Opening portal: $launchUrl"
    Start-Process $launchUrl | Out-Null
}

Write-Host ''
Write-Host 'Launch complete.' -ForegroundColor Green
Write-Host 'Login:                    http://localhost:3000/login'
Write-Host 'Launch Modules:           http://localhost:3000/launch-modules'
Write-Host 'AppRationalization API:    http://localhost:5001/api'
Write-Host 'InfraRationalization UI:   http://localhost:5174/infra/'
Write-Host 'CodeAnalysis UI:           http://localhost:5173/ca/'
Write-Host 'Novastra-ITSM UI:   http://localhost:5177/novastra-itsm/ticket-analysis'
Write-Host 'Dashboard UI:              http://localhost:5178/dash/connect?autostart=1'
Write-Host 'SSDLC Assessment UI:       http://localhost:5182/ssdlc/'
Write-Host 'Modernization UI:          http://localhost:5175/'
Write-Host 'LabRobot UI:               http://localhost:7000/'
Write-Host 'OpportunityTracker UI:     http://localhost:5183/ot/'
Write-Host 'AI Reman Core UI:          http://localhost:5184/'
Write-Host 'AI Vehicle Loan UI:        http://localhost:5185/'
Write-Host 'Data Analysis Studio UI:   http://localhost:5187/mda/'
Write-Host 'Supply Chain Disruption Manager UI: http://localhost:5188/'
Write-Host ''
Write-Host 'Usage:' -ForegroundColor Cyan
Write-Host '  .\Start-AllServices.ps1'
Write-Host '  .\Start-AllServices.ps1 -InstallDeps'
Write-Host '  .\Start-AllServices.ps1 -BuildFirst'
Write-Host '  .\Start-AllServices.ps1 -DryRun'
