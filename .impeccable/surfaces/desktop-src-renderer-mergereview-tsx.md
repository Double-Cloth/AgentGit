---
version: 1
slug: "desktop-src-renderer-mergereview-tsx"
primary_target: "desktop/src/renderer/MergeReview.tsx"
related_targets: ["desktop/src/renderer/styles.css"]
---

## Direction contract

THESIS：开发者逐项核对两个真实执行来源，解决候选冲突，再以确切票据批准。来源、候选和执行决定分别可见。

OWN-WORLD：沿用 DESIGN 的暖白画布、珊瑚主操作、墨色正文、EB Garamond 标题、Inter 表单及 JetBrains Mono 原文。采用现有 panel、facts、notice、details 与 table，不改变既有五页体系。

STORY：进入先发现或建立提案，核对 base/ours/theirs，编辑并保存新修订，重验及本人审阅；批准后另行建立独立分支和新任务。

FIRST VIEWPORT：现有顶栏的第六项“合并审阅”；左对齐标题和一句目的。上方有界历史表与按 ID 打开，下一块双来源创建表单；打开后依次为当前状态、三方原文、候选/冲突、修订、验证与决定，主操作跟随当前步骤。

FORM：既有操作工作区的精确扩展，沿用已确认 S11 结构；未运行形式抽签，seed=not-run-precise-extension。三方列在宽屏并排，最窄支持窗口堆叠，长原文在局部滚动。

FINISH：unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
