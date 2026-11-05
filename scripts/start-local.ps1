# AgentGit 一键本地启动：自动准备数据库/迁移，后台启动 API 与 Worker，前台启动桌面。
# 用法（仓库根目录）：
#   pwsh -ExecutionPolicy Bypass -File scripts\start-local.ps1
# 可选开关：
#   -NoDesktop  只启动后端（API + Worker + Repair 编排器），按回车停止
#   -Repair     同时启动 Repair Lab 三个编排器（仅 Repair Lab 需要）
# 退出（含 Ctrl+C）时自动停止本脚本启动的全部后台服务。
[CmdletBinding()]
param(
  [switch]$NoDesktop,
  [switch]$Repair
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$script:services = @()
$logDir = Join-Path $root '.agentgit\dev\logs'

function Start-AgentGitService {
  param([string]$Name, [string[]]$ArgumentList)
  New-Item -ItemType Directory -Force $logDir | Out-Null
  $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
  $uv = (Get-Command uv -ErrorAction Stop).Source
  $out = Join-Path $logDir "$Name-$stamp.out.log"
  $err = Join-Path $logDir "$Name-$stamp.err.log"
  $proc = Start-Process -FilePath $uv -ArgumentList $ArgumentList -WindowStyle Hidden `
    -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
  $script:services += $proc
  Write-Host "  已启动 $Name（PID $($proc.Id)），日志 $out"
}

function Stop-Services {
  foreach ($proc in $script:services) {
    if ($proc -and -not $proc.HasExited) {
      & taskkill /T /F /PID $proc.Id | Out-Null
    }
  }
  Remove-Item Env:\AGENTGIT_API_TOKEN -ErrorAction SilentlyContinue
  Remove-Item Env:\AGENTGIT_API_URL -ErrorAction SilentlyContinue
  Remove-Item Env:\PYTHONUNBUFFERED -ErrorAction SilentlyContinue
}

try {
  # ---- 1. Docker 引擎与数据库 ----
  Write-Host '== 1/5 检查 Docker 与数据库 =='
  docker info *> $null
  if ($LASTEXITCODE -ne 0) {
    $dockerExe = @(
      "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe",
      "$env:LOCALAPPDATA\Docker\Docker Desktop.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $dockerExe) { throw 'Docker Desktop 未运行且未找到安装位置，请先安装并启动。' }
    Write-Host '  正在启动 Docker Desktop（最长等待 3 分钟）...'
    Start-Process $dockerExe
    $deadline = (Get-Date).AddMinutes(3)
    do {
      docker info *> $null
      if ($LASTEXITCODE -eq 0) { break }
      Start-Sleep -Seconds 10
    } while ((Get-Date) -lt $deadline)
    if ($LASTEXITCODE -ne 0) { throw 'Docker 引擎 3 分钟内未就绪，请手动启动后重试。' }
  }
  docker compose up -d --wait
  if ($LASTEXITCODE -ne 0) { throw 'docker compose up 失败，请检查 compose.yaml 与 Docker 状态。' }
  Write-Host '  PostgreSQL 已就绪。'

  # ---- 2. 数据库迁移核对 ----
  Write-Host '== 2/5 核对数据库迁移 =='
  $currentId = (uv run --locked alembic current 2>$null |
    ForEach-Object { ($_ -split '\s+')[0] } | Where-Object { $_ } | Select-Object -Last 1)
  $headId = (uv run --locked alembic heads 2>$null |
    ForEach-Object { ($_ -split '\s+')[0] } | Where-Object { $_ } | Select-Object -Last 1)
  if ($currentId -eq $headId -and $currentId) {
    Write-Host "  schema 已是 head（$headId）。"
  } else {
    $currentDisplay = if ($currentId) { $currentId } else { '空库' }
    Write-Host "  数据库 schema 停留在 '$currentDisplay'，代码要求 '$headId'。"
    if ($currentId) {
      # 已有业务数据：按项目规范先备份再升级
      $answer = Read-Host '  先备份到 .agentgit\backups 再升级，输入 y 继续，回车取消'
      if ($answer -ne 'y') { throw '已取消。请自行备份后执行 alembic upgrade head。' }
      New-Item -ItemType Directory -Force (Join-Path $root '.agentgit\backups') | Out-Null
      $dumpName = "agentgit_dev_$(Get-Date -Format 'yyyyMMdd-HHmmss').dump"
      docker compose exec postgres sh -c 'pg_dump -U agentgit -d agentgit_dev -Fc -f /tmp/pre-upgrade.dump'
      docker compose cp postgres:/tmp/pre-upgrade.dump ".agentgit\backups\$dumpName"
      docker compose exec postgres rm /tmp/pre-upgrade.dump
      Write-Host "  已备份到 .agentgit\backups\$dumpName"
    }
    uv run --locked alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw '迁移升级失败，请检查上方输出。' }
    Write-Host "  已升级到 $headId。"
  }

  # ---- 3. 生成令牌并启动后端服务 ----
  Write-Host '== 3/5 生成令牌并启动 API 与 Worker =='
  if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) {
    throw '端口 8000 已被占用（可能已有 API 在运行），请先停止旧进程。'
  }
  $token = (uv run --locked python -c "import secrets; print(secrets.token_urlsafe(32))").Trim()
  $env:AGENTGIT_API_TOKEN = $token
  $env:PYTHONUNBUFFERED = '1'  # 让后台服务的 Python 输出即时写入日志
  Write-Host '  本轮令牌（已通过环境变量传给各服务；如需另开终端可复制）：'
  Write-Host "  $token" -ForegroundColor Cyan

  Start-AgentGitService -Name 'api' -ArgumentList @('run', '--locked', 'agentgit', 'serve', '--port', '8000')
  Write-Host '  等待 API 就绪...'
  $ready = $false
  $deadline = (Get-Date).AddSeconds(90)
  while ((Get-Date) -lt $deadline) {
    try {
      Invoke-RestMethod http://127.0.0.1:8000/health -TimeoutSec 3 | Out-Null
      $ready = $true
      break
    } catch { Start-Sleep -Seconds 2 }
  }
  if (-not $ready) { throw 'API 90 秒内未就绪，请查看 .agentgit\dev\logs 下最新 api-*.log。' }
  Write-Host '  API 就绪（http://127.0.0.1:8000）。'

  Start-AgentGitService -Name 'worker' -ArgumentList @('run', '--locked', 'agentgit', 'worker')
  if ($Repair) {
    Start-AgentGitService -Name 'repair-worker' -ArgumentList @('run', '--locked', 'agentgit', 'repair-worker')
    Start-AgentGitService -Name 'repair-study-worker' -ArgumentList @('run', '--locked', 'agentgit', 'repair-study-worker')
    Start-AgentGitService -Name 'repair-confirm-worker' -ArgumentList @('run', '--locked', 'agentgit', 'repair-confirm-worker')
  }

  # ---- 4. 桌面构建环境 ----
  Write-Host '== 4/5 检查桌面构建环境（Node 26.x / npm 12.1.0）=='
  $desktopDir = Join-Path $root 'desktop'
  if (-not (Test-Path (Join-Path $desktopDir 'node_modules'))) {
    Push-Location $desktopDir
    try {
      Write-Host '  首次构建：npm ci + npm run setup（需几分钟）...'
      npm ci
      if ($LASTEXITCODE -ne 0) { throw 'npm ci 失败。' }
      npm run setup
      if ($LASTEXITCODE -ne 0) { throw 'npm run setup 失败。' }
    } finally { Pop-Location }
    Write-Host '  桌面依赖安装完成。'
  } elseif (-not (Test-Path (Join-Path $desktopDir 'node_modules\electron\dist\electron.exe'))) {
    Push-Location $desktopDir
    try {
      Write-Host '  补装 Electron 运行时（npm run setup）...'
      npm run setup
      if ($LASTEXITCODE -ne 0) { throw 'npm run setup 失败。' }
    } finally { Pop-Location }
  } else {
    Write-Host '  桌面依赖已就绪。'
  }

  # ---- 5. 启动 / 汇总 ----
  Write-Host '== 5/5 启动 =='
  if ($NoDesktop) {
    Write-Host '  后端已全部就绪（API + Worker）。在此窗口按回车停止全部服务...'
    Read-Host | Out-Null
  } else {
    $env:AGENTGIT_API_URL = 'http://127.0.0.1:8000'
    Push-Location $desktopDir
    try {
      npm start
    } finally { Pop-Location }
  }
} finally {
  Write-Host ''
  Write-Host '正在停止本脚本启动的后台服务...'
  Stop-Services
  Write-Host '已全部停止。'
}
