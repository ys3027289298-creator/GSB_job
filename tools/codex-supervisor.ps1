param(
    [Parameter(Mandatory = $true)] [string] $TaskId,
    [Parameter(Mandatory = $true)] [ValidateSet('A', 'B')] [string] $Side,
    [Parameter(Mandatory = $true)] [string] $Repo,
    [Parameter(Mandatory = $true)] [string] $Workspace,
    [Parameter(Mandatory = $true)] [string] $PromptFile,
    [Parameter(Mandatory = $true)] [string] $CodexCmd
)

$ErrorActionPreference = 'Stop'
$TaskId = $TaskId.ToUpperInvariant()
$Side = $Side.ToUpperInvariant()
$TaskScript = Join-Path $Repo 'tools\task.py'
$AttemptScript = Join-Path $PSScriptRoot 'codex-attempt.ps1'
$Uv = (Get-Command uv -ErrorAction Stop).Source
$PortLeaseBase = $env:LOCALAPPDATA
if (-not $PortLeaseBase) { $PortLeaseBase = $env:TEMP }
$PortLeaseRoot = Join-Path $PortLeaseBase 'GSB-port-leases'
$PortLockPath = Join-Path $PortLeaseRoot '.lock'
$PortStart = 3101
$PortEnd = 3999

function Test-PortFree([int] $Port) {
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new(
            [System.Net.IPAddress]::Any, $Port
        )
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($listener) { $listener.Stop() }
    }
}

function Acquire-PortLease {
    New-Item -ItemType Directory -Force -Path $PortLeaseRoot | Out-Null
    $lock = $null
    while ($true) {
        try {
            $lock = [System.IO.File]::Open(
                $PortLockPath,
                [System.IO.FileMode]::CreateNew,
                [System.IO.FileAccess]::Write,
                [System.IO.FileShare]::None
            )
            break
        } catch [System.IO.IOException] {
            try {
                if ((Get-Item -LiteralPath $PortLockPath -ErrorAction Stop).LastWriteTime -lt (Get-Date).AddMinutes(-2)) {
                    Remove-Item -LiteralPath $PortLockPath -Force -ErrorAction SilentlyContinue
                    continue
                }
            } catch {}
            Start-Sleep -Milliseconds 100
        }
    }

    try {
        foreach ($file in @(Get-ChildItem -LiteralPath $PortLeaseRoot -Filter '*.lease' -File -ErrorAction SilentlyContinue)) {
            $stale = $false
            try {
                $leaseInfo = Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
                $owner = Get-Process -Id ([int]$leaseInfo.pid) -ErrorAction SilentlyContinue
                $stale = $null -eq $owner
            } catch {
                $stale = $true
            }
            if ($stale) { Remove-Item -LiteralPath $file.FullName -Force -ErrorAction SilentlyContinue }
        }

        for ($port = $PortStart; $port -le $PortEnd; $port++) {
            if ($port -eq 3000) { continue }
            $leasePath = Join-Path $PortLeaseRoot ("{0}.lease" -f $port)
            if (Test-Path -LiteralPath $leasePath) { continue }
            if (-not (Test-PortFree $port)) { continue }
            $record = [ordered]@{
                pid = $PID
                task = $TaskId
                side = $Side
                port = $port
                created_at = (Get-Date).ToString('o')
            }
            [System.IO.File]::WriteAllText(
                $leasePath,
                (($record | ConvertTo-Json -Compress) + [Environment]::NewLine),
                [Text.Encoding]::UTF8
            )
            return [pscustomobject]@{ Port = $port; Path = $leasePath }
        }
        throw "没有可用的独立端口（候选范围 $PortStart-$PortEnd，已排除 3000）"
    } finally {
        if ($lock) { $lock.Dispose() }
        Remove-Item -LiteralPath $PortLockPath -Force -ErrorAction SilentlyContinue
    }
}

