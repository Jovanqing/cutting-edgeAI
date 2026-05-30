# AI 灵感采集系统 · 技术文档

**最后更新**：2026-04-30

---

## 1. 项目概述

自动采集 AI 前沿内容 → LLM 打分筛选 → 生成每日灵感日报。

**当前状态**：第一版完整跑通，3 条采集途径可用，日报排版已升级为产品级 Markdown。

---

## 2. 目录结构

```
cutting_edgeAI/
├── .env                          # API 密钥（不提交）
├── .env.example                  # .env 模板
├── config.yaml                   # 采集与分析配置（可热更新）
├── requirements.txt              # Python 依赖
├── ARCHITECTURE.md               # 本文件
│
├── src/
│   ├── __init__.py
│   ├── main.py                   # CLI 入口（collect / analyze / digest / run）
│   ├── db.py                     # SQLite 存储层
│   ├── analyzer.py               # LLM 打分模块（OpenAI 兼容接口）
│   ├── digest.py                 # Markdown 日报生成
│   │
│   └── collectors/
│       ├── __init__.py
│       ├── base.py               # Collector 基类 + Item 数据类
│       ├── reddit.py             # Reddit 采集（PRAW API / 匿名 JSON 降级）
│       ├── youtube.py            # YouTube 采集（Data API v3，含评论）
│       ├── rss.py                # RSS/Atom Feed 采集
│       └── twitter.py            # X/Twitter 采集（需付费 API）
│
├── data/
│   ├── inspiration.db            # SQLite 数据库
│   └── digests/                  # 生成的日报 .md 文件
│       └── 2026-04-30.md
│
└── .venv/                        # Python 虚拟环境
```

---

## 3. 数据流

```
config.yaml ─┬─ reddit.py ──────┐
             ├─ youtube.py ─────┤
             ├─ rss.py ─────────┤──→ SQLite (items 表) ──→ analyzer.py ──→ digest.py ──→ .md 日报
             └─ twitter.py ─────┘                        (LLM 打分)      (筛选排版)
```

**CLI 命令**：

```bash
python -m src.main collect    # 采集所有源 → 写入 SQLite（去重）
python -m src.main analyze    # LLM 循环打分所有未处理项
python -m src.main digest     # 生成今日日报
python -m src.main run        # 一键：collect → analyze → digest
```

### 3.1 collect 阶段

- 4 个 Collector 并行执行（实际为顺序）
- 每个 Collector 的 `collect()` 返回 `Iterable[Item]`
- `db.upsert_item()` 以 `id` 为主键去重写入
- 已存在的 id 会被 `INSERT OR IGNORE` 跳过

### 3.2 analyze 阶段

- 从 `items` 表读取 `processed=0` 的行
- 每个 item 独立调一次 LLM API（OpenAI 兼容接口）
- 返回 JSON：`{relevance: int, summary: str, ideas: str}`
- 写入 `relevance`、`summary`、`ideas` 字段，标记 `processed=1`
- **循环执行直到全部处理完**，每轮取 `batch` 条

### 3.3 digest 阶段

- 查询 `relevance >= min_score` 且 `fetched_at` 在指定时间窗口内的行
- 按 relevance 降序排列，分两层：
  - 突破性洞察（9-10 分）
  - 值得关注（7-8 分）
- 输出为 `data/digests/YYYY-MM-DD.md`

---

## 4. 核心模块

### 4.1 base.py — 数据模型

```
Item 数据类字段：
  id           str   # 唯一标识，格式 "{source}:{external_id}"
  source       str   # reddit | youtube | rss | twitter
  sub_source   str   # 子来源（subreddit名 / 频道名 / feed名）
  title        str
  author       str
  url          str
  content      str   # 正文/摘要
  comments_blob str  # JSON 字符串，存储评论列表
  published_at str   # 原始发布时间
  score        int   # 原始热度分（upvotes / likes）
```

### 4.2 db.py — 存储层

| 函数 | 说明 |
|---|---|
| `init()` | 建表 + 索引 |
| `upsert_item(item: dict)` | 插入或忽略（id 去重） |
| `get_unprocessed(limit)` | 轮询采样未分析项（按 source+sub_source 分层随机） |
| `update_analysis(id, relevance, summary, ideas)` | 回写分析结果 |
| `top_recent(min_score, hours, limit)` | 按 relevance 降序取高分项 |

