#Requires -Version 7.0

<#
.SYNOPSIS
核对本机环境，启动 API、Worker 和桌面，退出时清理本轮后台服务。
.DESCRIPTION
首次运行前请按根 README 准备数据库迁移及固定执行镜像。
日志保存在 AGENTGIT_DATA_ROOT 下的 logs 目录，每轮使用独立子目录。
.PARAMETER NoDesktop
只启动后端，跳过 Node/npm 检查及桌面安装，按回车停止。
.PARAMETER Repair
同时启动 Repair Lab 的三个编排器。
.PARAMETER Port
API 的回环端口，默认为 8000。
.PARAMETER SkipCompose
使用已准备的数据库，跳过 Docker Desktop 和 Compose 启动。
.EXAMPLE
pwsh -NoProfile -File scripts/start-local.ps1 -Repair
#>
[CmdletBinding()]
param(
  [switch]$NoDesktop,
  [switch]$Repair,
  [ValidateRange(1, 65535)]
  [int]$Port = 8000,
  [switch]$SkipCompose
)

$ErrorActionPreference = 'Stop'
# 原生命令统一显式检查退出码，兼容调用者启用了原生命令错误提升的环境。
$PSNativeCommandUseErrorActionPreference = $false
$root = Split-Path -Parent $PSScriptRoot
$desktopDir = Join-Path $root 'desktop'
$script:services = @()
$savedEnvironment = @{}
foreach ($name in @('AGENTGIT_API_TOKEN', 'AGENTGIT_API_URL', 'PYTHONUNBUFFERED')) {
  $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$dataRoot = if ($env:AGENTGIT_DATA_ROOT) { $env:AGENTGIT_DATA_ROOT } else { '.agentgit/dev' }
$dataRoot = [IO.Path]::GetFullPath($dataRoot, $root)
$sessionId = "$(Get-Date -Format 'yyyyMMdd-HHmmss')-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
$logDir = Join-Path $dataRoot "logs/$sessionId"
$apiUrl = "http://127.0.0.1:$Port"

function Invoke-CheckedCommand {
  param([string]$FilePath, [string[]]$Arguments, [string]$Description)
  & $FilePath @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "$Description 失败（退出码 $LASTEXITCODE），请检查上方输出。"
  }
}

function Assert-ServicesRunning {
  foreach ($service in $script:services) {
    if ($service.Process.HasExited) {
      throw "$($service.Name) 提前退出（退出码 $($service.Process.ExitCode)），请查看 $($service.ErrorLog) 和 $($service.OutputLog)。"
    }
  }
}

