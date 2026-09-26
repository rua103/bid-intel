$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$processFile = Join-Path $repoRoot 'backend/.data/demo-processes.json'
if (-not (Test-Path -LiteralPath $processFile)) {
    Write-Host '没有找到本脚本启动的演示服务记录。'
    exit 0
}

$records = Get-Content -LiteralPath $processFile -Raw | ConvertFrom-Json
foreach ($record in $records) {
    $process = Get-Process -Id $record.id -ErrorAction SilentlyContinue
    if (-not $process) { continue }
    $recordedStart = [datetime]::Parse($record.started_utc).ToLocalTime()
    if ([math]::Abs(($process.StartTime - $recordedStart).TotalSeconds) -gt 2) {
        Write-Warning "PID $($record.id) 已被其他进程复用，跳过。"
        continue
    }
    Stop-Process -Id $process.Id -Force
}
Remove-Item -LiteralPath $processFile -Force
Write-Host '演示服务已停止。日志保留在 backend/.data/demo-logs。'
