# tenko-plugins

[Tenko](https://github.com/g1331/tenko) 的官方插件。每个插件在 `plugins/<插件 ID>/` 下，各自是一个独立发布到
PyPI 的包，并收录在 [Tenko 插件目录](https://github.com/g1331/tenko-registry)里，等级为官方。

| 插件 | 包名 | 说明 |
| --- | --- | --- |
| [复读机](plugins/repeater) | `tenko-plugin-repeater` | 群友复读时偶尔跟一句，偶尔打断一下 |

## 安装

在 Tenko WebUI 的插件市场安装，或由 Master 在聊天里 `/安装插件 <插件 ID>`。第三方插件默认不启用，启用后重启生效。

## 开发

本仓库是一个 uv 工作区，开发依赖里包含固定标签的 Tenko 宿主，测试会加载真实的宿主：

```text
uv sync
uv run ruff check .
uv run pytest -q
```

新插件照 Tenko 的[示例插件](https://github.com/g1331/tenko/tree/main/examples/tenko-plugin-example)写，放进
`plugins/<插件 ID>/`，`pyproject.toml` 的包名用 `tenko-plugin-<名字>`，`[project.urls]` 指向本仓库。

## 发布

1. 在 PyPI 上为新包添加 pending publisher：所有者 `g1331`、仓库 `tenko-plugins`、工作流 `publish.yml`、环境 `pypi`。
2. 修改插件 `pyproject.toml` 和 `plugin.metadata` 里的版本，提交后推送标签 `<插件 ID>-v<版本>`，例如：

   ```text
   git tag repeater-v0.1.0 && git push origin repeater-v0.1.0
   ```

3. 首次发布后，在插件目录用投稿表单提交 PyPI 包名；之后的新版本由目录每天自动发现。

## 许可证

GPL-3.0-only，见 [LICENSE](LICENSE)。
