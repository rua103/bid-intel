param(
    [switch]$Offline,
    [switch]$InstallDependencies,
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 5173
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$backendRoot = Join-Path $repoRoot 'backend'
$frontendRoot = Join-Path $repoRoot 'frontend'
$dataRoot = Join-Path $backendRoot '.data'
$logRoot = Join-Path $dataRoot 'demo-logs'
$processFile = Join-Path $dataRoot 'demo-processes.json'
$backendEnv = Join-Path $backendRoot '.env'
$script:createdProcesses = @()

function Test-PortFree([int]$Port) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if ($listener) { throw "端口 $Port 已被占用；为避免关闭其他服务，请先确认占用程序。" }
}

function Read-EnvValue([string]$Name) {
    $row = Get-Content -LiteralPath $backendEnv | Where-Object { $_ -match "^\s*$([regex]::Escape($Name))\s*=" } | Select-Object -Last 1
    if (-not $row) { return '' }
    $value = ($row -split '=', 2)[1].Trim()
    return $value.Trim('"').Trim("'")
}

function Start-HiddenProcess([string]$FilePath, [string[]]$ArgumentList, [string]$WorkingDirectory, [string]$Name) {
    $safeName = $Name.ToLowerInvariant()
    $outPath = Join-Path $logRoot "$safeName.out.log"
    $errPath = Join-Path $logRoot "$safeName.err.log"
    $proc = Start-Process -FilePath $FilePath -ArgumentList $ArgumentList -WorkingDirectory $WorkingDirectory `
        -PassThru -WindowStyle Hidden -RedirectStandardOutput $outPath -RedirectStandardError $errPath
    $script:createdProcesses += [pscustomobject]@{
        name = $Name
        id = $proc.Id
        started_utc = $proc.StartTime.ToUniversalTime().ToString('o')
    }
    return $proc
}

if (Test-Path -LiteralPath $processFile) {
    $oldState = Get-Content -LiteralPath $processFile -Raw | ConvertFrom-Json
    $live = @($oldState | Where-Object { Get-Process -Id $_.id -ErrorAction SilentlyContinue })
    if ($live.Count -gt 0) { throw '演示服务似乎已在运行；请先执行 scripts/Stop-Demo.ps1。' }
    Remove-Item -LiteralPath $processFile -Force
}

Test-PortFree $BackendPort
Test-PortFree $FrontendPort
New-Item -ItemType Directory -Force -Path $dataRoot, $logRoot | Out-Null
if (-not (Test-Path -LiteralPath $backendEnv)) {
    Copy-Item -LiteralPath (Join-Path $backendRoot '.env.example') -Destination $backendEnv
    Write-Host '已从 .env.example 创建 backend/.env；请配置模型或评审账号后再进行正式演示。'
}

$pythonExe = Join-Path $backendRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        & $pyLauncher.Source -3.13 -m venv (Join-Path $backendRoot '.venv')
    } else {
        $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pythonCmd) { throw '找不到 Python 3.11–3.13；请先安装 Python。' }
        & $pythonCmd.Source -m venv (Join-Path $backendRoot '.venv')
    }
    if ($LASTEXITCODE -ne 0) { throw '创建后端虚拟环境失败。' }
}

$npmCmd = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $npmCmd) { $npmCmd = Get-Command npm -ErrorAction SilentlyContinue }
if (-not $npmCmd) { throw '找不到 Node.js/npm；请先安装 Node.js。' }

$hasBackendDeps = & $pythonExe -c "import fastapi, uvicorn" 2>$null
if ($LASTEXITCODE -ne 0 -or $InstallDependencies) {
    if ($Offline -and $LASTEXITCODE -ne 0) { throw '离线演示缺少后端依赖；请在联网准备阶段先运行本脚本并安装依赖。' }
    & $pythonExe -m pip install -e '.[dev,ocr]' --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { throw '安装后端依赖失败。' }
}

if (-not (Test-Path -LiteralPath (Join-Path $frontendRoot 'node_modules'))) {
    if ($Offline) { throw '离线演示缺少前端 node_modules；请在联网准备阶段先执行 npm ci。' }
    Push-Location $frontendRoot
    try { & $npmCmd.Source ci; if ($LASTEXITCODE -ne 0) { throw '安装前端依赖失败。' } }
    finally { Pop-Location }
}

$authEnabled = (Read-EnvValue 'AUTH_ENABLED') -match '^(?i:true|1|yes)$'
if ($authEnabled) {
    foreach ($keyName in @('AUTH_USERNAME', 'AUTH_PASSWORD', 'AUTH_SECRET_KEY')) {
        if (-not (Read-EnvValue $keyName)) { throw "AUTH_ENABLED=true，但 $keyName 未配置；演示账号不会被打印到终端。" }
    }
}

if ($Offline) {
    Push-Location $backendRoot
    try {
        & $pythonExe -m app.demo_seed
        if ($LASTEXITCODE -ne 0) { throw '生成离线演示数据失败。' }
    } finally { Pop-Location }
}

try {
    $previousExtractionMode = $env:EXTRACTION_MODE
    if ($Offline) { $env:EXTRACTION_MODE = 'rules' }
    $backendProcess = Start-HiddenProcess $pythonExe @('-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', [string]$BackendPort) $backendRoot 'backend'
    if ($null -eq $previousExtractionMode) { Remove-Item Env:EXTRACTION_MODE -ErrorAction SilentlyContinue }
    else { $env:EXTRACTION_MODE = $previousExtractionMode }

    $deadline = (Get-Date).AddSeconds(45)
    $healthy = $false
    while ((Get-Date) -lt $deadline) {
        try {
            $null = Invoke-RestMethod -Uri "http://127.0.0.1:$BackendPort/api/v1/health" -TimeoutSec 2
            $healthy = $true
            break
        } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $healthy) { throw '后端 45 秒内未就绪；查看 backend/.data/demo-logs/backend.err.log。' }

    $previousApiBase = $env:VITE_API_BASE
    $previousApiPort = $env:VITE_API_PORT
    Remove-Item Env:VITE_API_BASE -ErrorAction SilentlyContinue
    $env:VITE_API_PORT = [string]$BackendPort
    $frontendProcess = Start-HiddenProcess $npmCmd.Source @('run', 'dev', '--', '--host', '0.0.0.0', '--port', [string]$FrontendPort) $frontendRoot 'frontend'
    if ($null -eq $previousApiBase) { Remove-Item Env:VITE_API_BASE -ErrorAction SilentlyContinue } else { $env:VITE_API_BASE = $previousApiBase }
    if ($null -eq $previousApiPort) { Remove-Item Env:VITE_API_PORT -ErrorAction SilentlyContinue } else { $env:VITE_API_PORT = $previousApiPort }
    $script:createdProcesses | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $processFile -Encoding utf8
    Write-Host '演示服务已启动：'
    Write-Host "  分析台：http://localhost:$FrontendPort"
    Write-Host "  标注页：http://localhost:$FrontendPort/annotation.html"
    if ($Offline) { Write-Host '  离线快照：数据集“离线演示样例（纯虚构）”，完全不调用模型。' }
    Write-Host "停止服务：powershell -ExecutionPolicy Bypass -File `"$(Join-Path $PSScriptRoot 'Stop-Demo.ps1')`""
} catch {
    foreach ($procRecord in $script:createdProcesses) {
        Stop-Process -Id $procRecord.id -Force -ErrorAction SilentlyContinue
    }
    throw
}
