# D1 桌面离线操作与演示步骤

本指南对应 S6 已实现的四页操作，不表示 D1 已最终验收。当前候选和剩余门槛见 [实施进度](PROGRESS.md)及[独立 Windows 清单](S6-windows-acceptance.md)。使用固定离线 Mock，不配置或调用真实供应商；真实模型冒烟须另行明确开启。演示结果只证明历史、隔离执行与客观差异，修复有效性属于后续阶段。

## 准备和启动

后端操作者先按[开发说明](DEVELOPMENT.md)和[固定镜像说明](../sandbox/README.md)准备 PostgreSQL、迁移、数据根、Worker 与已审阅镜像。API、Worker 和桌面必须使用一致的项目环境；身份令牌由操作者预置，不粘贴到界面、启动参数或截图，也不写入本指南。演示期间保持后端源码与 uv.lock 不变，旧 Job 不会绕过代码兼容性检查。

在项目根目录检查环境，再在两个已配置身份的终端分别启动 API 和 Worker：

```powershell
uv run --locked agentgit doctor --database
uv run --locked agentgit doctor --sandbox

# 终端一，保持运行。
uv run --locked agentgit serve --port 8000

# 终端二，保持运行。
uv run --locked agentgit worker
```

doctor 不创建执行容器或自动下载镜像。检查失败时先修复后端；API 可连接并不证明 Worker、Docker 或固定镜像可用。

从[当前候选清单](S6-windows-acceptance.md)取得并核对 Setup / ZIP 的版本、摘要与签名状态。安装版可在已预置匹配令牌的终端执行 `& "$env:LOCALAPPDATA\AgentGit\AgentGit.exe"`；便携版从新目录解压后启动其中 AgentGit.exe。两者均不需要 Node/npm/Vite。当前候选未签名，若系统拦截则保留实际结果，不将“已构建”记为“已安装通过”。

桌面默认连接 http://127.0.0.1:8000，也可在“连接与项目”中填写其他已准备的回环 HTTP 地址，点击“连接”或“重新检查”。确认身份 accepted、数据库 ready、协议版本兼容；Worker / 容器保持未知，直到实际任务提供证据。

## 运行：创建一次固定五工具任务

1. 首次演示在“新项目名称”填写一个新名称，点击“创建并选择”。已有项目则填写完整 UUID 并“选择项目”，不要把 Run / Job ID 当作项目 ID。
2. 收起连接面板，在“运行”页点击“新建运行”。选择离线 Mock，保留“启用固定五工具”和现有有界预算。使用默认目标“比较 Aster、Birch、Cedar 的能力与约束，计算统计并整理产物。”，选择 main 后提交一次。
3. 保存受理后的完整 Run / Job ID。等待终态“已完成”；持续排队时检查后端 Worker，sandbox_unavailable 时按界面建议核验执行环境。不要用再次新建任务代替查询未确认的提交。

固定示例先检索、读来源和写产物，再实际运行 code_exec 统计、terminal_exec 整理。成功结果包括 results/statistics.json 和 results/report.md；统计应有 3 个框架、6 个来源、9 条 claim。具体协议与 7 个产物的说明见 [五工具流程](API.md#五工具容器流程)。缺设施、失败、预算耗尽或未知结果均不能记作五工具通过。

## 时间线：查看历史、代码和原字节

在“时间线”选中一个检查点。“逻辑状态”随该检查点切换；图节点和下方列表都可选择，键盘用 Tab 到节点后按 Enter 或空格。焦点采用 DESIGN 的 ink 外框，不能只看选中色判断当前详情。

“执行证据”显示本 Run 的实际执行账本，包括物理来源、命令/代码、stdout/stderr、退出码、截断与清理确认；它与所选历史状态分别展示。用“执行调用”和“原始证据”切换到 Python 代码、Shell 命令及日志。未知退出码不是 0，账本 complete 也不等于工具成功，仍须核对 error_code、完整性和输出 manifest。

“产物文件”保持当前 Commit 的版本，可点击名称预览原字节。代码、HTML、Markdown 都作为文本显示；历史同名文件不会自动换成最新版本。记录来源 Run、main 的 HEAD 以及需要比较的完整 Commit ID。

## 分支实验：Fork 与 Recorded

1. 从来源 Run 最早的安全检查点点击“从所选检查点分支 →”，或在“分支实验”填写完整来源 Run 与安全起点 Commit。
2. 选择 Fork，填写新名称，例如 demo-fork，提交。成功只表示建立了分支，不会自动运行或验证。
3. 重新打开来源 Run，在同一起点选择 Recorded。验证终点填写来源已完成时的 HEAD，另取新分支名 demo-recorded，提交并保存新 Job / Run ID。
4. 在受理区域或新 Run 查看报告：目标范围到达、verified=true，逻辑状态与来源一致，实际模型/工具调用为 0。缺录制或不兼容应明确失败，不能改成 Live 后仍称作 Recorded 通过。

起止点和分支名称都由后端校验；未知副作用、待审批或名称冲突时保留错误，不覆盖已有分支或修改历史来通过检查。完成后回到原项目的 main，确认来源 HEAD 未被分支操作推进。

## 分支实验与差异：A/B Live

本例只展示两个独立执行结果的文件差异，不进行修复有效性或因果判断。

1. 在来源历史中找到工具启动前的安全检查点，其“逻辑状态”的 pending_tool.tool 为 terminal_exec。复制完整 Commit ID，两次 Live 都使用这个来源起点。
2. 选择 Live，填写新分支 demo-live-A。在“干预 overrides JSON”填入：

```json
{
  "schema_version": 1,
  "artifacts": [
    {
      "path": "execution/organize.sh",
      "text": "set -eu\nmkdir -p results\nprintf A > results/desktop-same.txt\n"
    }
  ],
  "execution": {
    "tool": "terminal_exec",
    "path": "execution/organize.sh"
  }
}
```

3. 使用界面允许的 Mock 配置及独立有界预算，提交并等待新 Run 完成，保存 HEAD。Live 使用新 Task / Job / Run、工作区、预算与授权，不继承旧审批。
4. 从同一来源起点创建 demo-live-B，只把脚本里的 printf A 改为 printf B。等待完成，保存第二个 HEAD。
5. 在“差异”页的 BASE / HEAD 填入两个新 HEAD，点击“比较”。展开 results/desktop-same.txt 对应项，两边摘要应不同；分别打开对应 Run 的产物，原字节为 A 和 B。不要把任一分支结果写回来源 main。

## 原生保存与生命周期复核

从 A 的所选版本点击“另存为”，在真实 Windows 对话框选择新临时目录的 A.txt。成功提示后核对文件原字节为 A；再打开保存框，选择取消，确认没有新增文件。不能用替换 showSaveDialog 返回值的自动化测试代替这一步；当前本机原生窗口控制仍缺成功证据。

关闭桌面不会停止独立后端任务。重开后连接同一项目，用保存的 Job / Run ID 查询进展，不重新提交。重复启动、崩溃恢复、覆盖升级和卸载的数据保留按[独立 Windows 清单](S6-windows-acceptance.md)执行；仅在测试自有安装中操作，不删除业务数据库、CAS 或产物。

留存候选摘要、实际环境、项目 / Run / Job / Commit、截图和结果，凭据始终排除。禁止外网、无开发源码/构建 Node、独立用户、真实原生保存及安装/ZIP 两种分发形态的证据必须各自齐全，才能完成 D1，不能用本指南或单项成功代替阶段验收。
