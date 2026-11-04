# 公开 HTTPS 测试证书

`model_https_public_test_cert.pem` 和 `model_https_public_test_key.pem` 仅用于回环测试服务器。这是刻意公开的自签名测试密钥，不是供应商凭据、部署证书或任何账户秘密。仅测试构造的内存 TLS context 信任它，不安装到操作系统证书库。

证书 SAN 使用 `api.openai.com`，用于验证生产传输仍要求该主机名；测试只替换 DNS 返回的地址为测试服务器的 `127.0.0.1:随机端口`。默认系统信任库必须拒绝此证书。证书有效期为生成日起十年；到期时重新生成这两个测试文件。
