# S6 当前用户 0.1.0 安装基线

状态：首次实际 Setup 安装与安装目录中的五工具完整桌面流程通过；独立 Windows 用户、升级/卸载与实际原生保存仍未验收。本记录补充现有便携验证，不替代实施方案 S6-T10 的独立用户条件。

初始已核实 `%LOCALAPPDATA%/AgentGit`、`%APPDATA%/AgentGit` 均不存在，没有运行中的 AgentGit/Update 进程。本次只安装本项目已构建的 `b649bf3` / AgentGit 0.1.0，Setup SHA256 `6004d327e6e20598a659f769f5286bde0c931f1f524fee56bca4cc034ce21bab`，仍为 NotSigned。

`AgentGitSetup.exe --silent` 退出 0，Squirrel 日志确认首次完整安装至 `C:/Users/DC/AppData/Local/AgentGit/app-0.1.0`，创建根执行 stub、Update.exe、开始菜单与桌面快捷方式。当前用户的普通安装无需提升权限；不据此宣称独立用户环境已完成。

对实际安装目录运行 `audit-package.cjs`：app.asar 94 项、27 个构建文件与 b649bf3 构建相同；字体/OFL/26 项声明授权通过，无开发源码/测试/业务数据。安装程序附加的 Squirrel 文件另由安装日志解释，不冒充 ZIP 的逐文件总数。

`AGENTGIT_DESKTOP_EXECUTABLE` 指向 app-0.1.0/AgentGit.exe，独立临时 cwd，客户端 PATH 只有 System32、无 NODE_PATH/NODE_OPTIONS/Vite。真实独立 API/Worker/PostgreSQL/固定 Docker 完成五工具→历史/Fork→Recorded 28 检查点零模型/工具/容器→A/B Live→Diff/导出，**1 passed，45.5s**。来源指纹/main 不变，4 容器清理确认。导出仍替换原生保存框返回值，不能算真实保存交互。

后续以实际桌面 0.1.1 构建测试版本升级，后端协议保持 0.1.0、Schema 保持 0012。为避免发布后界面仍硬编码旧版本，页脚和授权面板改从 package.json 的构建元数据获取桌面版本，Electron 测试同时比较 app.getVersion。此次元数据变化不升级任何依赖；类型检查和真实 Electron 边界测试通过（1 passed，9.8s）。新包尚待构建、安装和卸载验证，不能在此提前算通过。

包内 Squirrel 实测 ProductVersion 为 2.0.1+eef37460ae。已核对[同提交的官方 CLI 源码](https://github.com/Squirrel/Squirrel.Windows/blob/eef37460ae/src/Update/Program.cs)：Install 会重建本包安装目录，Uninstall 调用 FullUninstall；实际操作前必须核实本次测试所拥有的绝对安装根，业务数据和测试 API/Worker 保持在安装根外。

原始证据位于忽略的 `.agentgit/evidence/`：

| 文件 | SHA256 |
|---|---|
| S6-setup-result.json | `9913c001d3119a71eff922b3aad0e5717062d06f885b84e2c1358fe476e9396f` |
| S6-setup-010.log | `7effa76f1aad0c6e4451955d8a10dc25768a15d2546d8f2d6dc44d70f2304756` |
| S6-shortcut-010.log | `97c96e2b56ee98dccc7b6b23bd9040d1142eb1924d82d1c8b8e8a9f6aa4b41fb` |
| S6-installed-010-audit.json | `b245b4387f1a0ceff0d07dd7eb42a5cf73d827aaeac7db3ae5e8b887b68f1252` |
| S6-installed-010-real.xml | `500247970037ebc59898d715ab1355e98cff1c5d817c11ad8ab84f3d1862c74c` |
| S6-installed-010-real-audit.json | `405b9af0beac136c03fc1554992b1822f351bbd80a1cd0f84477322d9e93ccd1` |