function Release-PortLease($Lease) {
    if ($Lease -and $Lease.Path) {
        Remove-Item -LiteralPath $Lease.Path -Force -ErrorAction SilentlyContinue
    }
}

function Normalize-Path([string] $Path) {
    try {
        return ([System.IO.Path]::GetFullPath($Path)).TrimEnd('\').ToLowerInvariant()
    } catch {
        return $Path.TrimEnd('\').ToLowerInvariant()
    }
}

function Get-CodexSessionRoots {
    $roots = New-Object System.Collections.Generic.List[string]
    $home = $env:USERPROFILE
    $roots.Add((Join-Path $home '.codex\sessions'))
    $roots.Add((Join-Path $home '.codex-cli-relay\sessions'))
    if ($env:CODEX_HOME) {
        $roots.Add((Join-Path $env:CODEX_HOME 'sessions'))
    }
    Get-ChildItem -LiteralPath $home -Directory -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like '.codex*' } |
        ForEach-Object { $roots.Add((Join-Path $_.FullName 'sessions')) }
    return @($roots | Select-Object -Unique)
}

function Get-RolloutCwd([string] $Path) {
    try {
        $line = Get-Content -LiteralPath $Path -TotalCount 1 -Encoding UTF8 -ErrorAction Stop
        $event = $line | ConvertFrom-Json
        return [string]$event.payload.cwd
    } catch {
        return ''
    }
}

function Find-SessionFile([string] $TargetWorkspace, [Int64] $StartedMs) {
    $target = Normalize-Path $TargetWorkspace
    $candidates = @()
    foreach ($root in @(Get-CodexSessionRoots)) {
        if (-not (Test-Path -LiteralPath $root -PathType Container)) { continue }
        $files = Get-ChildItem -LiteralPath $root -Recurse -File -Filter 'rollout-*.jsonl' -ErrorAction SilentlyContinue
        foreach ($file in @($files)) {
            try {
                $mtime = ([DateTimeOffset]$file.LastWriteTimeUtc).ToUnixTimeMilliseconds()
                if ($StartedMs -and $mtime -lt ($StartedMs - 15000)) { continue }
                $cwd = Get-RolloutCwd $file.FullName
                if ($cwd -and (Normalize-Path $cwd) -eq $target) {
                    $candidates += [pscustomobject]@{ Path = $file.FullName; Mtime = $mtime }
                }
            } catch {}
        }
    }
    $best = $candidates | Sort-Object Mtime -Descending | Select-Object -First 1
    if ($best) { return $best.Path }
    return ''
}

function Get-SessionStatus([string] $Path) {
    try {
        $lines = @(Get-Content -LiteralPath $Path -Tail 300 -Encoding UTF8 -ErrorAction Stop)
    } catch {
        return 'running'
    }

    foreach ($line in $lines) {
        try {
            $event = $line | ConvertFrom-Json
            $payload = $event.payload
            $payloadError = $null
            if ($payload -and $payload.PSObject.Properties['error']) {
                $payloadError = $payload.error
            }
            if ($event.type -eq 'event_msg' -and $payload.type -eq 'turn_aborted') {
                return 'failed'
            }
            if ($event.type -eq 'event_msg' -and $payload.type -eq 'task_complete') {
                if ($payloadError) { return 'failed' }
                return 'success'
            }
            if ($payloadError) {
                # 504 可能只是 Codex CLI 正在执行自己的 Reconnecting 重试；
                # 只有出现 task_complete 错误，或进程最终退出，才进入重跑流程。
                if ([string]$payloadError -match '(?i)(504\s+gateway\s+timeout|gateway\s+timeout|http[_\s-]*status[_\s-]*code\s*[:=]\s*504)') {
                    continue
                }
                return 'failed'
            }
        } catch {}
        if ($line -match '(?i)(reconnecting\s*\.{0,3}\s*\d+\s*/\s*\d+|504\s+gateway\s+timeout|gateway\s+timeout|http[_\s-]*status[_\s-]*code\s*[:=]\s*504)') {
            # 不因单次 504 立即杀进程，等待 Codex CLI 自己重连到最终结果。
            continue
        }
        if ($line -match '(?i)(status\s*[:=]\s*(409|429|504)|http[_\s-]*status[_\s-]*code\s*[:=]\s*(409|429|504)|504\s+gateway\s+timeout|gateway\s+timeout|too many requests|request.*(limit|failed)|timed out|timeout|network.*(error|timeout)|connection.*(reset|closed|failed))') {
            return 'failed'
        }
    }
    return 'running'
}

function Stop-ProcessTree([int] $ProcessId) {
    $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $ProcessId" -ErrorAction SilentlyContinue)
    foreach ($child in $children) {
        Stop-ProcessTree ([int]$child.ProcessId)
    }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

function Start-CodexAttempt([int] $Port) {
    $arguments = @(
        '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $AttemptScript),
        '-TaskId', ('"{0}"' -f $TaskId), '-Side', ('"{0}"' -f $Side),
        '-Workspace', ('"{0}"' -f $Workspace), '-PromptFile', ('"{0}"' -f $PromptFile),
        '-Port', ('"{0}"' -f $Port),
        '-CodexCmd', ('"{0}"' -f $CodexCmd)
    )
    return Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments -WorkingDirectory $Workspace -WindowStyle Normal -PassThru
}

Write-Host "[supervisor] $TaskId-$Side will retry every interrupted Codex run until task_complete succeeds."

$portLease = Acquire-PortLease
$assignedPort = [int]$portLease.Port
Write-Host "[supervisor] allocated unique development port $assignedPort (3000 is forbidden)."

$attemptNumber = 0
try {
while ($true) {
    $attemptNumber++
    Write-Host "[supervisor] preparing attempt $attemptNumber ..."
    & $Uv run python $TaskScript cycle $TaskId --side $Side
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[supervisor] prepare failed with code $LASTEXITCODE."
        exit $LASTEXITCODE
    }

    $startedMs = ([DateTimeOffset]::UtcNow).ToUnixTimeMilliseconds()
    $process = Start-CodexAttempt $assignedPort
    $sessionFile = ''
    $successful = $false

    while ($true) {
        Start-Sleep -Milliseconds 1000
        if (-not $sessionFile) {
            $sessionFile = Find-SessionFile $Workspace $startedMs
        }
        if ($sessionFile) {
            $status = Get-SessionStatus $sessionFile
            if ($status -eq 'failed') {
                Write-Host "[supervisor] interrupted/error detected; stopping this Codex attempt."
                Stop-ProcessTree ([int]$process.Id)
                break
            }
            if ($status -eq 'success') {
                $successful = $true
                Write-Host "[supervisor] task_complete detected; closing the finished Codex attempt."
                Stop-ProcessTree ([int]$process.Id)
                break
            }
        }

        $process.Refresh()
        if ($process.HasExited) {
            if ($sessionFile) {
                $status = Get-SessionStatus $sessionFile
                if ($status -eq 'success') { $successful = $true }
            }
            if (-not $successful) {
                Write-Host "[supervisor] Codex exited before task_complete; restarting from the first prompt."
            }
            break
        }
    }

    if ($successful) {
        Start-Sleep -Milliseconds 500
        & $Uv run python $TaskScript finalize-run $TaskId --side $Side --workspace $Workspace --started-ms $startedMs --port $assignedPort
        if ($LASTEXITCODE -eq 0) {
            Write-Host "[supervisor] complete trajectory archived; product branch pushed."
            exit 0
        }
        Write-Host '[supervisor] project completed but finalization/push failed; do not rerun the project.'
        exit 1
    }

    Write-Host '[supervisor] restarting the project from the first prompt in 2 seconds ...'
    Start-Sleep -Seconds 2
}
} finally {
    Release-PortLease $portLease
    Write-Host "[supervisor] released development port $assignedPort."
}
