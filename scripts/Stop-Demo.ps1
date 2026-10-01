$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$processFile = Join-Path $repoRoot 'backend/.data/demo-processes.json'

if (-not (Test-Path -LiteralPath $processFile)) {
    Write-Host '没有找到本脚本启动的演示服务记录。'
    exit 0
}

function Get-ProcessTree([object[]]$Processes, [int]$RootId) {
    $childrenByParent = @{}
    foreach ($item in $Processes) {
        $parentId = [int]$item.ParentProcessId
        if (-not $childrenByParent.ContainsKey($parentId)) {
            $childrenByParent[$parentId] = [System.Collections.ArrayList]::new()
        }
        [void]$childrenByParent[$parentId].Add($item)
    }

    $queue = [System.Collections.Queue]::new()
    $queue.Enqueue([pscustomobject]@{ id = $RootId; depth = 0 })
    $seen = @{}
    $descendants = [System.Collections.ArrayList]::new()
    while ($queue.Count -gt 0) {
        $current = $queue.Dequeue()
        if (-not $childrenByParent.ContainsKey([int]$current.id)) { continue }
        foreach ($child in @($childrenByParent[[int]$current.id])) {
            $childId = [int]$child.ProcessId
            if ($seen.ContainsKey($childId)) { continue }
            $seen[$childId] = $true
            [void]$descendants.Add([pscustomobject]@{
                process = $child
                depth = [int]$current.depth + 1
            })
            $queue.Enqueue([pscustomobject]@{ id = $childId; depth = [int]$current.depth + 1 })
        }
    }
    return @($descendants)
}

function ConvertTo-UtcDateTime([object]$Value) {
    if ($Value -is [datetime]) { return $Value.ToUniversalTime() }
    if ($Value -is [datetimeoffset]) { return $Value.UtcDateTime }
    return [datetimeoffset]::Parse([string]$Value).UtcDateTime
}

function Test-RootIdentity([object]$Record, [object]$Metadata) {
    if (-not $Metadata) { return $false }
    $process = Get-Process -Id ([int]$Record.id) -ErrorAction SilentlyContinue
    if (-not $process) { return $false }
    try {
        $recordedStart = ConvertTo-UtcDateTime $Record.started_utc
        $actualStart = $process.StartTime.ToUniversalTime()
        if ([math]::Abs(($actualStart - $recordedStart).TotalSeconds) -gt 2) { return $false }
    } catch { return $false }
    if ($Record.image_path -and $Metadata.ExecutablePath -and
        ([string]$Record.image_path -ne [string]$Metadata.ExecutablePath)) { return $false }
    if ($Record.command_line -and $Metadata.CommandLine -and
        ([string]$Record.command_line -ne [string]$Metadata.CommandLine)) { return $false }
    return $true
}

function Test-ScopedDescendant([object]$Record, [object]$Metadata) {
    $scope = [string]$Record.working_directory
    if (-not $scope) { return $false }
    $scope = $scope.TrimEnd('\')
    $commandLine = [string]$Metadata.CommandLine
    $imagePath = [string]$Metadata.ExecutablePath
    return $commandLine.IndexOf($scope, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -or
        $imagePath.StartsWith($scope, [System.StringComparison]::OrdinalIgnoreCase)
}

try {
    try {
        $records = @(Get-Content -LiteralPath $processFile -Raw | ConvertFrom-Json)
    } catch {
        Write-Warning '演示服务记录已损坏，无法安全判断其进程归属；未终止任何进程。'
        return
    }

    $allProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    foreach ($record in $records) {
        if (-not $record.id -or -not $record.started_utc) {
            Write-Warning '跳过缺少 PID 或启动时间的演示服务记录。'
            continue
        }
        $rootId = [int]$record.id
        $rootMetadata = $allProcesses | Where-Object { [int]$_.ProcessId -eq $rootId } | Select-Object -First 1
        $rootIdentityValid = Test-RootIdentity $record $rootMetadata
        $tree = @(Get-ProcessTree $allProcesses $rootId)

        if ($rootMetadata -and -not $rootIdentityValid) {
            Write-Warning "PID $rootId 已被其他进程复用，跳过该记录及其子进程。"
            continue
        }

        try { $recordedStart = ConvertTo-UtcDateTime $record.started_utc }
        catch { $recordedStart = [datetime]::MinValue.ToUniversalTime() }
        $childrenToStop = @($tree | Sort-Object depth -Descending | Where-Object {
            $metadata = $_.process
            $created = $metadata.CreationDate.ToUniversalTime()
            $created -ge $recordedStart.AddSeconds(-2) -and
                ($rootIdentityValid -or (Test-ScopedDescendant $record $metadata))
        })
        foreach ($child in $childrenToStop) {
            Stop-Process -Id ([int]$child.process.ProcessId) -Force -ErrorAction SilentlyContinue
        }
        if ($rootIdentityValid) {
            Stop-Process -Id $rootId -Force -ErrorAction SilentlyContinue
        }
    }
} finally {
    Remove-Item -LiteralPath $processFile -Force -ErrorAction SilentlyContinue
}
Write-Host '演示服务已停止。日志保留在 backend/.data/demo-logs。'
