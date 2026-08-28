# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Create-IIS-Strat-AqorynthProjectSite.ps1 — Create-IIS-Strat-AqorynthProjectSite (Create-IIS-Strat-AqorynthProjectSite.ps1)
# Date: 2026-05-14
# ---------------------------------------------------------------------------
[CmdletBinding()]
param(
    [string]$SiteName = 'StratApp',
    [int]$Port = 80,
    [string[]]$HostHeaders = @('stratapp.org', 'www.stratapp.org'),
    [string]$AppPoolName = 'StratApp-Frontends',
    [string]$ReportPath = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

trap {
    if ($ReportPath) {
        [ordered]@{
            Success = $false
            Error = $_.Exception.Message
            Position = $_.InvocationInfo.PositionMessage
            FailedAt = (Get-Date).ToString('o')
        } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $ReportPath -Encoding UTF8
    }
    exit 1
}

$repoRoot = Split-Path -Parent $PSCommandPath
$portalPath = Join-Path $repoRoot 'AppRationalization\frontend\build'
$apps = @(
    @{ Name = 'ca'; Path = Join-Path $repoRoot 'CodeAnalysis\frontend\dist' },
    @{ Name = 'infra'; Path = Join-Path $repoRoot 'InfraRationalization\frontend\dist' },
    @{ Name = 'novastra-itsm'; Path = Join-Path $repoRoot 'Novastra-ITSM\frontend\dist' },
    @{ Name = 'dash'; Path = Join-Path $repoRoot 'Dashboard\frontend\dist' },
    @{ Name = 'ssdlc'; Path = Join-Path $repoRoot 'SSDLC_Process_Assessment\frontend\dist' },
    @{ Name = 'mod'; Path = Join-Path $repoRoot 'Modernization\frontend\dist' },
    @{ Name = 'lab'; Path = Join-Path $repoRoot 'LabRobot\frontend\dist' },
    @{ Name = 'ot'; Path = Join-Path $repoRoot 'OpportunityTracker\frontend\dist' },
    @{ Name = 'reman'; Path = Join-Path $repoRoot 'AI_Reman_Core\build' },
    @{ Name = 'vl'; Path = Join-Path $repoRoot 'AI_Vehicle_Loan\frontend\build' },
    @{ Name = 'mda'; Path = Join-Path $repoRoot 'Microsite_Data_Analysis\dist' },
    @{ Name = 'scm'; Path = Join-Path $repoRoot 'supply-chain-disruption-manager\apps\web-ui\dist' },
    @{ Name = 'tf'; Path = Join-Path $repoRoot 'TraceForge\ui\dist' }
)

if (-not (Test-Path -LiteralPath $portalPath)) {
    throw "Portal build output not found: $portalPath"
}
foreach ($app in $apps) {
    if (-not (Test-Path -LiteralPath $app.Path)) {
        throw "Build output not found for /$($app.Name): $($app.Path)"
    }
}

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator privileges are required to configure IIS.'
}

$requiredFeatures = @(
    'IIS-WebServerRole',
    'IIS-WebServer',
    'IIS-CommonHttpFeatures',
    'IIS-DefaultDocument',
    'IIS-HttpErrors',
    'IIS-StaticContent',
    'IIS-HttpLogging',
    'IIS-RequestFiltering',
    'IIS-WebSockets'
)
foreach ($featureName in $requiredFeatures) {
    $feature = Get-WindowsOptionalFeature -Online -FeatureName $featureName
    if ($feature.State -ne 'Enabled') {
        Enable-WindowsOptionalFeature -Online -FeatureName $featureName -All -NoRestart | Out-Null
    }
}

Import-Module WebAdministration -ErrorAction Stop

$rewriteModule = Get-WebGlobalModule | Where-Object Name -eq 'RewriteModule'
if (-not $rewriteModule) {
    throw 'IIS URL Rewrite is required but is not installed.'
}
$arrModule = Get-WebGlobalModule | Where-Object Name -eq 'ApplicationRequestRouting'
if (-not $arrModule) {
    throw 'IIS Application Request Routing is required but is not installed.'
}