**关键设计**：`get_unprocessed` 使用 `ROW_NUMBER() OVER (PARTITION BY source, sub_source)` 做分层采样，避免单一活跃 feed 独占 batch。

### 4.3 analyzer.py — LLM 打分

| 配置项 | 说明 |
|---|---|
| `model` | 模型名，默认 `qwen-max-latest` |
| `min_score` | 日报最低分阈值，默认 7 |
| `batch` | 每轮处理条数，默认 50 |

- 使用 OpenAI 兼容接口（默认指向 DashScope / Qwen）
- System prompt 强调新颖性 + 深度，抑制 hype/buzzword
- Prompt 中注入 top 15 评论（截断到 400 字/条）
- 输出严格 JSON，带正则容错解析
- `ideas` 字段支持 list / JSON array / 纯文本三种格式归一化

**如需切换模型**：
1. 改 `.env` 中的 `LLM_BASE_URL` 和 `LLM_API_KEY`
2. 改 `config.yaml` 中的 `analyzer.model`

### 4.4 digest.py — 日报生成

| 参数 | 默认值 | 说明 |
|---|---|---|
| `min_score` | 7 | 入选最低分 |
| `hours` | 24 | 时间窗口 |
| `limit` | 50 | 最多条目数 |
| `total_collected` | 0 | 采集总量（概览面板用） |
| `total_analyzed` | 0 | 分析总量（概览面板用） |

新版日报结构：
1. **标题行**：日期 + 星期 + 精选数
2. **今日概览**：采集量、分析量、平均分、分布等统计表
3. **突破性洞察**：9-10 分内容，带引用块摘要 + 可探索方向
4. **值得关注**：7-8 分内容

每条目包含：标题（可点击）、来源标注、自动标签 (tag)、摘要引用、可探索方向列表、分数 badge。

---

## 5. 采集途径详情

### 5.1 Reddit

| 项目 | 说明 |
|---|---|
| 认证方式 | PRAW（需 client_id/secret）或匿名 JSON 降级 |
| 操作 | 拉取指定 subreddit 的 top posts |
| 评论 | API 模式可取 top 10 评论；匿名模式无评论 |
| 限速 | 匿名模式下较严格，建议间隔 ≥2s |

**当前配置**：8 个 subreddit，每 sub 25 条，时间窗口 `day`

### 5.2 YouTube

| 项目 | 说明 |
|---|---|
| 认证方式 | API Key（YouTube Data API v3） |
| 免费额度 | 每天 10,000 units |
| 频道跟踪 | `channels` 列表中的频道按时间排序抓视频 |
| 关键词搜索 | `search_queries` 列表按相关性抓视频 |
| 评论 | 每视频取 top N 条评论（`fetch_top_comments`） |
| 消耗估算 | ~200 units/轮（15 视频 + 25 评论/视频） |

**当前配置**：19 个 AI 频道 + 4 个搜索关键词

### 5.3 RSS

| 项目 | 说明 |
|---|---|
| 认证方式 | 无需认证 |
| 支持格式 | RSS 2.0 / Atom |
| 覆盖源 | 11 个博客 + 5 个 YouTube Atom feed + 3 个 arXiv |
| 去重方式 | SHA1(id or link or title) 前 16 位 |

**当前问题**：arXiv 3 个 feed（cs.AI, cs.CL, cs.LG）每天产生 ~2,500 条，远超其他源总和。

### 5.4 Twitter/X

| 项目 | 说明 |
|---|---|
| 认证方式 | Bearer Token（需付费 API，$200/月 Basic 档） |
| 当前状态 | **已禁用**（`twitter.enabled: false`） |

---

## 6. 数据库 Schema

