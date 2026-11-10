#Requires -Version 7.0
# 用受控命令替身验证启动编排，不访问真实数据库、Docker 或桌面。
$ErrorActionPreference = 'Stop'
$launcher = Join-Path $PSScriptRoot '../../scripts/start-local.ps1'
$originalEnvironment = @{}
foreach ($name in @('AGENTGIT_API_TOKEN', 'AGENTGIT_API_URL', 'PYTHONUNBUFFERED', 'AGENTGIT_DATA_ROOT')) {
  $originalEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$originalExitCode = $global:LASTEXITCODE
$testRoot = Join-Path ([IO.Path]::GetTempPath()) "agentgit-launcher-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory $testRoot | Out-Null

function Assert-True($Condition, [string]$Message) {
  if (-not $Condition) { throw $Message }
}

function Get-Command {
  param($Name, $CommandType, $ErrorAction)
  $global:AgentGitLauncherTestState.Calls.Add("command $Name")
  if ($Name -eq 'uv' -and $global:AgentGitLauncherTestState.Case -eq 'missing-tool') { throw '缺少 uv' }
  [pscustomobject]@{ Source = $Name }
}

function uv {
  $call = $args -join ' '
  $global:AgentGitLauncherTestState.Calls.Add("uv $call")
  $global:LASTEXITCODE = 0
  if ($call -eq 'sync --locked' -and $global:AgentGitLauncherTestState.Case -eq 'sync-failure') {
    $global:LASTEXITCODE = 8
  } elseif ($call -match 'alembic current$') {
    switch ($global:AgentGitLauncherTestState.Case) {
      'migration-failure' { $global:LASTEXITCODE = 9 }
      'empty-database' { }
      'old-migration' { '0032_a2a_bindings' }
      default { '0033_a2a_runs (head)' }
    }
  } elseif ($call -match 'alembic heads$') {
    '0033_a2a_runs (head)'
    if ($global:AgentGitLauncherTestState.Case -eq 'multiple-heads') { 'other_revision (head)' }
  } elseif ($call -match 'token_urlsafe') {
    switch ($global:AgentGitLauncherTestState.Case) {
      'token-failure' { $global:LASTEXITCODE = 10 }
      'invalid-token' { 'short' }
      default { 'a' * 43 }
    }
  }
}

function node {
  $global:AgentGitLauncherTestState.Calls.Add('node')
  $global:LASTEXITCODE = 0
  if ($global:AgentGitLauncherTestState.Case -eq 'wrong-node') { 'v24.19.0' } else { 'v26.13.0' }
}

function npm.cmd {
  $call = $args -join ' '
  $global:AgentGitLauncherTestState.Calls.Add("npm $call")
  $global:LASTEXITCODE = 0
  if ($call -eq '--version') {
    if ($global:AgentGitLauncherTestState.Case -eq 'wrong-npm') { '11.0.0' } else { '12.1.0' }
  }
  if ($call -eq 'start' -and $global:AgentGitLauncherTestState.Case -eq 'desktop-failure') { $global:LASTEXITCODE = 11 }
  if ($call -eq 'ci' -and $global:AgentGitLauncherTestState.Case -eq 'install-failure') { $global:LASTEXITCODE = 12 }
  if ($call -eq 'run setup' -and $global:AgentGitLauncherTestState.Case -eq 'setup-failure') { $global:LASTEXITCODE = 13 }
}

function docker {
  $call = $args -join ' '
  $global:AgentGitLauncherTestState.Calls.Add("docker $call")
  $global:LASTEXITCODE = 0
  if ($call -match '--format') {
    if ($global:AgentGitLauncherTestState.Case -eq 'windows-containers') { 'windows' } else { 'linux' }
  }
  if ($call -match '^compose' -and $global:AgentGitLauncherTestState.Case -eq 'compose-failure') { $global:LASTEXITCODE = 14 }
}

function Test-Path {
  param($Path)
  if ($Path -in @('node_modules/.package-lock.json', 'node_modules/electron/dist/electron.exe')) { return $false }
  Microsoft.PowerShell.Management\Test-Path $Path
}

function Start-Process {
  param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle, $RedirectStandardOutput, $RedirectStandardError, [switch]$PassThru)
  Assert-True ($WindowStyle -eq 'Hidden') '后台服务必须隐藏窗口'
  Assert-True ($RedirectStandardOutput.StartsWith($env:AGENTGIT_DATA_ROOT)) '日志须使用配置的数据根'
  $name = [IO.Path]::GetFileNameWithoutExtension($RedirectStandardOutput) -replace '\.out$', ''
  $global:AgentGitLauncherTestState.Calls.Add("start $name $($ArgumentList -join ' ')")
  Assert-True ($env:AGENTGIT_API_TOKEN -eq ('a' * 43)) '服务须获得本轮令牌'
  if ($name -eq 'repair-study-worker' -and $global:AgentGitLauncherTestState.Case -eq 'repair-partial') {
    throw '模拟编排器创建失败'
  }
  $proc = [pscustomobject]@{
    Id = 10000 + $global:AgentGitLauncherTestState.Processes.Count
    Name = $name
    HasExited = ($name -eq 'api' -and $global:AgentGitLauncherTestState.Case -eq 'api-exit')
    ExitCode = 15
  }
  $global:AgentGitLauncherTestState.Processes.Add($proc)
  $proc
}

