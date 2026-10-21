# 第三方运行环境

`vendor/python-3.11.9-embed-amd64.zip` 是 CPython 3.11.9 的 Windows x64 嵌入式发行包。

- 官方来源：<https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip>
- SHA-256：`009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b`
- 运行环境及随附组件的许可文本随官方 ZIP 保留，请查阅其中的 `LICENSE.txt`。

程序使用 Windows / .NET Framework 的系统组件；不会把聊天内容交给在线 Markdown 渲染服务。

本文件只说明第三方组件，不为本项目自行指定开源许可证。

## Zstandard 1.5.7

`vendor/zstd-v1.5.7-win64.zip` 是 Zstandard 项目的官方 Windows x64 发行包。
程序校验整个 ZIP 后，从中提取 `dll/libzstd.dll` 到本工具的 `.runtime`，通过标准库 `ctypes` 流式读取压缩会话；运行时不下载组件。

- 官方来源：<https://github.com/facebook/zstd/releases/download/v1.5.7/zstd-v1.5.7-win64.zip>
- SHA-256：`acb4e8111511749dc7a3ebedca9b04190e37a17afeb73f55d4425dbf0b90fad9`
- 此摘要也与 OpenAI Codex 仓库 `.github/workflows/zstd` 中固定的官方发行包摘要一致。
- 本工具按 BSD 许可分发此组件，完整许可文本保存在 `vendor/zstd-LICENSE.txt`，并随便携包分发。
