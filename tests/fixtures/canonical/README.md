# AGJSON-v1 黄金向量

向量在 S1 冻结，预期规范字节为独立书写的 UTF-8 文本或二进制十六进制，摘要使用标准库 SHA-256 对以下封装计算：

`agentgit NUL AGJSON-v1 NUL object_type NUL payload_byte_length NUL payload`

头长度以载荷 UTF-8 **字节数**计；对象类型在 core/canonical.py 的受限枚举中定义。Blob 不做文本转换。正文保留 Unicode，不作 NFC 归一化。环境和 Commit 向量是内容协议示例，不含秘密及操作元数据。修改预期需要协议升级或记录依据充分的规范修正，不可为迁就实现自动重写期望值。
