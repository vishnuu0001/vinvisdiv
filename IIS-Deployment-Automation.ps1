# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: IIS-Deployment-Automation.ps1 — IIS-Deployment-Automation (IIS-Deployment-Automation.ps1)
# Date: 2026-06-24
# ---------------------------------------------------------------------------
# IIS Deployment Automation Script for Strat-Aqorynth
# This script automates the complete deployment process
# Run as Administrator

[CmdletBinding()]
param(
    [switch]$SkipWindowsUpdates,
    [switch]$SkipGPUSetup,
    [switch]$SkipOllama,
    [switch]$TestOnly,
    [string]$Domain = "strat-aqorynth.yourdomain.com",
    [string]$DeployPath = "C:\Strat-Aqorynth\Production",
    [string]$DataPath = "C:\Strat-Aqorynth\Data"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ============================================================================
# Logging and Utility Functions
# ============================================================================

$LogPath = Join-Path $DataPath "logs\deployment-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
New-Item -ItemType Directory -Path (Split-Path $LogPath) -Force | Out-Null

# Function: Write-Log
function Write-Log {
    param(
        [string]$Message,
        [ValidateSet('Info', 'Success', 'Warning', 'Error')]
        [string]$Level = 'Info'
    )
    
    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $colorMap = @{
        'Info'    = 'Cyan'
        'Success' = 'Green'
        'Warning' = 'Yellow'
        'Error'   = 'Red'
    }
    
    $logMessage = "[$timestamp] [$Level] $Message"
    Write-Host $logMessage -ForegroundColor $colorMap[$Level]
    Add-Content -Path $LogPath -Value $logMessage
}

# Function: Assert-Admin
function Assert-Admin {
    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object System.Security.Principal.WindowsPrincipal($currentUser)
    if (-not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Log "This script requires Administrator privileges" -Level Error
        exit 1
    }
}

# Function: Test-CommandExists
function Test-CommandExists {
    param([string]$Command)
    $null = Get-Command $Command -ErrorAction SilentlyContinue
    return $?
}

# Function: Wait-Service
function Wait-Service {
    param(
        [string]$ServiceName,
        [int]$TimeoutSeconds = 60
    )
    
    $elapsed = 0
    while ($elapsed -lt $TimeoutSeconds) {
        $svc = Get-Service $ServiceName -ErrorAction SilentlyContinue
        if ($svc -and $svc.Status -eq 'Running') {
            return $true
        }
        Start-Sleep -Seconds 2
        $elapsed += 2
    }
    return $false
}

# ============================================================================
# Phase 1: Windows Configuration
# ============================================================================

# Function: Initialize-WindowsEnvironment
function Initialize-WindowsEnvironment {
    Write-Log "=== Phase 1: Windows Environment Setup ===" -Level Info
    
    Assert-Admin
    
    # Set execution policy
    Write-Log "Configuring PowerShell execution policy"
    Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser -Force
    
    if (-not $SkipWindowsUpdates) {
        Write-Log "Checking for Windows updates (this may take several minutes)"
        # Note: Full update process may require manual restart
    }
    
    # Enable IIS features
    Write-Log "Enabling IIS and required features"
    $windowsFeatures = @(
        'IIS-WebServerRole',
        'IIS-WebServer',
        'IIS-CommonHttpFeatures',
        'IIS-DefaultDocument',
        'IIS-DirectoryBrowsing',
        'IIS-HttpErrors',
        'IIS-UrlAuthorization',
        'IIS-StaticContent',
        'IIS-WebSockets',
        'NetFx3'
    )
    foreach ($feature in $windowsFeatures) {
        try {
            Enable-WindowsOptionalFeature -Online -FeatureName $feature -NoRestart -ErrorAction SilentlyContinue
            Write-Log "  Enabled $feature" -Level Success
        } catch {
            Write-Log "  Failed to enable $feature" -Level Warning
        }
    }
    
    Write-Log "Restarting IIS"
    iisreset /stop
    Start-Sleep -Seconds 2
    iisreset /start
    
    Write-Log "Windows environment initialized successfully" -Level Success
}

# ============================================================================
# Phase 2: Runtime Installation
# ============================================================================

# Function: Install-Python
function Install-Python {
    Write-Log "Installing Python 3.12.6"
    
    if (Test-CommandExists "python") {
        $version = python --version 2>&1
        Write-Log "Python already installed: $version" -Level Success
        return
    }
    
    $PythonVersion = "3.12.6"
    $DownloadURL = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe"
    $InstallerPath = "$env:TEMP\python-$PythonVersion-amd64.exe"
    
    Write-Log "Downloading Python $PythonVersion"
    try {
        Invoke-WebRequest -Uri $DownloadURL -OutFile $InstallerPath -ErrorAction Stop
    } catch {
        Write-Log "Failed to download Python: $_" -Level Error
        return $false
    }
    
    Write-Log "Installing Python"
    & $InstallerPath /quiet InstallAllUsers=1 PrependPath=1 Include_test=0 Include_tcltk=1 Include_pip=1
    
    $installed = $false
    for ($i = 0; $i -lt 30; $i++) {
        if (Test-CommandExists "python") {
            python --version
            $installed = $true
            break
        }
        Start-Sleep -Seconds 2
    }
    
    if ($installed) {
        Write-Log "Python installed successfully" -Level Success
        python -m pip install --upgrade pip setuptools wheel
        return $true
    } else {
        Write-Log "Python installation failed" -Level Error
        return $false
    }
}

# Function: Install-NodeJS
function Install-NodeJS {
    Write-Log "Installing Node.js 20.x"
    
    if (Test-CommandExists "node") {
        $version = node --version
        Write-Log "Node.js already installed: $version" -Level Success
        return
    }
    
    $DownloadURL = "https://nodejs.org/dist/latest-v20.x/node-v20.13.0-x64.msi"
    $InstallerPath = "$env:TEMP\node-installer.msi"
    
    Write-Log "Downloading Node.js"
    try {
        Invoke-WebRequest -Uri $DownloadURL -OutFile $InstallerPath -ErrorAction Stop
    } catch {
        Write-Log "Failed to download Node.js: $_" -Level Error
        return $false
    }
    
    Write-Log "Installing Node.js"
    msiexec.exe /i $InstallerPath /quiet /norestart
    
    for ($i = 0; $i -lt 30; $i++) {
        if (Test-CommandExists "node") {
            node --version
            npm --version
            npm install -g npm@latest
            Write-Log "Node.js installed successfully" -Level Success
            return $true
        }
        Start-Sleep -Seconds 2
    }
    
    Write-Log "Node.js installation failed" -Level Error
    return $false
}

# Function: Install-Git
function Install-Git {
    Write-Log "Installing Git"
    
    if (Test-CommandExists "git") {
        $version = git --version
        Write-Log "Git already installed: $version" -Level Success
        return
    }
    
    $DownloadURL = "https://github.com/git-for-windows/git/releases/download/v2.44.0.windows.1/Git-2.44.0-64-bit.exe"
    $InstallerPath = "$env:TEMP\Git-installer.exe"
    
    Write-Log "Downloading Git"
    Invoke-WebRequest -Uri $DownloadURL -OutFile $InstallerPath -ErrorAction Stop
    
    Write-Log "Installing Git"
    & $InstallerPath /VERYSILENT /NORESTART
    
    Start-Sleep -Seconds 5
    Write-Log "Git installed successfully" -Level Success
}

# Function: Install-URLRewrite
function Install-URLRewrite {
    Write-Log "Installing IIS URL Rewrite"
    
    # Check if already installed
    if ((Get-IISConfigSection -SectionPath "system.webServer/proxy" -CommitPath "machine/webroot/apphost" -ErrorAction SilentlyContinue) -ne $null) {
        Write-Log "URL Rewrite already installed" -Level Success
        return
    }
    
    $URLRewriteURL = "https://download.microsoft.com/download/1/2/8/128E2E22-C1B9-44F4-9B4E-EBE20C87FB3A/rewrite_amd64_en-US.msi"
    $URLRewritePath = "$env:TEMP\rewrite_amd64.msi"
    
    Write-Log "Downloading URL Rewrite"
    Invoke-WebRequest -Uri $URLRewriteURL -OutFile $URLRewritePath -ErrorAction Stop
    
    Write-Log "Installing URL Rewrite"
    msiexec.exe /i $URLRewritePath /quiet /norestart
    
    Start-Sleep -Seconds 5
    Write-Log "URL Rewrite installed successfully" -Level Success
}

# Function: Configure-IISReverseProxy
function Configure-IISReverseProxy {
    Write-Log "Configuring IIS ARR reverse proxy"

    Import-Module WebAdministration -ErrorAction Stop
    $proxySection = Get-WebConfiguration `
        -Filter 'system.webServer/proxy' `
        -PSPath 'IIS:\' `
        -ErrorAction Stop

    if ($null -eq $proxySection) {
        throw 'IIS ARR proxy configuration is unavailable. Install Application Request Routing before deployment.'
    }

    # SCM exposes two permanent Server-Sent Events connections. Buffering a
    # chunked response waits for a body that never completes, leaks proxy
    # connections, and eventually turns otherwise healthy /api/* calls into
    # 502.3 responses.
    Set-WebConfigurationProperty `
        -Filter 'system.webServer/proxy' `
        -PSPath 'IIS:\' `
        -Name 'enabled' `
        -Value $true
    Set-WebConfigurationProperty `
        -Filter 'system.webServer/proxy' `
        -PSPath 'IIS:\' `
        -Name 'bufferChunkedResponses' `
        -Value $false

    $configured = Get-WebConfiguration -Filter 'system.webServer/proxy' -PSPath 'IIS:\'
    if (-not $configured.enabled -or $configured.bufferChunkedResponses) {
        throw 'ARR proxy configuration validation failed.'
    }

    Write-Log "IIS ARR enabled with chunked-response buffering disabled" -Level Success
}

# Function: Install-NSSM
function Install-NSSM {
    Write-Log "Installing NSSM (Non-Sucking Service Manager)"
    
    $NSSmPath = "C:\Program Files\NSSM\win64\nssm.exe"
    if (Test-Path $NSSmPath) {
        Write-Log "NSSM already installed" -Level Success
        return
    }
    
    $NSSmURL = "https://nssm.cc/download/nssm-2.24-103-g9efd37d.zip"
    $NSSmZip = "$env:TEMP\nssm.zip"
    
    Write-Log "Downloading NSSM"
    Invoke-WebRequest -Uri $NSSmURL -OutFile $NSSmZip -ErrorAction Stop
    
    Write-Log "Extracting NSSM"
    Expand-Archive -Path $NSSmZip -DestinationPath "C:\Program Files\NSSM" -Force
    
    # Add to PATH
    $env:Path += ";C:\Program Files\NSSM\win64"
    [Environment]::SetEnvironmentVariable("Path", $env:Path, "Machine")
    
    Write-Log "NSSM installed successfully" -Level Success
}

# ============================================================================
# Phase 3: Directory and IIS Setup
# ============================================================================

# Function: Initialize-DirectoryStructure
function Initialize-DirectoryStructure {
    Write-Log "Creating directory structure"
    
    $directories = @(
        $DeployPath,
        $DataPath,
        "$DeployPath\CodeAnalysis",
        "$DeployPath\CodeAnalysis\frontend\dist",
        "$DeployPath\AppRationalization",
        "$DeployPath\AppRationalization\frontend\build",
        "$DeployPath\Novastra-ITSM",
        "$DeployPath\Novastra-ITSM\frontend\dist",
        "$DeployPath\Dashboard",
        "$DeployPath\Dashboard\frontend\dist",
        "$DeployPath\InfraRationalization",
        "$DeployPath\InfraRationalization\frontend\dist",
        "$DeployPath\IntuneAutomation",
        "$DeployPath\IntuneAutomation\frontend\dist",
        "$DeployPath\Tool_Analysis_Qualification",
        "$DeployPath\Tool_Analysis_Qualification\frontend\dist",
        "$DeployPath\Modernization",
        "$DeployPath\Modernization\frontend\dist",
        "$DeployPath\LabRobot",
        "$DeployPath\LabRobot\frontend\dist",
        "$DeployPath\SSDLC_Process_Assessment",
        "$DeployPath\SSDLC_Process_Assessment\frontend\dist",
        "$DeployPath\LaunchModules",
        "$DataPath\logs",
        "$DataPath\databases"
    )
    
    foreach ($dir in $directories) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        Write-Log "   Created $dir" -Level Success
    }
    
    # Set permissions
    Write-Log "Configuring NTFS permissions"
    $AppPoolUser = "IIS AppPool\DefaultAppPool"
    $Acl = Get-Acl $DeployPath
    $Ar = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $AppPoolUser,
        "FullControl",
        "ContainerInherit,ObjectInherit",
        "None",
        "Allow"
    )
    $Acl.SetAccessRule($Ar)
    Set-Acl -Path $DeployPath -AclObject $Acl
    
    Write-Log "Directory structure initialized" -Level Success
}

# Function: Setup-IISAppPools
function Setup-IISAppPools {
    Write-Log "Configuring IIS Application Pools"
    
    Import-Module WebAdministration
    
    # Backend app pool
    if (-not (Test-Path "IIS:\AppPools\Strat-Aqorynth-Backends")) {
        New-WebAppPool -Name "Strat-Aqorynth-Backends" -Force
    }
    
    Set-ItemProperty -Path "IIS:\AppPools\Strat-Aqorynth-Backends" -Name "managedRuntimeVersion" -Value ""
    Set-ItemProperty -Path "IIS:\AppPools\Strat-Aqorynth-Backends" -Name "startMode" -Value "AlwaysRunning"
    Set-ItemProperty -Path "IIS:\AppPools\Strat-Aqorynth-Backends" -Name "enable32BitAppCompat" -Value $true
    
    # Frontend app pool
    if (-not (Test-Path "IIS:\AppPools\Strat-Aqorynth-Frontends")) {
        New-WebAppPool -Name "Strat-Aqorynth-Frontends" -Force
    }
    
    Set-ItemProperty -Path "IIS:\AppPools\Strat-Aqorynth-Frontends" -Name "managedRuntimeVersion" -Value "v4.0"
    Set-ItemProperty -Path "IIS:\AppPools\Strat-Aqorynth-Frontends" -Name "startMode" -Value "AlwaysRunning"
    
    Write-Log "IIS Application Pools configured" -Level Success
}

# Function: Setup-IISWebsite
function Setup-IISWebsite {
    Write-Log "Configuring IIS Website"
    
    Import-Module WebAdministration
    
    # Remove existing site if present
    if (Get-IISSite -Name "Strat-Aqorynth" -ErrorAction SilentlyContinue) {
        Remove-IISSite -Name "Strat-Aqorynth" -Confirm:$false
    }
    
    $portalRoot = Join-Path $DeployPath "AppRationalization\frontend\build"

    # Create new website. The root site must serve the AppRationalization
    # portal build so http://localhost and /login resolve inside IIS.
    New-Website -Name "Strat-Aqorynth" `
        -PhysicalPath $portalRoot `
        -Port 80 `
        -IPAddress "*" `
        -ApplicationPool "Strat-Aqorynth-Frontends" `
        -Force
    
    Set-ItemProperty -Path "IIS:\Sites\Strat-Aqorynth" -Name "serverAutoStart" -Value $true

    $frontendApps = @(
        @{ Alias = "ca";    Path = "$DeployPath\CodeAnalysis\frontend\dist" }
        @{ Alias = "novastra-itsm";    Path = "$DeployPath\Novastra-ITSM\frontend\dist" }
        @{ Alias = "dash";  Path = "$DeployPath\Dashboard\frontend\dist" }
        @{ Alias = "infra"; Path = "$DeployPath\InfraRationalization\frontend\dist" }
        @{ Alias = "intune";Path = "$DeployPath\IntuneAutomation\frontend\dist" }
        @{ Alias = "tools"; Path = "$DeployPath\Tool_Analysis_Qualification\frontend\dist" }
        @{ Alias = "mod";   Path = "$DeployPath\Modernization\frontend\dist" }
        @{ Alias = "lab";   Path = "$DeployPath\LabRobot\frontend\dist" }
        @{ Alias = "ssdlc"; Path = "$DeployPath\SSDLC_Process_Assessment\frontend\dist" }
    )

    foreach ($frontend in $frontendApps) {
        $appPath = "IIS:\Sites\Strat-Aqorynth\$($frontend.Alias)"
        if (Test-Path $appPath) {
            Remove-WebApplication -Site "Strat-Aqorynth" -Name $frontend.Alias
        }
        New-WebApplication `
            -Site "Strat-Aqorynth" `
            -Name $frontend.Alias `
            -PhysicalPath $frontend.Path `
            -ApplicationPool "Strat-Aqorynth-Frontends" | Out-Null
    }
    
    Write-Log "IIS Website configured" -Level Success
}

# ============================================================================
# Phase 4: Application Deployment
# ============================================================================

# Function: Setup-PythonVenvs
function Setup-PythonVenvs {
    Write-Log "Setting up Python virtual environments"
    
    $projects = @(
        @{ Name = "AppRationalization";       Path = "$DeployPath\AppRationalization\backend" }
        @{ Name = "CodeAnalysis";             Path = "$DeployPath\CodeAnalysis" }
        @{ Name = "Novastra-ITSM";     Path = "$DeployPath\Novastra-ITSM" }
        @{ Name = "Dashboard";               Path = "$DeployPath\Dashboard\backend" }
        @{ Name = "IntuneAutomation";        Path = "$DeployPath\IntuneAutomation\backend" }
        @{ Name = "Tool_Analysis_Qualification"; Path = "$DeployPath\Tool_Analysis_Qualification\backend" }
        @{ Name = "Modernization";           Path = "$DeployPath\Modernization" }
        @{ Name = "LabRobot";                Path = "$DeployPath\LabRobot\backend" }
        @{ Name = "SSDLCAssessment";         Path = "$DeployPath\SSDLC_Process_Assessment\backend" }
    )
    
    foreach ($project in $projects) {
        Write-Log "  Setting up $($project.Name)..."
        
        $venvPath = "$($project.Path)\.venv"
        if (-not (Test-Path $venvPath)) {
            python -m venv $venvPath
        }
        
        # Activate and install requirements
        $activateScript = "$venvPath\Scripts\Activate.ps1"
        & $activateScript
        
        python -m pip install --upgrade pip setuptools wheel
        
        $reqFile = "$($project.Path)\requirements.txt"
        if (Test-Path $reqFile) {
            pip install -r $reqFile
        }
        
        Write-Log "     $($project.Name) venv ready" -Level Success
    }
    
    Write-Log "Python virtual environments setup complete" -Level Success
}

# Function: Setup-DotEnvFiles
function Setup-DotEnvFiles {
    Write-Log "Configuring .env files"

    # AppRationalization
    $AppRationalizationEnv = @(
        "FLASK_ENV=production",
        "FLASK_DEBUG=false",
        "FLASK_HOST=127.0.0.1",
        "DATABASE_PROVIDER=sqlite",
        "DATABASE_PATH=$DeployPath\AppRationalization\backend\instance\infra_assessment.db",
        "SECRET_KEY=SCu0Fo2HIFWWHyfqrRtRqNaNmRj0NY-C3mfEMcqSRSeBCOG7",
        "AUTH_TOKEN_SECRET=SCu0Fo2HIFWWHyfqrRtRqNaNmRj0NY-C3mfEMcqSRSeBCOG7",
        "AUTH_TOKEN_TTL_SECONDS=28800",
        "DEFAULT_ADMIN_USERNAME=admin",
        "DEFAULT_ADMIN_PASSWORD=UqSBUGPN7GOPj5k2ZSAjDP5u!9",
        "AUTH_SUCCESS_REDIRECT_URL=http://localhost/login",
        "CORS_ORIGINS=http://localhost,http://127.0.0.1,http://localhost:3000,http://127.0.0.1:3000,https://$Domain"
    ) -join "`r`n"
    $AppRationalizationEnv | Out-File "$DeployPath\AppRationalization\backend\.env" -Encoding UTF8
    
    # CodeAnalysis
    $CodeAnalysisEnv = @(
        "PYTHONUNBUFFERED=1",
        "LOG_LEVEL=INFO",
        "CORS_ORIGINS=http://localhost,http://localhost:5173,https://$Domain"
    ) -join "`r`n"
    $CodeAnalysisEnv | Out-File "$DeployPath\CodeAnalysis\.env" -Encoding UTF8
    
    # Novastra-ITSM
    $NovastraItsmEnv = @(
        "PYTHONUNBUFFERED=1",
        "LOG_LEVEL=INFO",
        "JWT_SECRET=SCu0Fo2HIFWWHyfqrRtRqNaNmRj0NY-C3mfEMcqSRSeBCOG7",
        "PORTAL_AUTH_TOKEN_SECRET=SCu0Fo2HIFWWHyfqrRtRqNaNmRj0NY-C3mfEMcqSRSeBCOG7",
        "OLLAMA_BASE_URL=http://localhost:11434",
        "OLLAMA_MODEL=llama3.1:8b",
        "GPU_ENABLED=true",
        "CUDA_VISIBLE_DEVICES=0",
        "CORS_ORIGINS=http://localhost,http://localhost:5177,https://$Domain"
    ) -join "`r`n"
    $NovastraItsmEnv | Out-File "$DeployPath\Novastra-ITSM\.env" -Encoding UTF8
    
    # Dashboard
    $DashboardEnv = @(
        "PYTHONUNBUFFERED=1",
        "LOG_LEVEL=INFO",
        "SERVICENOW_BASE_URL=https://dev393867.service-now.com",
        "SERVICENOW_USERNAME=admin",
        'SERVICENOW_PASSWORD=$AzP1x7=uRyO',
        "SERVICENOW_VERIFY_SSL=false",
        "CORS_ORIGINS=http://localhost,http://localhost:5178,https://$Domain"
    ) -join "`r`n"
    $DashboardEnv | Out-File "$DeployPath\Dashboard\.env" -Encoding UTF8

    # IntuneAutomation
    $IntuneEnv = @(
        "PYTHONUNBUFFERED=1",
        "LOG_LEVEL=INFO",
        "CORS_ORIGINS=http://localhost,http://localhost:5179,https://$Domain"
    ) -join "`r`n"
    $IntuneEnv | Out-File "$DeployPath\IntuneAutomation\backend\.env" -Encoding UTF8

    # Tool Analysis Qualification
    $ToolAnalysisEnv = @(
        "PYTHONUNBUFFERED=1",
        "LOG_LEVEL=INFO",
        "OLLAMA_BASE_URL=http://localhost:11434",
        "OLLAMA_MODEL=llama3.1",
        "OLLAMA_NUM_GPU=-1",
        "OLLAMA_TIMEOUT_SECONDS=180"
    ) -join "`r`n"
    $ToolAnalysisEnv | Out-File "$DeployPath\Tool_Analysis_Qualification\backend\.env" -Encoding UTF8

    # Modernization
    $ModernizationEnv = @(
        "PYTHONUNBUFFERED=1",
        "LOG_LEVEL=INFO",
        "AUTH_TOKEN_SECRET=SCu0Fo2HIFWWHyfqrRtRqNaNmRj0NY-C3mfEMcqSRSeBCOG7",
        "ALLOW_LOCAL_AUTH_BYPASS=false",
        "OLLAMA_BASE_URL=http://localhost:11434",
        "OLLAMA_NUM_GPU=-1",
        "CORS_ORIGINS=http://localhost,http://localhost:5175,https://$Domain"
    ) -join "`r`n"
    $ModernizationEnv | Out-File "$DeployPath\Modernization\.env" -Encoding UTF8

    # LabRobot
    $LabRobotEnv = @(
        "PYTHONUNBUFFERED=1",
        "LOG_LEVEL=INFO",
        "CORS_ORIGINS=http://localhost,http://localhost:7000,https://$Domain"
    ) -join "`r`n"
    $LabRobotEnv | Out-File "$DeployPath\LabRobot\backend\.env" -Encoding UTF8

    # SSDLC Process Assessment
    $SSDLCEnv = @(
        "PYTHONUNBUFFERED=1",
        "LOG_LEVEL=INFO",
        "ALLOW_LOCAL_AUTH_BYPASS=false",
        "OLLAMA_BASE_URL=http://localhost:11434",
        "OLLAMA_MODEL=llama3.1",
        "OLLAMA_NUM_GPU=-1",
        "OLLAMA_TIMEOUT_SECONDS=180"
    ) -join "`r`n"
    $SSDLCEnv | Out-File "$DeployPath\SSDLC_Process_Assessment\backend\.env" -Encoding UTF8

    Write-Log ".env files configured" -Level Success
}

# ============================================================================
# Phase 5: Windows Services Setup
# ============================================================================

# Function: Create-WindowsServices
function Create-WindowsServices {
    Write-Log "Creating Windows Services for backends"

    $appRatService = "Strat-Aqorynth-AppRationalization"
    $appRatBackendDir = "$DeployPath\AppRationalization\backend"
    $appRatPython = "$appRatBackendDir\.venv\Scripts\python.exe"
    $appRatDbPath = "$appRatBackendDir\instance\infra_assessment.db"
    $appRatCommand = "`$env:FLASK_ENV='production'; `$env:FLASK_DEBUG='false'; `$env:FLASK_HOST='127.0.0.1'; `$env:DATABASE_PROVIDER='sqlite'; `$env:DATABASE_PATH='$appRatDbPath'; `$env:SECRET_KEY='SCu0Fo2HIFWWHyfqrRtRqNaNmRj0NY-C3mfEMcqSRSeBCOG7'; `$env:AUTH_TOKEN_SECRET='SCu0Fo2HIFWWHyfqrRtRqNaNmRj0NY-C3mfEMcqSRSeBCOG7'; `$env:DEFAULT_ADMIN_USERNAME='admin'; `$env:DEFAULT_ADMIN_PASSWORD='UqSBUGPN7GOPj5k2ZSAjDP5u!9'; Set-Location '$appRatBackendDir'; & '$appRatPython' run.py"

    Write-Log "  Creating service: $appRatService"
    $existingAppRat = Get-Service -Name $appRatService -ErrorAction SilentlyContinue
    if ($existingAppRat) {
        nssm stop $appRatService
        nssm remove $appRatService confirm
    }

    nssm install $appRatService powershell.exe `
        -ExecutionPolicy Bypass `
        -NoProfile `
        -Command $appRatCommand

    nssm set $appRatService AppDirectory $appRatBackendDir
    nssm set $appRatService AppStdout "$DataPath\logs\$appRatService.out"
    nssm set $appRatService AppStderr "$DataPath\logs\$appRatService.err"
    nssm set $appRatService AppRotateFiles 1
    Set-Service -Name $appRatService -StartupType Automatic
    Write-Log "     Service created: $appRatService" -Level Success
    
    $runScript = @(
        'param(',
        '    [string]$BackendName,',
        '    [int]$Port,',
        '    [string]$AppModule',
        ')',
        '',
        '$ProjectDir = Join-Path "' + $DeployPath + '" $BackendName',
        '$VenvPath = Join-Path $ProjectDir ".venv"',
        '$LogPath = Join-Path "' + $DataPath + '\logs" "$BackendName.log"',
        '',
        '& (Join-Path $VenvPath "Scripts\Activate.ps1")',
        'Set-Location $ProjectDir',
        '',
        'python -m uvicorn $AppModule --host 127.0.0.1 --port $Port --log-level info --access-log 2>&1 | Tee-Object -FilePath $LogPath -Append'
    ) -join "`r`n"
    
    $runScript | Out-File "$DeployPath\run-backend.ps1" -Encoding UTF8
    
    $backends = @(
        @{ Name = "Strat-Aqorynth-CodeAnalysis";        Backend = "CodeAnalysis";                         Port = 8082; AppModule = "api.server:app" }
        @{ Name = "Strat-Aqorynth-Novastra-ITSM"; Backend = "Novastra-ITSM";                Port = 8086; AppModule = "backend.main:app" }
        @{ Name = "Strat-Aqorynth-Dashboard";            Backend = "Dashboard\backend";                   Port = 8087; AppModule = "main:app" }
        @{ Name = "Strat-Aqorynth-IntuneAutomation";     Backend = "IntuneAutomation\backend";            Port = 8088; AppModule = "app.main:app" }
        @{ Name = "Strat-Aqorynth-ToolAnalysis";         Backend = "Tool_Analysis_Qualification\backend"; Port = 8010; AppModule = "app.main:app" }
        @{ Name = "Strat-Aqorynth-Modernization";        Backend = "Modernization";                       Port = 8084; AppModule = "api.server:app" }
        @{ Name = "Strat-Aqorynth-LabRobot";             Backend = "LabRobot\backend";                    Port = 8000; AppModule = "main:app" }
        @{ Name = "Strat-Aqorynth-SSDLCAssessment";      Backend = "SSDLC_Process_Assessment\backend";    Port = 8091; AppModule = "app.main:app" }
    )
    
    foreach ($backend in $backends) {
        Write-Log "  Creating service: $($backend.Name)"
        
        # Remove existing
        $existingService = Get-Service -Name $backend.Name -ErrorAction SilentlyContinue
        if ($existingService) {
            nssm stop $backend.Name
            nssm remove $backend.Name confirm
        }
        
        # Create new service
        nssm install $backend.Name powershell.exe `
            -ExecutionPolicy Bypass `
            -NoProfile `
            -File "$DeployPath\run-backend.ps1" `
            -BackendName $backend.Backend `
            -Port $backend.Port `
            -AppModule $backend.AppModule
        
        nssm set $backend.Name AppDirectory "$DeployPath\$($backend.Backend)"
        nssm set $backend.Name AppStdout "$DataPath\logs\$($backend.Name).out"
        nssm set $backend.Name AppStderr "$DataPath\logs\$($backend.Name).err"
        nssm set $backend.Name AppRotateFiles 1
        
        Set-Service -Name $backend.Name -StartupType Automatic
        
        Write-Log "     Service created: $($backend.Name)" -Level Success
    }
    
    Write-Log "Windows Services created successfully" -Level Success
}

# ============================================================================
# Phase 6: Frontend Build
# ============================================================================

# Function: Install-ModernizationTypeScriptValidator
function Install-ModernizationTypeScriptValidator {
    $directory = "$DeployPath\Modernization\tools\ts-validate"
    $compilerPath = Join-Path $directory 'node_modules\typescript\lib\tsc.js'
    Write-Log "Installing Modernization TypeScript validator"
    Push-Location -LiteralPath $directory
    try {
        npm ci --ignore-scripts
        if ($LASTEXITCODE -ne 0) {
            throw "Modernization TypeScript validator restore failed with exit code $LASTEXITCODE"
        }
    } finally {
        Pop-Location
    }
    if (-not (Test-Path -LiteralPath $compilerPath -PathType Leaf)) {
        throw "Modernization TypeScript validator was not installed at $compilerPath"
    }
    Write-Log "Modernization TypeScript validator installed successfully" -Level Success
}

# Function: Build-ReactFrontends
function Build-ReactFrontends {
    Write-Log "Building React frontends"
    
    $frontends = @(
        @{ Name = "AppRationalization";       Path = "$DeployPath\AppRationalization\frontend";         Output = "build";
           EnvVars = @{
               REACT_APP_API_URL                          = "http://localhost/api"
               REACT_APP_CODE_ANALYSIS_URL                = "/ca/"
               REACT_APP_INFRA_SCAN_URL                   = "/infra/"
               REACT_APP_DASHBOARD_URL                    = "/dash"
               REACT_APP_INTUNE_AUTOMATION_URL            = "/intune/"
               REACT_APP_NOVASTRA_ITSM_URL        = "/novastra-itsm/ticket-analysis"
               REACT_APP_TOOL_ANALYSIS_QUALIFICATION_URL  = "/tools/"
               REACT_APP_MODERNIZATION_URL                = "/mod/"
               REACT_APP_LAB_ROBOT_URL                    = "/lab/"
               REACT_APP_SSDLC_PROCESS_ASSESSMENT_URL     = "/ssdlc/"
           }
        }
        @{ Name = "CodeAnalysis";             Path = "$DeployPath\CodeAnalysis\frontend" }
        @{ Name = "Novastra-ITSM";     Path = "$DeployPath\Novastra-ITSM\frontend" }
        @{ Name = "Dashboard";               Path = "$DeployPath\Dashboard\frontend" }
        @{ Name = "InfraRationalization";    Path = "$DeployPath\InfraRationalization\frontend" }
        @{ Name = "IntuneAutomation";        Path = "$DeployPath\IntuneAutomation\frontend" }
        @{ Name = "Tool_Analysis_Qualification"; Path = "$DeployPath\Tool_Analysis_Qualification\frontend" }
        @{ Name = "Modernization";           Path = "$DeployPath\Modernization\frontend" }
        @{ Name = "LabRobot";                Path = "$DeployPath\LabRobot\frontend" }
        @{ Name = "SSDLCAssessment";         Path = "$DeployPath\SSDLC_Process_Assessment\frontend" }
    )
    
    foreach ($frontend in $frontends) {
        Write-Log "  Building $($frontend.Name)..."

        Set-Location $frontend.Path
        npm ci --only=production

        # Apply any module-specific env vars for this build
        $savedEnv = @{}
        if ($frontend.ContainsKey("EnvVars")) {
            foreach ($kv in $frontend.EnvVars.GetEnumerator()) {
                $savedEnv[$kv.Key] = [System.Environment]::GetEnvironmentVariable($kv.Key)
                [System.Environment]::SetEnvironmentVariable($kv.Key, $kv.Value)
            }
        }

        npm run build

        # Restore env vars
        foreach ($kv in $savedEnv.GetEnumerator()) {
            [System.Environment]::SetEnvironmentVariable($kv.Key, $kv.Value)
        }

        $outputDir = if ($frontend.ContainsKey("Output")) { $frontend.Output } else { "dist" }
        if (Test-Path ".\$outputDir") {
            Write-Log "     $($frontend.Name) built successfully" -Level Success
        } else {
            Write-Log "     Build failed for $($frontend.Name)" -Level Error
        }
    }
    
    Write-Log "React frontends built successfully" -Level Success
}

# ============================================================================
# Phase 7: Service Startup and Verification
# ============================================================================

# Function: Start-AllServices
function Start-AllServices {
    Write-Log "Starting all services"
    
    # Start main IIS service
    Start-Service W3SVC
    Wait-Service -ServiceName W3SVC -TimeoutSeconds 30
    Write-Log "   IIS started" -Level Success
    
    # Start backend services
    $services = @("Strat-Aqorynth-AppRationalization", "Strat-Aqorynth-CodeAnalysis", "Strat-Aqorynth-Novastra-ITSM", "Strat-Aqorynth-Dashboard", "Strat-Aqorynth-IntuneAutomation", "Strat-Aqorynth-ToolAnalysis", "Strat-Aqorynth-Modernization", "Strat-Aqorynth-LabRobot", "Strat-Aqorynth-SSDLCAssessment")
    
    foreach ($svc in $services) {
        Write-Log "  Starting $svc..."
        Start-Service $svc
        
        if (Wait-Service -ServiceName $svc -TimeoutSeconds 60) {
            Write-Log "     $svc is running" -Level Success
        } else {
            Write-Log "     $svc may be slow to start" -Level Warning
        }
    }
}

# Function: Install-BackendWatchdog
function Install-BackendWatchdog {
    Write-Log "Registering master backend watchdog"

    $watchdogPath = Join-Path $DeployPath 'watchdog_all_backends.ps1'
    if (-not (Test-Path -LiteralPath $watchdogPath)) {
        throw "Master watchdog was not found at $watchdogPath"
    }

    $taskName = 'Strat-Aqorynth-Master-Watchdog'
    $powerShell = Join-Path $PSHOME 'powershell.exe'
    $action = New-ScheduledTaskAction `
        -Execute $powerShell `
        -Argument "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$watchdogPath`""
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal `
        -UserId 'SYSTEM' `
        -LogonType ServiceAccount `
        -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -RestartCount 10 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)

    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName

    Write-Log "Master backend watchdog registered and started" -Level Success
}

# Function: Verify-Health
function Verify-Health {
    Write-Log "Verifying service health"
    
    $checks = @(
        @{ Name = "IIS";                  URL = "http://localhost:80/" }
        @{ Name = "AppRationalization";   URL = "http://localhost:5000/api/health" }
        @{ Name = "CodeAnalysis";         URL = "http://localhost:8082/health" }
        @{ Name = "Novastra-ITSM"; URL = "http://localhost:8086/health" }
        @{ Name = "Dashboard";            URL = "http://localhost:8087/health" }
        @{ Name = "IntuneAutomation";     URL = "http://localhost:8088/api/health" }
        @{ Name = "ToolAnalysis";         URL = "http://localhost:8010/api/health" }
        @{ Name = "Modernization";        URL = "http://localhost:8084/api/health" }
        @{ Name = "LabRobot";             URL = "http://localhost:8000/api/health" }
        @{ Name = "SSDLCAssessment";      URL = "http://localhost:8091/api/health" }
        @{ Name = "SCM KG";                URL = "http://localhost:8001/health" }
        @{ Name = "SCM Signal Inspector";  URL = "http://localhost:8003/health" }
        @{ Name = "SCM Agent Service";     URL = "http://localhost:8002/health"; Headers = @{ "X-API-Key" = "agent-dev-key-change-in-prod" } }
        @{ Name = "SCM KG via IIS";        URL = "http://localhost:8090/api/kg/health" }
        @{ Name = "SCM Inspector via IIS"; URL = "http://localhost:8090/api/inspector/health" }
        @{ Name = "SCM Agent via IIS";     URL = "http://localhost:8090/api/agents/health"; Headers = @{ "X-API-Key" = "agent-dev-key-change-in-prod" } }
    )
    
    foreach ($check in $checks) {
        try {
            $requestArgs = @{
                Uri         = $check.URL
                ErrorAction = 'Stop'
                TimeoutSec  = 10
            }
            if ($check.Headers) { $requestArgs.Headers = $check.Headers }
            $response = Invoke-WebRequest @requestArgs
            Write-Log "   $($check.Name) responding" -Level Success
        } catch {
            Write-Log "   $($check.Name) not responding" -Level Error
        }
    }
}

# ============================================================================
# Main Execution
# ============================================================================

# Function: Main
function Main {
    Write-Log "========================================" -Level Info
    Write-Log "Strat-Aqorynth IIS Deployment Automation" -Level Info
    Write-Log "========================================" -Level Info
    
    if ($TestOnly) {
        Write-Log "TEST MODE: No changes will be made"
        return
    }
    
    try {
        Initialize-WindowsEnvironment
        Install-Python
        Install-NodeJS
        Install-Git
        Install-URLRewrite
        Configure-IISReverseProxy
        Install-NSSM
        
        Initialize-DirectoryStructure
        Setup-IISAppPools
        Setup-IISWebsite
        
        Setup-PythonVenvs
        Setup-DotEnvFiles
        Install-ModernizationTypeScriptValidator
        Create-WindowsServices
        
        Build-ReactFrontends
        
        Start-AllServices
        Install-BackendWatchdog
        Verify-Health
        
        Write-Log "========================================" -Level Success
        Write-Log "Deployment completed successfully!" -Level Success
        Write-Log "========================================" -Level Success
        Write-Log "Access your application at: http://localhost or https://$Domain" -Level Info
        
    } catch {
        Write-Log "Deployment failed: $_" -Level Error
        Write-Log "Check logs at: $LogPath" -Level Error
        exit 1
    }
}

# Run main
Main
