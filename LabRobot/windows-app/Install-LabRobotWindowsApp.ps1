param(
    [string]$InstallDirectory = "$env:LOCALAPPDATA\StratAqorynth\LabRobotWindowsApp",
    [string]$RuntimeIdentifier = "win-x64"
)

$ErrorActionPreference = 'Stop'
$projectDirectory = $PSScriptRoot
$projectFile = Join-Path $projectDirectory 'LabRobot.WindowsApp.csproj'
$publishDirectory = Join-Path $projectDirectory 'publish'

dotnet publish $projectFile -c Release -r $RuntimeIdentifier --self-contained false -o $publishDirectory
if ($LASTEXITCODE -ne 0) { throw 'Lab Robot Windows App publish failed.' }

New-Item -ItemType Directory -Path $InstallDirectory -Force | Out-Null
Copy-Item -Path (Join-Path $publishDirectory '*') -Destination $InstallDirectory -Recurse -Force

$executable = Join-Path $InstallDirectory 'LabRobot.WindowsApp.exe'
if (-not (Test-Path -LiteralPath $executable)) { throw "Published executable not found: $executable" }

$protocolRoot = 'HKCU:\Software\Classes\stratiq-labrobot'
New-Item -Path $protocolRoot -Force | Out-Null
Set-Item -Path $protocolRoot -Value 'URL:Strat-Aqorynth Lab Robot Protocol'
New-ItemProperty -Path $protocolRoot -Name 'URL Protocol' -Value '' -PropertyType String -Force | Out-Null
New-Item -Path "$protocolRoot\DefaultIcon" -Force | Out-Null
Set-Item -Path "$protocolRoot\DefaultIcon" -Value "`"$executable`",0"
New-Item -Path "$protocolRoot\shell\open\command" -Force | Out-Null
Set-Item -Path "$protocolRoot\shell\open\command" -Value "`"$executable`" `"%1`""

Write-Host "Lab Robot Windows App installed: $executable"
Write-Host 'Protocol registered: stratiq-labrobot://'
Write-Host 'Launch test: stratiq-labrobot://open/lab-robot'