function taskkill.exe {
  $processId = [int]$args[-1]
  $global:AgentGitLauncherTestState.Killed.Add($processId)
  ($global:AgentGitLauncherTestState.Processes | Where-Object Id -eq $processId).HasExited = $true
  $global:LASTEXITCODE = 0
}

function Invoke-RestMethod {
  param($Uri, $TimeoutSec, [switch]$NoProxy)
  $global:AgentGitLauncherTestState.Calls.Add("probe $Uri")
  Assert-True ($Uri -eq "http://127.0.0.1:$($global:AgentGitLauncherTestState.Port)/ready") '须检查所选端口的数据库就绪接口'
  if ($global:AgentGitLauncherTestState.Case -eq 'ready-failure') {
    ($global:AgentGitLauncherTestState.Processes | Where-Object Name -eq 'api').HasExited = $true
    throw '模拟数据库不可用'
  }
  if ($global:AgentGitLauncherTestState.Case -eq 'ready-timeout') { return @{ status = 'starting' } }
  @{ status = 'ready' }
}

function Get-Date {
  param($Format)
  if ($Format) { return Microsoft.PowerShell.Utility\Get-Date -Format $Format }
  $now = Microsoft.PowerShell.Utility\Get-Date
  if ($global:AgentGitLauncherTestState.Case -eq 'ready-timeout' -and
      @($global:AgentGitLauncherTestState.Calls | Where-Object { $_ -match '^probe ' }).Count -gt 0) {
    return $now.AddMinutes(2)
  }
  $now
}

function Start-Sleep {
  param($Seconds)
  if ($global:AgentGitLauncherTestState.Case -eq 'worker-exit') {
    foreach ($proc in $global:AgentGitLauncherTestState.Processes | Where-Object Name -eq 'worker') { $proc.HasExited = $true }
  }
}

function Read-Host {
  param($Prompt)
  $global:AgentGitLauncherTestState.Calls.Add('wait')
  if ($global:AgentGitLauncherTestState.Case -eq 'interrupted') { throw '模拟交互中断' }
  ''
}

function Write-Host { }

$cases = @(
  @{ Name = 'backend'; NoDesktop = $true; Repair = $false; SkipCompose = $false; Error = ''; Count = 2 },
  @{ Name = 'repair'; NoDesktop = $true; Repair = $true; SkipCompose = $true; Error = ''; Count = 5 },
  @{ Name = 'desktop'; NoDesktop = $false; Error = ''; Count = 2 },
  @{ Name = 'missing-tool'; Error = '缺少 uv'; Count = 0 },
  @{ Name = 'occupied-port'; Error = '无法绑定'; Count = 0 },
  @{ Name = 'sync-failure'; Error = 'Python 依赖同步'; Count = 0 },
  @{ Name = 'migration-failure'; Error = '数据库迁移查询'; Count = 0 },
  @{ Name = 'empty-database'; Error = '尚未迁移'; Count = 0 },
  @{ Name = 'old-migration'; Error = '尚未迁移'; Count = 0 },
  @{ Name = 'multiple-heads'; Error = '唯一 head'; Count = 0 },
  @{ Name = 'token-failure'; Error = '令牌生成'; Count = 0 },
  @{ Name = 'invalid-token'; Error = '令牌生成结果无效'; Count = 0 },
  @{ Name = 'api-exit'; Error = 'api 提前退出'; Count = 1 },
  @{ Name = 'ready-failure'; Error = 'api 提前退出'; Count = 1 },
  @{ Name = 'ready-timeout'; Error = '90 秒内未就绪'; Count = 1 },
  @{ Name = 'worker-exit'; Error = 'worker 提前退出'; Count = 2 },
  @{ Name = 'wrong-node'; NoDesktop = $false; Error = '桌面需要'; Count = 0 },
  @{ Name = 'wrong-npm'; NoDesktop = $false; Error = '桌面需要'; Count = 0 },
  @{ Name = 'repair-partial'; Repair = $true; Error = '模拟编排器创建失败'; Count = 3 },
  @{ Name = 'interrupted'; Error = '模拟交互中断'; Count = 2 },
  @{ Name = 'install-failure'; NoDesktop = $false; Error = '桌面依赖安装'; Count = 0 },
  @{ Name = 'setup-failure'; NoDesktop = $false; Error = 'Electron 运行时准备'; Count = 0 },
  @{ Name = 'desktop-failure'; NoDesktop = $false; Error = '桌面启动'; Count = 2 },
  @{ Name = 'windows-containers'; Error = 'Linux 容器模式'; Count = 0 },
  @{ Name = 'compose-failure'; Error = 'PostgreSQL 启动'; Count = 0 }
)

