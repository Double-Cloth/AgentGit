---
version: 1
slug: "desktop-src-renderer-runs-tsx"
primary_target: "desktop/src/renderer/Runs.tsx"
related_targets: ["desktop/src/renderer/Collaboration.tsx","desktop/src/renderer/RunAdapterSummary.tsx","desktop/src/renderer/Timeline.tsx","desktop/src/renderer/styles.css"]
---

## Direction contract

THESIS：操作者在同一工作区核对四个独立角色的进展、预算和消息来路，再明确执行组控制；不把请求已接受当作全部角色已停。

OWN-WORLD：沿用既有 DESIGN 的暖白画布、珊瑚操作、墨色正文及现有字体、panel、facts、table、notice，不改变全局六页导航或设计令牌。

STORY：从运行页切换协作，准备固定离线研究，查看四个 Run、消息父事件和共享版本；准备与启动分别确认，成功 Live 可另建 Recorded 组。

FIRST VIEWPORT：保留运行标题，下面以单次运行／四角色协作切换。协作页先展示有界列表及按 ID 打开；详情先给组状态、是否静止、控制修订与当前可执行操作，其后为四角色表、消息与共享版本。

FORM：既有操作工作区的精确扩展，code-led，无批准效果图；seed=not-run-precise-extension。宽屏表格，窄屏局部滚动，表单纵向堆叠；显式重试保留原请求及幂等键。

FINISH：unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

## 完成核对（2026-10-31）

Operate／code-led 精确扩展。run-v2 的9张真实 Electron 截图覆盖准备、列表、角色1440／768／360、消息、共享版本、Recorded重开、断连原请求；逐张保留 capture metadata。独立 fresh finish reviewer 五节结论 disposition=ship，仅限本开发增量；独立 documenter 对比既有 DESIGN 后确认延续。无新增 shipping raster，无批准效果图。手工 detector 只有历史 Inter／选中边线，未作全局修复；DESIGN 保持原 SHA256，sidecar 原本缺席且不生成。原生 Windows／DPI、正式 C/T05/D3 及最终包门槛保留。证据目录与命令见 doc/acceptance/S12-C-desktop.md。
