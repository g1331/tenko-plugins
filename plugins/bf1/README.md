# 战地一查询

经 [bf-manager](https://github.com/g1331/bf-manager) 的公开查询接口查询战地一数据，回复文字。

| 命令 | 说明 |
| --- | --- |
| `/bf1s <EA 名字>` | 生涯战绩 |
| `/bf1w <EA 名字>` | 武器击杀前十 |
| `/bf1v <EA 名字>` | 载具击杀前十 |
| `/bf1ss <服务器名关键字>` | 搜索有人的私服，显示前十个 |

新群默认开启。

## 配置

```toml
[plugin_config.bf1]
base_url = "https://bfm.umrbotech.com"  # bf-manager 地址，不带 /api/v1
```