try {
  foreach ($case in $cases) {
    $global:AgentGitLauncherTestState = @{
      Case = $case.Name
      Calls = [Collections.Generic.List[string]]::new()
      Processes = [Collections.Generic.List[object]]::new()
      Killed = [Collections.Generic.List[int]]::new()
    }
    # 由操作系统分配可用端口，避免依赖开发机的 8000 端口。
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $global:AgentGitLauncherTestState.Port = $listener.LocalEndpoint.Port
    if ($case.Name -ne 'occupied-port') { $listener.Stop() }
    $options = @{ NoDesktop = $true; Port = $global:AgentGitLauncherTestState.Port }
    foreach ($name in @('NoDesktop', 'Repair', 'SkipCompose')) {
      if ($case.ContainsKey($name)) { $options[$name] = $case[$name] }
    }
    $env:AGENTGIT_API_TOKEN = 'original-token'
    $env:AGENTGIT_API_URL = 'http://127.0.0.1:9000'
    Remove-Item Env:\PYTHONUNBUFFERED -ErrorAction SilentlyContinue
    $env:AGENTGIT_DATA_ROOT = Join-Path $testRoot '含 空格的数据'
    $location = (Get-Location).Path
    $failure = ''
    try { & $launcher @options } catch { $failure = $_.Exception.Message } finally { $listener.Stop() }
    Assert-True (($case.Error -eq '' -and $failure -eq '') -or ($case.Error -ne '' -and $failure.Contains($case.Error))) "$($case.Name) 错误不符 $failure"
    Assert-True ($global:AgentGitLauncherTestState.Processes.Count -eq $case.Count) "$($case.Name) 服务数量不符"
    Assert-True (@($global:AgentGitLauncherTestState.Processes | Where-Object { -not $_.HasExited }).Count -eq 0) "$($case.Name) 留下存活进程"
    $expectedKilled = @($global:AgentGitLauncherTestState.Processes | Where-Object { $_.Id -in $global:AgentGitLauncherTestState.Killed } | Select-Object -ExpandProperty Id)
    [array]::Reverse($expectedKilled)
    Assert-True (($global:AgentGitLauncherTestState.Killed -join ',') -eq ($expectedKilled -join ',')) "$($case.Name) 清理顺序错误"
    Assert-True ($env:AGENTGIT_API_TOKEN -eq 'original-token') '调用者令牌未恢复'
    Assert-True ($env:AGENTGIT_API_URL -eq 'http://127.0.0.1:9000') '调用者 API 地址未恢复'
    Assert-True ($null -eq $env:PYTHONUNBUFFERED) '调用者空环境变量未恢复'
    Assert-True ((Get-Location).Path -eq $location) '调用者工作目录未恢复'
    Assert-True (@($global:AgentGitLauncherTestState.Calls | Where-Object { $_ -match 'alembic upgrade|pg_dump|compose (stop|down)' }).Count -eq 0) '启动流程发生意外数据库写入或停机'
    if ($options.NoDesktop) {
      Assert-True (@($global:AgentGitLauncherTestState.Calls | Where-Object { $_ -match 'npm|node' }).Count -eq 0) '纯后端模式调用了 Node/npm'
    }
    if ($options.SkipCompose) {
      Assert-True (@($global:AgentGitLauncherTestState.Calls | Where-Object { $_ -match 'docker' }).Count -eq 0) 'SkipCompose 模式调用了 Docker'
    }
    Microsoft.PowerShell.Utility\Write-Host "通过 $($case.Name)"
  }
  Microsoft.PowerShell.Utility\Write-Host "启动脚本回归通过，共 $($cases.Count) 项。"
} finally {
  foreach ($name in $originalEnvironment.Keys) {
    if ($null -eq $originalEnvironment[$name]) {
      Remove-Item "Env:\$name" -ErrorAction SilentlyContinue
    } else {
      [Environment]::SetEnvironmentVariable($name, $originalEnvironment[$name], 'Process')
    }
  }
  Remove-Variable AgentGitLauncherTestState -Scope Global
  $global:LASTEXITCODE = $originalExitCode
  # 仅删除本测试创建且位于系统临时目录内的唯一目录。
  $resolved = [IO.Path]::GetFullPath($testRoot)
  $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
  if (-not $resolved.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) { throw '测试清理路径越界' }
  Remove-Item -LiteralPath $resolved -Recurse -Force
}
