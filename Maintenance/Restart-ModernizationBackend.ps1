[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Administrator privileges are required. Open PowerShell as Administrator and run: .\Maintenance\Restart-ModernizationBackend.ps1"
}

$port = 8084
$repoRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $repoRoot 'Modernization'
$configuredPythonExe = Join-Path $backendDir '.venv\Scripts\python.exe'
$pythonExe = if (Test-Path -LiteralPath $configuredPythonExe) { $configuredPythonExe } else { $null }
$logDir = Join-Path $backendDir 'data\logs'
$logFile = Join-Path $logDir 'administrator-backend-restart.log'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Start-Transcript -Path $logFile -Append | Out-Null

try {
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($listener) {
        $worker = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
        $parent = if ($worker) {
            Get-CimInstance Win32_Process -Filter "ProcessId=$($worker.ParentProcessId)"
        } else {
            $null
        }
        $candidate = if ($parent -and $parent.Name -eq 'python.exe') {
            $parent
        } elseif ($worker -and $worker.Name -eq 'python.exe') {
            $worker
        } else {
            throw "Port $port is not owned by the expected Python worker; refusing to terminate it."
        }
        # Keep the restarted service bound to this checkout. Only reuse the
        # previous process interpreter when this checkout has no local venv.
        if (-not $pythonExe -and $candidate.ExecutablePath) {
            $pythonExe = $candidate.ExecutablePath
        } elseif (-not $pythonExe -and $worker.ExecutablePath) {
            $pythonExe = $worker.ExecutablePath
        }
        Write-Host "Stopping Modernization process tree $($candidate.ProcessId) on port $port."
        & taskkill.exe /PID $candidate.ProcessId /T /F | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to terminate Modernization process tree $($candidate.ProcessId)."
        }
    }

    $deadline = (Get-Date).AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 500
        $listening = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    } while ($listening -and (Get-Date) -lt $deadline)

    if ($listening) {
        throw "Port $port did not stop listening after terminating the previous Modernization process."
    }

    if (-not $pythonExe -or -not (Test-Path -LiteralPath $pythonExe)) {
        throw 'No usable Python executable could be resolved from the previous Modernization process.'
    }
    . (Join-Path $PSScriptRoot 'Shared-Auth.ps1')
    $env:AUTH_TOKEN_SECRET = Get-Strat-AqorynthSharedAuthSecret -RepoRoot $repoRoot
    $env:AUTH_TOKEN_TTL_SECONDS = '28800'
    $env:AUTH_REQUIRED = 'true'
    $env:CORS_ORIGINS = 'http://localhost,http://127.0.0.1,http://localhost:8090,http://127.0.0.1:8090,http://localhost:3000,http://127.0.0.1:3000,https://stratapp.org'
    $env:OLLAMA_BASE_URL = 'http://localhost:11434'
    Start-Process -FilePath $pythonExe `
        -ArgumentList @('-m', 'uvicorn', 'api.server:app', '--host', '0.0.0.0', '--port', '8084', '--log-level', 'info') `
        -WorkingDirectory $backendDir -WindowStyle Hidden

    $deadline = (Get-Date).AddSeconds(30)
    do {
        Start-Sleep -Seconds 1
        $listening = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    } while (-not $listening -and (Get-Date) -lt $deadline)
    if (-not $listening) {
        throw 'Modernization backend did not begin listening after launch.'
    }
    Write-Host "Started Modernization backend from $backendDir."
} catch {
    Write-Error $_
    throw
} finally {
    Stop-Transcript | Out-Null
}
