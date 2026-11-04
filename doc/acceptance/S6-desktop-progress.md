# S6 桌面初版增量

状态：初版实现与定向测试通过，**S6 未验收**。不替代 D1、真实后端五工具、真实模型 Adapter、DPI、安装/便携包及独立用户证据。

本文保留初版检查基线。后续真实五工具/Replay/Diff 联调、查询字段与切页取消修复，以及当前 15 项桌面单元结果见 [真实联调](S6-desktop-real.md)；不能用本页历史的 14 项数量覆盖最新结果。

## 本批实现

建立 `desktop/`，固定构建环境/依赖锁；main、sandbox preload、React renderer、shared 契约分离。主进程只允许固定业务路由，验证参数、项目、当前窗口/顶层 frame/受信任文档。自定义协议只读包内页面和受限资产，realpath 防越界，设置 CSP/nosniff；权限、新窗口、外部导航和渲染网络默认拒绝。窗口启用 contextIsolation、sandbox、webSecurity，关闭 Node/webview。

四页初版已经接业务方法：Runs 串行受控创建配置/任务/Run 并保留原动作键；Timeline 显示图、事件、状态、实际调用来源、字节分段、版本产物及控制/审批；Branch 的 Recorded/Live/Fork 字段互斥并保留原请求；Diff 使用后端 JSON Pointer 和客观前后值。图与非首页按需加载，无新的状态/请求库。

连接每 5 秒只读检查，事件按游标顺序补齐，退出页面取消 GET，旧上下文回复丢弃；不自动重发 POST。原生保存回调接收经归属/摘要校验的产物，renderer 不能传宿主路径。单实例、崩溃提示重开及非敏感窗口偏好已接入；仍待完整生命周期实测。

## 已运行的检查

- `npm run typecheck`：main/preload/shared 与 renderer 严格类型通过。
- `npm test`：**14 passed**，覆盖回环/重定向、身份脱敏、尺寸/超时、POST 未知不重发、GET 取消、项目/端点竞争、业务字段/资源白名单、摘要/原字节导出、sender/frame/文档及路径验证、偏好写入/重开。
- `npm run test:electron`：**1 条真实 Electron 综合测试通过**。HTTP 服务为测试 fixture：包内页面与字体、四页与关系图、Recorded/Live 字段互斥、客观 Diff、三步提交双击只产生 3 个 POST、无 Node/process/raw IPC、额外窗口桥接拒绝、外部打开/导航拒绝、HTML 注入仅为文本、身份与项目头不带 Origin、界面和偏好不含令牌。
- 真实 Electron 检查 Runs 在 1440/1024/768px，Timeline/Branch/Diff 在 768px，另四页补充 360px，根文档无横向溢出，表格/图/代码内部允许滚动。已经视觉查看四页及窄窗口截图；**尚未完成全部宽度×DPI 矩阵和完整键盘流程**。
- 生产资源构建通过；初始 JS 约 237.5 kB，React Flow 图约 178.7 kB 独立按需加载；本地字体约 1.92 MB。React Flow 的客户端 directive 构建提示保留，不涉及本应用不存在的 SSR。

原始证据在忽略的 `.agentgit/evidence/`。`S6-desktop-unit.xml` SHA256 `ebce75869b5acd361cc9636a472c3e9255a57fc566cd8b145ede1dfe2c04007c`；真实 Electron 最终 XML 的摘要另由当前测试产物核对，不冻结不断更新的截图为正式发布基线。

初次额外窗口攻击测试准备误用了已不返回 preload 字段的 getLastWebPreferences，修正为从测试端传入实际绝对 preload 路径；未放宽应用 sandbox。导航被取消时 Playwright 缓存 URL 为空，已同时用真实 webContents.getURL 和页面 location.href 验证仍为受信任文档。360px 检查发现隐藏表头绝对定位扩大页面范围，已约束到表格滚动容器；窄表格保持正常列宽并内部滚动。

## 视觉与规范核对

四页概念及修正说明见 [concepts](../desktop/concepts/README.md)。按 DESIGN 使用本地授权替代字体、1200px 最大宽度和现有暖色；实测对比度后先增补 DESIGN accessible 变体，再在代码使用。普通正文/按钮不使用不达标的原 primary 白字组合。

Impeccable detector 一批检查及修正后确认：错误提示改为完整细边框；剩余 Inter 两处与当前项 2px 标记均是既定 DESIGN / 概念方向，保留并记录，不擅自换品牌字体。没有把检测器风格警告当作功能验收，也未以此引入新视觉体系。

## 后续门槛

真实 API/Worker/PostgreSQL/固定容器的桌面五工具→历史→Fork→Recorded/Live→Diff→原生导出；断连与进程恢复/单实例/审批/长内容；完整宽度/DPI/键盘；真实模型 Adapter 与明确开启的供应商冒烟；Forge 安装/ZIP、摘要/依赖/签名和独立 Windows 用户离线安装/升级/卸载。上述证据齐全前不合并或标记 S6 完成。