if (-not (Test-Path "IIS:\AppPools\$AppPoolName")) {
    New-WebAppPool -Name $AppPoolName | Out-Null
}
Set-ItemProperty -Path "IIS:\AppPools\$AppPoolName" -Name managedRuntimeVersion -Value ''
Set-ItemProperty -Path "IIS:\AppPools\$AppPoolName" -Name startMode -Value AlwaysRunning
# Disable idle timeout — startMode=AlwaysRunning alone isn't always sufficient to keep the
# worker process responsive; an idle pool was observed going unresponsive overnight (still
# "Started" but not serving requests) even with AlwaysRunning set. See deployment.md.
Set-ItemProperty -Path "IIS:\AppPools\$AppPoolName" -Name processModel.idleTimeout -Value '00:00:00'

if (-not (Test-Path "IIS:\Sites\$SiteName")) {
    $firstHostHeader = if ($HostHeaders.Count -gt 0) { $HostHeaders[0] } else { '' }
    New-Website -Name $SiteName -Port $Port -HostHeader $firstHostHeader -PhysicalPath $portalPath -ApplicationPool $AppPoolName | Out-Null
} else {
    Set-ItemProperty -Path "IIS:\Sites\$SiteName" -Name physicalPath -Value $portalPath
    Set-ItemProperty -Path "IIS:\Sites\$SiteName" -Name applicationPool -Value $AppPoolName
}

$desiredBindings = @($HostHeaders | ForEach-Object { "*:${Port}:$_" })
$site = Get-Website -Name $SiteName
foreach ($binding in @($site.bindings.Collection)) {
    if ($binding.protocol -eq 'http' -and
        $binding.bindingInformation -like "*:${Port}:*" -and
        $binding.bindingInformation -notin $desiredBindings) {
        Remove-WebBinding -Name $SiteName -Protocol http -BindingInformation $binding.bindingInformation
    }
}

foreach ($hostHeader in $HostHeaders) {
    $bindingInfo = "*:${Port}:${hostHeader}"
    $site = Get-Website -Name $SiteName
    $hasBinding = $site.bindings.Collection | Where-Object { $_.bindingInformation -eq $bindingInfo -and $_.protocol -eq 'http' }
    if (-not $hasBinding) {
        New-WebBinding -Name $SiteName -Protocol http -Port $Port -HostHeader $hostHeader | Out-Null
    }
}

Set-ItemProperty -Path "IIS:\Sites\$SiteName" -Name serverAutoStart -Value $true

foreach ($app in $apps) {
    $appPath = "IIS:\Sites\$SiteName\$($app.Name)"
    if (Test-Path $appPath) {
        Set-ItemProperty -Path $appPath -Name physicalPath -Value $app.Path
        Set-ItemProperty -Path $appPath -Name applicationPool -Value $AppPoolName
    } else {
        New-WebApplication -Site $SiteName -Name $app.Name -PhysicalPath $app.Path -ApplicationPool $AppPoolName | Out-Null
    }
}

Start-WebAppPool -Name $AppPoolName -ErrorAction SilentlyContinue
Start-Website -Name $SiteName

$proxy = Get-WebConfiguration -Filter 'system.webServer/proxy' -PSPath 'IIS:\'
if ($null -eq $proxy) {
    throw 'IIS ARR proxy configuration is unavailable.'
}
Set-WebConfigurationProperty -Filter 'system.webServer/proxy' -PSPath 'IIS:\' -Name enabled -Value $true
Set-WebConfigurationProperty -Filter 'system.webServer/proxy' -PSPath 'IIS:\' -Name bufferChunkedResponses -Value $false

$site = Get-Website -Name $SiteName
$report = [ordered]@{
    Success = $true
    SiteName = $SiteName
    State = [string]$site.State
    PhysicalPath = $portalPath
    ApplicationPool = $AppPoolName
    Bindings = @($site.bindings.Collection | ForEach-Object { "$($_.protocol)://$($_.bindingInformation)" })
    Applications = @($apps | ForEach-Object { "/$($_.Name)" })
    RewriteModule = [bool]$rewriteModule
    ArrModule = [bool]$arrModule
    ArrProxyEnabled = $true
    ConfiguredAt = (Get-Date).ToString('o')
}
if ($ReportPath) {
    $report | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $ReportPath -Encoding UTF8
}

Write-Host "IIS site ready on port $Port for $($HostHeaders -join ', ')" -ForegroundColor Green
foreach ($app in $apps) {
    Write-Host "  /$($app.Name)/ -> $($app.Path)" -ForegroundColor Green
}
