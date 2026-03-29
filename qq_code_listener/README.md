# qq_code_listener

基于 AstrBot 的 QQ 漫画监听插件（jmcomic 集成版）。

核心能力：
- 支持触发关键词前缀自定义（如 `/`、`!`、`漫画`）。
- 支持群聊与用户白名单控制。
- 支持漫画搜索（关键词或番号）。
- 支持漫画下载（番号或链接），并自动：图片 -> PDF -> 改名为 TXT 后缀 -> 加密 ZIP。
- 完成后将 ZIP 文件回传到触发聊天窗口。
- 全流程状态提示与异常友好提示。

## 1. 目录结构

```text
qq_code_listener/
  ├─ main.py
  ├─ metadata.yaml
  ├─ _conf_schema.json
  ├─ requirements.txt
  └─ README.md
```

## 2. 安装方式

1. 将 `qq_code_listener` 放入 AstrBot 插件目录（通常为 `data/plugins/`）。
2. 安装依赖：

```bash
pip install -r requirements.txt
```

3. 重启 AstrBot 或执行插件热加载。
4. 在插件管理中启用 `qq_code_listener`。

## 3. 配置项说明

- `enabled`：是否启用插件。
- `trigger_keywords`：触发关键词前缀列表。仅匹配此前缀开头的消息。
- `allowed_group_ids`：群聊白名单。为空时不限制。
- `allowed_user_ids`：用户白名单。为空时不限制。
- `deny_reply_enabled`：非白名单触发时是否提示无权限。
- `deny_reply_text`：无权限提示文案。
- `download_root`：下载与中间文件目录。
- `search_result_limit`：关键词查询时读取的候选结果上限。
- `zip_level`：ZIP 压缩等级（0~9）。
- `zip_password`：ZIP 压缩密码。

## 4. 指令格式

以 `trigger_keywords` 包含 `/` 为例：

1. 搜索漫画

```text
/漫画 搜索 关键词
/漫画 搜索 422866
```

2. 下载漫画（生成 PDF + ZIP 并发送）

```text
/漫画 下载 422866
/漫画 下载 https://18comic.vip/album/422866
```

可选前缀示例：

```text
!漫画 搜索 某关键词
漫画 下载 422866
```

## 5. 交互流程

下载类指令会按顺序提示：
- 正在查询
- 正在下载
- 正在生成 PDF
- 正在转换 TXT 后缀
- 正在加密压缩 ZIP
- 正在发送
- 发送完成

## 6. 异常处理

已覆盖：
- 网络失败
- 漫画不存在 / 未找到匹配
- 下载失败
- PDF 处理失败
- ZIP 压缩失败
- 文件发送接口不可用（自动退化文本提示）

## 7. 注意事项

- 请确保运行环境可访问 jmcomic 对应站点。
- 白名单建议优先配置，避免功能被非目标群组滥用。
- 不同 AstrBot/适配器版本文件发送接口命名可能不同，本插件已做多接口兼容尝试。
- 下载的临时图片会在任务结束后自动清理。
