# 🤖 Cutting-Edge AI — Daily Digest Pipeline

> 每天自动从 Reddit、YouTube、RSS、微信公众号等多渠道采集 AI 前沿信息，经 LLM 评分、聚类、趋势分析后，生成媲美专业 Newsletter 的 Markdown + HTML 日报。

---

## ✨ 功能特性

- **多源采集**：Reddit、YouTube、RSS（100+ 订阅源）、微信公众号（23 个账号）、X/Twitter
- **智能过滤**：关键词预过滤 + 多维排名，从 2000+ 条中精选 200 个候选
- **LLM 并发评分**：10 线程并行调用 Qwen API，打新颖性 / 影响力 / 信号分，200 条约 25 分钟
- **主题聚类**：K-Means 自动归组，识别当日热点主题
- **趋势分析**：跨渠道共振信号提炼，附反向视角与证据链
- **深度报道**：Top 3 条目由 LLM 撰写完整深度报道（背景脉络 / 关键事实 / 影响分析）
- **多格式输出**：Markdown 日报 + HTML 邮件版 + 微信公众号排版文章

---

## 📁 项目结构

```
cutting-edgeAI/
├── src/
│   ├── main.py              # CLI 入口，编排完整流水线
│   ├── collectors/          # 各渠道采集器
│   │   ├── rss.py           # RSS/Atom（含微信公众号 RSS 桥）
│   │   ├── reddit.py        # Reddit PRAW + 匿名 JSON
│   │   ├── youtube.py       # YouTube Data API v3
│   │   └── twitter.py       # X/Twitter API v2
│   ├── prefilter.py         # 关键词预过滤
│   ├── ranker.py            # 多维预排名（来源权重 + 关键词密度）
│   ├── analyzer.py          # LLM 评分（并发，qwen3.6-plus）
│   ├── cluster.py           # K-Means 主题聚类
│   ├── insight_generator.py # 趋势洞察生成
│   ├── story_writer.py      # 深度报道撰写
│   ├── trend_expander.py    # 趋势深度扩写
│   ├── digest.py            # 日报编排与渲染
│   ├── email_renderer.py    # HTML 邮件版渲染
│   ├── wechat_publisher.py  # 微信公众号文章生成
│   ├── weekly_digest.py     # 周报 / 月报
│   └── db.py                # SQLite 存储（WAL 模式，支持并发写）
├── config.yaml              # 所有渠道、关键词、模型参数配置
├── .env.example             # 环境变量模板
├── requirements.txt         # Python 依赖
└── scripts/
    └── daily_run.sh         # 可选：每日自动运行脚本
```

---

## 🚀 快速开始

### 1. 环境准备

```bash
git clone https://github.com/Jovanqing/cutting-edgeAI.git
cd cutting-edgeAI

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置 API Key

```bash
cp .env.example .env
# 编辑 .env，至少填写 LLM_API_KEY
```

**必填：**

| 变量 | 说明 |
|------|------|
| `LLM_API_KEY` | DashScope API Key（[获取地址](https://dashscope.console.aliyun.com/)） |
| `LLM_BASE_URL` | 默认 `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` |

**可选（不填会降级到免费模式）：**

| 变量 | 说明 |
|------|------|
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | Reddit API，不填用匿名 JSON |
| `YOUTUBE_API_KEY` | YouTube Data API v3 |
| `TWITTER_BEARER_TOKEN` | X/Twitter API v2 |

### 3. 运行

```bash
# 完整流水线（采集 → 评分 → 聚类 → 生成日报）
python -m src.main run

# 仅采集，不调用 LLM
python -m src.main run --dry

# 分步执行
python -m src.main collect       # 采集
python -m src.main analyze       # LLM 评分
python -m src.main digest        # 生成今日日报

# 补生成指定日期的日报（基于已有 DB 数据）
python -m src.main date 2026-05-29

# 周报 / 月报
python -m src.main weekly
python -m src.main monthly
```

生成的日报保存在 `data/digests/YYYY-MM-DD.md` 和 `.html`。

---

## ⚙️ 配置说明

所有参数集中在 `config.yaml`，修改后无需重启，下次 `run` 自动生效。

### 添加新订阅源

```yaml
rss:
  feeds:
    # 普通 RSS
    - { name: "My Blog", url: "https://example.com/feed" }

    # 微信公众号（via wechat2rss.xlab.app）
    - { name: "WX/机器之心", url: "https://wechat2rss.xlab.app/feed/<id>.xml" }

    # YouTube 频道
    - { name: "YT/Yannic Kilcher", url: "https://www.youtube.com/feeds/videos.xml?channel_id=UCZHmQk67mSJgfCCTn7xBfew" }
```

### 调整评分模型与并发数

```yaml
analyzer:
  model: qwen3.6-plus   # 支持任何 OpenAI 兼容接口的模型
  workers: 10           # 并发线程数（DashScope 建议 5-10）
  batch: 100            # 每轮处理条目数
```

### 关键词过滤

在 `config.yaml` 的 `keywords` 列表中添加，支持中英文混合：

```yaml
keywords:
  - LLM
  - 大模型
  - skill evolution
  - 具身智能
```

---

## 🏗️ 流水线架构

```
采集层                过滤层              分析层               生成层
─────────           ──────────          ──────────           ──────────
Reddit              关键词预过滤         LLM 并发评分          主题聚类
YouTube       →     多维排名       →     (新颖/影响/信号)  →   趋势分析
RSS/微信            候选筛选             结构化字段提取         深度报道
Twitter                                                       日报渲染
                                                             (MD + HTML)
```

每条内容经 LLM 打三个维度分数：

| 维度 | 说明 |
|------|------|
| **Novelty (N)** | 信息新颖程度，有无突破性进展 |
| **Impact (I)** | 对 AI 领域的潜在影响力 |
| **Signal (S)** | 信噪比，内容质量与深度 |

综合分 = `0.5×N + 0.3×I + 0.2×S`，高于 `min_score`（默认 4）的条目进入日报。

---

## 📦 依赖

- Python 3.9+
- `openai` — LLM API 调用（OpenAI 兼容接口）
- `feedparser` — RSS/Atom 解析
- `praw` — Reddit API
- `google-api-python-client` — YouTube API
- `scikit-learn` — K-Means 聚类
- `rich` — 终端进度显示
- `python-dotenv` — 环境变量加载

完整依赖见 `requirements.txt`。

---

## 📄 License

MIT
