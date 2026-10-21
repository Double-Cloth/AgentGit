# 固定研究语料

权威语料位于 [`src/agentgit/adapters/fixtures/research.json`](../../../src/agentgit/adapters/fixtures/research.json)，随 Python 包分发，避免生产代码导入 tests。包含 3 个虚构框架，各 2 份资料，固定采集日期、有效区间、正文 SHA-256 与结构化可引用事实。

运行时由 `Corpus.bundled()` 校验正文 hash、引用原文与时间区间。测试构造过期或带提示词注入的独立副本，不修改权威文件或放宽验收器。固定评估时间为 `2026-10-20T00:00:00Z`，不得将其表述为实时外部资料。
