# YouTube Topic Summary — 每日精选内容选题推送

每日自动扫描自定义 YouTube 订阅频道，用 LLM 智能筛选最值得深度观看的长视频，生成中文摘要和推荐理由。配合 Hermes Agent 推送到微信，帮你从海量内容中高效选题。

## 三段式流水线

```
阶段一：候选收集            阶段二：Gemini 智能排序      阶段三：摘要 + 保存
     │                           │                           │
41 个频道 RSS 并发轮询         硬规则预过滤                Top N 视频
     ↓                           ↓                           ↓
过滤 Shorts (< 3min)           Gemini 智能排序             yt-dlp 获取字幕
     ↓                     (MiniMax 兜底)                    ↓
YouTube Data API            输出 Top N + 推荐理由         MiniMax 中文摘要
(时长+描述+播放量)                                              ↓
                                                            日报保存
```

## 技术栈

| 组件 | 技术 |
|------|------|
| 视频源 | YouTube RSS + Data API v3 |
| 字幕 | yt-dlp |
| 智能排序 | Gemini 2.0 Flash |
| 摘要生成 | MiniMax M2.5 |
| 定时调度 | GitHub Actions（每天 10:00 北京时间） |
| 推送方式 | Hermes Agent → 微信（需自建 Hermes Gateway） |
| 去重 | history.json，自动清理 30 天前记录 |

## 快速开始

### 1. Fork 并配置 Secrets

在 GitHub 仓库 Settings → Secrets and variables → Actions 中添加：

| Secret | 说明 |
|--------|------|
| `YOUTUBE_API_KEY` | YouTube Data API Key |
| `GEMINI_API_KEY` | Google Gemini API Key |
| `MINIMAX_API_KEY` | MiniMax API Key（摘要生成） |

### 2. 自定义频道

编辑 `channels.json`，按格式增删频道：
```json
{"channel_id": "UCnUYZLuoy1rq1aVMwx4aTzw", "name": "Google DeepMind"}
```

### 3. 自定义用户画像

编辑 `profile.json`，调整筛选偏好：
- `preferred_channels`：常看频道（排序时优先）
- `exclude_title_patterns`：排除的标题关键词
- `deprioritize_topics`：降低优先级的话题

### 4. 手动触发测试

在 GitHub Actions 页面 → YouTube Topic Summary Daily → Run workflow

## 配合 Hermes 推送到微信（推荐）

1. 确保已安装 [Hermes Agent](https://hermes-agent.nousresearch.com) 并配置微信
2. 在 Hermes 中创建 cron 任务：
```
/cron create "0 10 * * *" "fetch daily digest from GitHub and send to me"
```
详见 Hermes cron 文档。

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `YOUTUBE_API_KEY` | 是 | - | YouTube Data API Key |
| `GEMINI_API_KEY` | 是 | - | Gemini API Key（智能排序） |
| `MINIMAX_API_KEY` | 是 | - | MiniMax API Key（摘要生成） |
| `MIN_DURATION_MINUTES` | 否 | `3` | 最短视频时长（过滤 Shorts） |
| `TOP_N` | 否 | `5` | 每日精选数量 |
| `LOOKBACK_HOURS` | 否 | `24` | 回溯时间窗口 |

## 本地运行

```bash
pip install -r requirements.txt

export YOUTUBE_API_KEY="your_key"
export GEMINI_API_KEY="your_key"
export MINIMAX_API_KEY="your_key"

python main.py
```

## License

MIT
