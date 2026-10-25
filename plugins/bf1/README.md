# 战地一查询

经 [bf-manager](https://github.com/g1331/bf-manager) 的公开查询接口查询战地一数据，回复图片；渲染不可用时退回文字。

| 命令 | 说明 |
| --- | --- |
| `/bf1绑定 <EA 名字>`（简写 `/bf1b`） | 绑定自己的 EA 账号；不带名字查看当前绑定 |
| `/bf1s [EA 名字]` | 生涯战绩；绑定后可不带名字 |
| `/bf1w [EA 名字]` | 武器击杀前十 |
| `/bf1v [EA 名字]` | 载具击杀前十 |
| `/bf1ss <服务器名关键字>` | 搜索有人的私服，显示前十个 |
| `/bf1pl <服务器名关键字 \| gameId>` | 服务器玩家列表（含排队、观战和行动进度）；匹配多个时取人最多的 |
| `/bf1` | 全服在线概况 |

新群默认开启。

## 配置

```toml
[plugin_config.bf1]
base_url = "https://bfm.umrbotech.com"  # bf-manager 地址，不带 /api/v1
```