```sql
CREATE TABLE items (
    id            TEXT PRIMARY KEY,        -- "{source}:{external_id}"
    source        TEXT NOT NULL,           -- reddit|youtube|rss|twitter
    sub_source    TEXT,                    -- subreddit/频道/feed 名
    title         TEXT,
    author        TEXT,
    url           TEXT,
    content       TEXT,                    -- 正文/描述
    comments_blob TEXT,                    -- JSON 评论数组
    published_at  TEXT,                    -- 原始发布时间
    fetched_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    score         INTEGER DEFAULT 0,       -- 原始热度（votes/likes）
    relevance     INTEGER,                 -- LLM 打分 (1-10)
    summary       TEXT,                    -- LLM 生成的中文摘要
    ideas         TEXT,                    -- LLM 提炼的可探索方向
    processed     INTEGER DEFAULT 0        -- 0=未分析 1=已分析
);

-- 索引
CREATE INDEX idx_processed  ON items(processed);
CREATE INDEX idx_relevance  ON items(relevance);
CREATE INDEX idx_fetched_at ON items(fetched_at);
```

---

## 7. 配置参考

```yaml
# config.yaml 完整结构
keywords: [LLM, GPT, Claude, agent, RAG, ...]

reddit:
  subreddits: [LocalLLaMA, MachineLearning, ...]
  limit_per_sub: 25
  time_filter: day

youtube:
  channels: [UC..., ...]            # channelId 列表
  search_queries: ["AI agents 2026", ...]
  max_results: 15
  fetch_top_comments: 25

rss:
  feeds:
    - { name: "显示名", url: "https://..." }

twitter:
  enabled: false
  users: [sama, karpathy, ...]
  query: "(AI OR LLM OR agent) lang:en"
  max_results: 50

analyzer:
  model: qwen-max-latest
  min_score: 7
  batch: 100
```

```bash
# .env 环境变量
LLM_API_KEY=          # 必填：LLM API Key
LLM_BASE_URL=         # 选填：默认 DashScope
REDDIT_CLIENT_ID=     # 选填：Reddit API
REDDIT_CLIENT_SECRET= # 选填
REDDIT_USER_AGENT=    # 选填
YOUTUBE_API_KEY=      # 选填：YouTube Data API v3
TWITTER_BEARER_TOKEN= # 选填：需付费 API
```

---

## 8. 依赖清单

```
openai>=1.40                   # LLM API 调用（OpenAI 兼容）
google-api-python-client>=2.100 # YouTube Data API
feedparser>=6.0                # RSS/Atom 解析
PyYAML>=6.0                    # 配置解析
python-dotenv>=1.0             # .env 加载
requests>=2.31                 # HTTP 请求
rich>=13.0                     # CLI 美化输出
praw>=7.7                      # Reddit API（可选）
tweepy>=4.14                   # Twitter API（可选）
```

---

## 9. 已知问题 & 改进方向

### 9.1 当前问题

| 问题 | 严重程度 | 说明 |
|---|---|---|
| RSS 数据量过大 | 高 | arXiv 3 个 feed 产出 ~2,500 条/天，占总量 88%，稀释 Reddit/YouTube 的优质讨论 |
| 全量分析耗时长 | 中 | 2837 条全部分析需 ~2.5 小时，需要增量策略 |
| Reddit 无评论 | 低 | 未配置 API key，匿名 JSON 模式无法获取评论 |
| Python 3.9 已 EOL | 低 | Google 库已停止为该版本提供完整更新 |
| Twitter 不可用 | 低 | 需 $200/月付费 API |

### 9.2 改进方向

1. **RSS 源裁剪**：arXiv 只留 `cs.AI`，或按关键词预过滤后再入库
2. **打分策略分层**：先快速筛选（标题关键词/热度阈值），只对候选集深度打分
3. **日报主题聚类**：代替按 source 分组，用 LLM 或关键词将条目聚类为 3-5 个主题
4. **增量运行**：cron 定时跑 `run`，只处理最近 24h 的新内容
5. **Reddit API 填上**：继续尝试 Reddit App 注册，或使用旧版页面
6. **Python 升级**：升级到 3.12+，解决依赖库警告

---

## 10. 快速开始

```bash
# 1. 安装依赖
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. 配置环境
cp .env.example .env
# 编辑 .env，填入 LLM_API_KEY 和 YOUTUBE_API_KEY

# 3. 编辑 config.yaml 调整采集源和分析参数

# 4. 运行
python -m src.main run
```