function Start-AgentGitService {
  param([string]$Name, [string[]]$ArgumentList)
  New-Item -ItemType Directory -Force $logDir | Out-Null
  $out = Join-Path $logDir "$Name.out.log"
  $err = Join-Path $logDir "$Name.err.log"
  $proc = Start-Process -FilePath $uvPath -ArgumentList $ArgumentList -WorkingDirectory $root `
    -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
  $script:services += [pscustomobject]@{
    Name = $Name
    Process = $proc
    OutputLog = $out
    ErrorLog = $err
  }
  Write-Host "  已启动 $Name（PID $($proc.Id)），日志 $out"
}

function Stop-Services {
  # 先停止编排器和 Worker，最后停止 API，仅清理本轮登记的进程树。
  for ($index = $script:services.Count - 1; $index -ge 0; $index--) {
    $service = $script:services[$index]
    if (-not $service.Process.HasExited) {
      & taskkill.exe /T /F /PID $service.Process.Id *> $null
      if ($LASTEXITCODE -ne 0 -and -not $service.Process.HasExited) {
        Write-Warning "未能停止 $($service.Name)（PID $($service.Process.Id)），请核对该进程。"
      }
    }
  }
}

Push-Location $root
try {
  Write-Host '== 1/5 检查工具与依赖 =='
  if (-not $IsWindows) { throw '本脚本需要 Windows 与 PowerShell 7，请按 README 手动启动后端。' }
  $uvPath = (Get-Command uv -CommandType Application -ErrorAction Stop).Source
  Get-Command taskkill.exe -CommandType Application -ErrorAction Stop | Out-Null
  if (-not $SkipCompose) { Get-Command docker -CommandType Application -ErrorAction Stop | Out-Null }
  if (-not $NoDesktop) {
    Get-Command node -CommandType Application -ErrorAction Stop | Out-Null
    Get-Command npm.cmd -CommandType Application -ErrorAction Stop | Out-Null
    $engines = (Get-Content (Join-Path $desktopDir 'package.json') -Raw | ConvertFrom-Json).engines
    $nodeVersion = (Invoke-CheckedCommand 'node' @('--version') 'Node 版本查询' | Out-String).Trim()
    $npmVersion = (Invoke-CheckedCommand 'npm.cmd' @('--version') 'npm 版本查询' | Out-String).Trim()
    $nodePattern = 'v' + ($engines.node -replace 'x', '*')
    if ($nodeVersion -notlike $nodePattern -or $npmVersion -ne $engines.npm) {
      throw "桌面需要 Node $($engines.node) / npm $($engines.npm)，当前为 $nodeVersion / $npmVersion。"
    }
  }
  $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
  try { $listener.Start() } catch { throw "回环端口 $Port 无法绑定，请检查占用情况或使用 -Port 指定其他端口。" } finally { $listener.Stop() }
  Invoke-CheckedCommand 'uv' @('sync', '--locked') 'Python 依赖同步'
  if (-not $NoDesktop) {
    Push-Location $desktopDir
    try {
      if (-not (Test-Path 'node_modules/.package-lock.json')) {
        Invoke-CheckedCommand 'npm.cmd' @('ci') '桌面依赖安装'
      }
      if (-not (Test-Path 'node_modules/electron/dist/electron.exe')) {
        Invoke-CheckedCommand 'npm.cmd' @('run', 'setup') 'Electron 运行时准备'
      }
    } finally { Pop-Location }
  }

  Write-Host '== 2/5 检查数据库设施 =='
  if (-not $SkipCompose) {
    & docker info *> $null
    if ($LASTEXITCODE -ne 0) {
      $dockerExe = @(
        "$env:ProgramFiles/Docker/Docker/Docker Desktop.exe",
        "$env:LOCALAPPDATA/Docker/Docker Desktop.exe"
      ) | Where-Object { Test-Path $_ } | Select-Object -First 1
      if (-not $dockerExe) { throw 'Docker Desktop 未运行且未找到安装位置，请先安装并启动。' }
      Write-Host '  正在启动 Docker Desktop（最长等待 3 分钟）...'
      Start-Process -FilePath $dockerExe -WindowStyle Hidden
      $deadline = (Get-Date).AddMinutes(3)
      do {
        Start-Sleep -Seconds 2
        & docker info *> $null
        if ($LASTEXITCODE -eq 0) { break }
      } while ((Get-Date) -lt $deadline)
      if ($LASTEXITCODE -ne 0) { throw 'Docker 引擎 3 分钟内未就绪，请手动启动后重试。' }
    }
    $dockerOS = (Invoke-CheckedCommand 'docker' @('info', '--format', '{{.OSType}}') 'Docker 模式查询' | Out-String).Trim()
    if ($dockerOS -ne 'linux') { throw '请将 Docker Desktop 切换到 Linux 容器模式后重试。' }
    Invoke-CheckedCommand 'docker' @('compose', 'up', '-d', '--wait', '--wait-timeout', '90') 'PostgreSQL 启动'
  } else {
    Write-Host '  使用当前环境配置的数据库，已跳过 Compose。'
  }

  Write-Host '== 3/5 核对数据库迁移 =='
  $current = @(Invoke-CheckedCommand 'uv' @('run', '--locked', '--no-sync', 'alembic', 'current') '数据库迁移查询')
  $heads = @(Invoke-CheckedCommand 'uv' @('run', '--locked', '--no-sync', 'alembic', 'heads') '代码迁移查询')
  $currentIds = @($current | Where-Object { $_ -match '^\S+\s+\(head\)$' } | ForEach-Object { ($_ -split '\s+')[0] })
  $headIds = @($heads | Where-Object { $_ -match '^\S+\s+\(head\)$' } | ForEach-Object { ($_ -split '\s+')[0] })
  if ($currentIds.Count -ne 1 -or $headIds.Count -ne 1 -or $currentIds[0] -ne $headIds[0]) {
    throw '数据库尚未迁移至当前唯一 head。新环境请先运行 uv run --locked alembic upgrade head；已有数据请停止写者并完整备份数据库和数据根后再升级。'
  }
  Write-Host "  schema 已是 head（$($headIds[0])）。"

  Write-Host '== 4/5 启动 API 与 Worker =='
  $token = (Invoke-CheckedCommand 'uv' @('run', '--locked', '--no-sync', 'python', '-c', 'import secrets; print(secrets.token_urlsafe(32))') '令牌生成' | Out-String).Trim()
  if ($token.Length -lt 32 -or $token -cmatch '[^\x21-\x7e]') { throw '令牌生成结果无效。' }
  $env:AGENTGIT_API_TOKEN = $token
  $env:AGENTGIT_API_URL = $apiUrl
  $env:PYTHONUNBUFFERED = '1'
  Write-Host '  本轮令牌（已自动传给桌面，如需另开终端可复制，请勿保存到日志或 Git）'
  Write-Host "  $token" -ForegroundColor Cyan
  Start-AgentGitService 'api' @('run', '--locked', '--no-sync', 'agentgit', 'serve', '--port', "$Port")
  $ready = $false
  $deadline = (Get-Date).AddSeconds(90)
  while ((Get-Date) -lt $deadline) {
    Assert-ServicesRunning
    try {
      $probe = Invoke-RestMethod "$apiUrl/ready" -TimeoutSec 3 -NoProxy
      if ($probe.status -eq 'ready') { $ready = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
  }
  if (-not $ready) { throw "API 与数据库 90 秒内未就绪，请查看 $logDir 下的 api 日志。" }
  Start-AgentGitService 'worker' @('run', '--locked', '--no-sync', 'agentgit', 'worker')
  if ($Repair) {
    foreach ($name in @('repair-worker', 'repair-study-worker', 'repair-confirm-worker')) {
      Start-AgentGitService $name @('run', '--locked', '--no-sync', 'agentgit', $name)
    }
  }
  Start-Sleep -Seconds 1
  Assert-ServicesRunning

  Write-Host '== 5/5 启动完成 =='
  Write-Host "  API $apiUrl，日志目录 $logDir"
  Write-Host '  退出会停止本轮 API/Worker 进程树，活动任务请先处理至安全状态。'
  if ($NoDesktop) {
    Read-Host '  按回车停止本轮后台服务' | Out-Null
    Assert-ServicesRunning
  } else {
    Push-Location $desktopDir
    try { Invoke-CheckedCommand 'npm.cmd' @('start') '桌面启动' } finally { Pop-Location }
    Assert-ServicesRunning
  }
} finally {
  try {
    Stop-Services
  } finally {
    foreach ($name in $savedEnvironment.Keys) {
      if ($null -eq $savedEnvironment[$name]) {
        Remove-Item "Env:\$name" -ErrorAction SilentlyContinue
      } else {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
      }
    }
    Pop-Location
  }
}
