# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Maintenance — Auto-Git-Sync (Auto-Git-Sync.ps1)
# Date: 2025-11-10
# ---------------------------------------------------------------------------
<#
.SYNOPSIS
    Auto-commits and pushes source changes in this repository to
  vishnuu0001/vinvisdiv on a schedule.

.DESCRIPTION
  Respects .gitignore (.venv, node_modules, build/dist output, runtime DB/log files
  are all excluded there). Does nothing if there are no changes to commit. Runs as
  the interactive StratDev\stratdev user (via the Strat-Aqorynth-Auto-Git-Sync scheduled
  task) so it can use the same cached Git Credential Manager credentials used for
  the initial manual push.
#>

$ErrorActionPreference = 'Continue'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogFile  = Join-Path $PSScriptRoot 'auto-git-sync.log'
$ExpectedRemote = 'https://github.com/vishnuu0001/vinvisdiv.git'
$PublishedCommitFile = Join-Path $PSScriptRoot '.last-published-commit'
$MaximumGitBlobBytes = 95MB
$ProductionBranch = 'main'

$GitCandidates = @(
    'C:\Program Files\Git\cmd\git.exe',
    'C:\Program Files\Git\bin\git.exe',
    "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe"
)
$GitExe = $GitCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1

# Function: Write-Log
function Write-Log {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') | $Message"
    Add-Content -Path $LogFile -Value $line
    Write-Host $line
}

Set-Location $RepoRoot

if (-not $GitExe) {
    Write-Log 'ERROR: git.exe not found. Install Git for Windows or update Maintenance/Auto-Git-Sync.ps1 Git candidates.'
    exit 1
}

Set-Alias -Name git -Value $GitExe -Scope Script

$mutex = New-Object System.Threading.Mutex($false, 'Strat-Aqorynth-Auto-Git-Sync')
if (-not $mutex.WaitOne(0)) {
    Write-Log 'Another synchronization run is active; skipping this invocation.'
    exit 0
}

try {
    $actualRemote = (git remote get-url origin 2>$null).Trim()
    if ($LASTEXITCODE -ne 0 -or $actualRemote -ne $ExpectedRemote) {
        Write-Log "ERROR: origin is '$actualRemote'; expected '$ExpectedRemote'. Nothing was committed."
        exit 1
    }

    $currentBranch = (git branch --show-current 2>$null).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $currentBranch) {
        Write-Log 'ERROR: Could not determine the current branch. Nothing was committed.'
        exit 1
    }

    # Keep the remote commit from before the push. This is required only when
    # main is published; feature branches are committed and pushed without
    # changing production.
    $remoteHeadBeforePush = (git rev-parse "origin/$currentBranch" 2>$null).Trim()
    $remoteBranchExists = $LASTEXITCODE -eq 0 -and $remoteHeadBeforePush

    # Generated modernization workspaces are ignored at repository level, so
    # standard staging includes source changes without sweeping in runtime data.
    git add -A
    if ($LASTEXITCODE -ne 0) {
        Write-Log 'ERROR: git add failed.'
        exit 1
    }

    $oversizedFiles = @()
    $stagedFiles = @(git diff --cached --name-only --diff-filter=ACMR)
    foreach ($path in $stagedFiles) {
        $sizeText = git cat-file -s ":$path" 2>$null
        if ($LASTEXITCODE -eq 0 -and [long]$sizeText -gt $MaximumGitBlobBytes) {
            $oversizedFiles += "$path ($([math]::Round([long]$sizeText / 1MB, 1)) MB)"
        }
    }
    if ($oversizedFiles.Count -gt 0) {
        Write-Log "ERROR: Refusing to commit files above 95 MB: $($oversizedFiles -join ', ')."
        exit 1
    }

    git diff --cached --quiet
    if ($LASTEXITCODE -ne 0) {
        $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
        $commitMsg = "Auto-sync: $ts"
        $commitOut = git commit -m "$commitMsg" 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Log "ERROR: Commit failed - $commitOut"
            exit 1
        }
        Write-Log "Committed: $commitMsg"
    }

    $headCommit = (git rev-parse HEAD).Trim()

    # GitHub is the release source of truth. Never publish a local-only commit:
    # push first, then deploy the exact commit now present on origin/main.
    $aheadCount = if ($remoteBranchExists) {
        [int](git rev-list --count "origin/$currentBranch..HEAD")
    } else {
        [int](git rev-list --count HEAD)
    }
    if ($aheadCount -gt 0) {
        $pushOut = git push --set-upstream origin $currentBranch 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Log "Pushed $aheadCount local commit(s) successfully."
        } else {
            Write-Log "ERROR: Push failed; production was not changed and the next run will retry - $pushOut"
            exit 1
        }
    } else {
        Write-Log 'GitHub already matches the local branch.'
    }

    if ($currentBranch -ne $ProductionBranch) {
        Write-Log "Branch '$currentBranch' was synchronized; production publishing is restricted to '$ProductionBranch'."
        exit 0
    }

    if (-not $remoteBranchExists) {
        Write-Log "ERROR: The previous origin/$ProductionBranch commit could not be resolved; skipping production publish."
        exit 1
    }

    $publishedCommit = if (Test-Path -LiteralPath $PublishedCommitFile) {
        (Get-Content -LiteralPath $PublishedCommitFile -Raw).Trim()
    } else {
        ''
    }
    $publishedCommitIsValid = $false
    if ($publishedCommit) {
        git merge-base --is-ancestor $publishedCommit HEAD 2>$null
        $publishedCommitIsValid = $LASTEXITCODE -eq 0
    }

    $publishBase = if ($publishedCommitIsValid) { $publishedCommit } else { $remoteHeadBeforePush }
    $changedFiles = @(git diff --name-only "$publishBase..HEAD")
    if ($changedFiles.Count -gt 0) {
        $publishScript = Join-Path $PSScriptRoot 'Publish-Production.ps1'
        try {
            # A change anywhere in the repository can affect shared portal/IIS
            # behavior, so automated releases deliberately rebuild and restart
            # the complete application set. Publish-Production.ps1 retains its
            # selective default for ad-hoc/manual releases.
            & $publishScript -ChangedFiles $changedFiles -PublishAll
            Set-Content -LiteralPath $PublishedCommitFile -Value $headCommit -Encoding ASCII
            Write-Log "Published commit $headCommit to production successfully."
        } catch {
            # $_.Exception.Message alone can be a bare ANSI escape fragment when the
            # failure originates from a native command's colored stderr output under
            # $ErrorActionPreference='Stop' in Publish-Production.ps1 — log full detail
            # (category, target, script line) so a real cause is diagnosable, not just
            # a truncated "[33m".
            Write-Log "ERROR: Production publish failed - $($_.Exception.Message)"
            $stackFlat = if ($_.ScriptStackTrace) { $_.ScriptStackTrace -replace "`r?`n", ' | ' } else { '(no stack trace)' }
            Write-Log "ERROR detail: Category=$($_.CategoryInfo.Category) Reason=$($_.CategoryInfo.Reason) TargetName=$($_.CategoryInfo.TargetName) ScriptStackTrace=$stackFlat"
        }
    } else {
        Write-Log 'Production already matches the current local commit.'
    }
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}

# Keep this script's own log from growing unbounded
if (Test-Path $LogFile) {
    $lines = Get-Content $LogFile
    if ($lines.Count -gt 2000) {
        $lines[-2000..-1] | Set-Content $LogFile
    }
}
