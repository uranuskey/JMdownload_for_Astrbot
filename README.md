# JMdownload_for_Astrbot

基于 AstrBot 的 QQ 漫画监听插件（jmcomic 集成版）。

核心能力：
- 支持触发关键词前缀自定义（如 `/`、`!`、`漫画`）。
- 支持群聊与用户白名单控制，支持黑名单与群管理员绕过白名单。
- 支持漫画搜索（关键词或番号）。
- 支持多页搜索与快速翻页（`next`）。
- 支持漫画下载（番号或链接），并自动：图片 -> PDF -> 改名为 EXE 后缀 -> 加密 ZIP。
- 支持章节级重试与断点续下，降低大本下载中断概率。
- 支持失败章节补偿重试，并输出失败章节清单。
- 支持下载缓存去重，重复请求可直接命中缓存发送。
- 支持质量档位（`fast` / `balanced` / `high`）平衡体积与画质。
- 支持下载队列与并发上限（全局/同群）。
- 支持配额与冷却策略。
- 支持指定章节下载：`jmcomic 123 p456`。
- 支持管理员动态控制最大下载页数和功能开关。
- 支持 `help` / `doctor` / `stats` 指令。
- 支持审计日志与近 7 天统计。
- 完成后将 ZIP 文件回传到触发聊天窗口。
- 全流程状态提示与异常友好提示。

## 1. 目录结构

```text
JMdownload_for_Astrbot/
  ├─ main.py
  ├─ plugin_types.py
  ├─ services/
  │   ├─ manga_service.py
  │   ├─ package_service.py
  │   ├─ send_service.py
  │   ├─ cache_service.py
  │   └─ audit_service.py
  ├─ metadata.yaml
  ├─ _conf_schema.json
  ├─ requirements.txt
  └─ README.md
```

## 2. 安装方式

1. 将 `JMdownload_for_Astrbot` 放入 AstrBot 插件目录（通常为 `data/plugins/`）。
2. 安装依赖：

```bash
pip install -r requirements.txt
```

3. 重启 AstrBot 或执行插件热加载。
4. 在插件管理中启用 `JMdownload_for_Astrbot`。

## 3. 配置项说明

- `enabled`：是否启用插件。
- `trigger_keywords`：触发关键词前缀列表。仅匹配此前缀开头的消息。
- `allowed_group_ids`：群聊白名单。为空时不限制。
- `allowed_user_ids`：用户白名单。为空时不限制。
- `blacklist_group_ids`：群聊黑名单（优先生效）。
- `blacklist_user_ids`：用户黑名单（优先生效）。
- `allow_group_admin_bypass`：开启后，群管理员可绕过 `allowed_user_ids`。
- `deny_reply_enabled`：非白名单触发时是否提示无权限。
- `deny_reply_text`：无权限提示文案。
- `download_root`：下载与中间文件目录。
- `cache_root`：缓存目录（缓存命中会直接发送，默认 `data/plugin_data/JMdownload_for_Astrbot/cache`）。
- `cache_ttl_hours`：缓存有效期（小时）。
- `search_result_limit`：关键词查询时读取的候选结果上限。
- `download_profile`：质量档位（fast / balanced / high）。
- `pdf_layout_mode`：PDF 布局模式（multipage / longpage）。
- `long_page_max_images`：longpage 最大图片数阈值，超出自动回退 multipage。
- `long_page_max_height`：longpage 最大总高度阈值，超出自动回退 multipage。
- `zip_level`：ZIP 压缩等级（0~9）。
- `zip_password`：ZIP 压缩密码。
- `default_max_page`：默认最大下载页数（防止超大本任务失控）。
- `retry_per_chapter`：每章节下载失败重试次数（建议 2~3）。
- `download_concurrency_limit`：下载全局并发上限。
- `group_download_concurrency_limit`：同群下载并发上限。
- `daily_quota_per_user`：每用户每日下载配额，0 为不限。
- `cooldown_seconds`：同一用户下载冷却时间。
- `confirm_ttl_seconds`：超上限时 `/yes` 确认有效秒数（默认 180）。
- `audit_log_path`：审计日志文件路径（jsonl）。
- `admin_user_ids`：管理员 QQ 号列表，用于执行管理指令。

## 4. 指令格式

注意输入如下指令前需要加上唤醒词，如唤醒词是“/”，即为//jmcomic 搜索：

1. 搜索漫画

```text
/jmcomic 搜索 关键词
/jmcomic 搜索 关键词 5
/jmcomic 搜索 关键词 p2 5
/jmcomic next
/jmcomic 搜索 422866
```

说明：
- 当输入 `关键词 数量`（如 `saber 5`）时，会按热度排序返回前 N 条简介。
- 数量默认 3，最大 20。
- 支持 `p页码` 指定页；`/jmcomic next` 会继续上一条搜索翻页。

2. 下载漫画（生成 PDF + ZIP 并发送）

```text
/jmcomic 422866
/jmcomic 422866 p123456
/jmcomic 下载 422866
/jmcomic 下载 https://18comic.vip/album/422866
```

可选前缀示例：

```text
!漫画 搜索 某关键词
漫画 下载 422866
```

3. 管理指令（仅 admin_user_ids）

```text
/jmcomic set maxpage 200
/jmcomic open
/jmcomic close
```

4. 辅助指令

```text
/jmcomic help
/jmcomic doctor
/jmcomic stats
/yes
/no
```

## 5. 交互流程

下载类指令会按顺序提示：
- 下载前预检（预计章节数/总页数）
- 正在查询
- 正在下载
- 排队中（有并发压力时）
- 正在生成 PDF
- 正在转换 EXE 后缀
- 正在加密压缩 ZIP
- 正在发送
- 发送完成

若命中缓存，会直接提示“命中缓存，正在发送”。

当预计总页数超过当前上限时：
- 会先告警并暂停任务；
- 需在 `confirm_ttl_seconds`（默认 180 秒）内发送 `/yes` 才会继续；
- 发送 `/no` 可主动取消待确认任务。

当配置 `default_max_page=200` 时，超出页数会自动截断，仅处理前 200 页。

PDF 去白缝策略：
- `multipage`（默认）：多页 PDF + 轻裁白边 + 避免 balanced 缩放，兼容性最好。
- `longpage`：先拼成长图再生成单页超长 PDF，可从根源规避页边界缝；当图片数或总高度过大时会自动回退到 `multipage`。

## 6. 异常处理

已覆盖：
- 网络失败
- 漫画不存在 / 未找到匹配
- 下载失败
- PDF 处理失败
- ZIP 压缩失败
- 文件发送接口不可用（自动退化文本提示）
- 失败章节补偿后仍失败（输出章节清单）

## 7. 注意事项

- 请确保运行环境可访问 jmcomic 对应站点。
- 白名单建议优先配置，避免功能被非目标群组滥用。
- 文件发送仅使用“消息链文件组件（MessageEventResult + File）”路径，代码更简洁。
- ZIP 包内文件后缀为 `.exe`，实际内容为由图片合成的 PDF，仅用于规避部分平台后缀限制。
- 下载的临时图片会在任务结束后自动清理。
