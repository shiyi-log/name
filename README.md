# 名字研究所

这是一个本地运行的二字、三字、四字公司名筛选工具。它保存完整的 8,105 个通用规范汉字，并持续将规则组合、历史候选和古诗词原句片段评分后追加到 SQLite 数据库。前端通过本地 API 分页查询，默认按综合评分降序展示候选，并显示评分理由。选择字库中的汉字后，还可以通过可选的 AI 服务生成、评分并保存候选。

项目仓库：[shiyi-log/name](https://github.com/shiyi-log/name)。当前版本：`0.1.0`。

## 启动

推荐 Python 3.12，要求 Python 3.10 或更新版本。在项目目录建立虚拟环境并启动：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python api_server.py
```

然后打开：[http://127.0.0.1:4173/](http://127.0.0.1:4173/)。服务默认只监听本机；可用 `--port 4174` 调整端口。关闭服务时按 `Ctrl+C`。

页面依赖 `/api/` 接口，必须通过 `api_server.py` 启动。首次启动会自动迁移旧版数据库结构并保留已有候选与核验状态。

### 可选 AI 生成

普通检索、筛选、排序和导出无需配置 AI。要启用「字库 → 选择汉字 → 生成并保存」，服务启动时会只读导入本机 CC Switch 数据库（默认路径 `~/.cc-switch/cc-switch.db`）中带有 `OPENAI_API_KEY` 的 Codex/OpenAI-compatible provider，并在页面中提供服务商选择。官方 Codex OAuth 登录不会被当作 API key 使用。也可以用环境变量提供一个兼容服务：`OPENAI_API_KEY`、可选的 `OPENAI_BASE_URL`（默认为 `https://api.openai.com/v1`）、`OPENAI_MODEL`（默认为 `gpt-4o-mini`）和 `OPENAI_WIRE_API`（`chat_completions` 或 `responses`）。

密钥只在本地 Python 服务进程内读取，provider 列表和浏览器页面不会返回密钥，也不会写入项目数据库。CC Switch 的 `wire_api = "responses"` 会调用对应的 `/responses` 端点；Chat Completions provider 则调用 `/chat/completions`。生成会调用所选模型服务，并按服务商规则计费。没有可用 provider 时，页面会禁用生成按钮；其他功能仍可使用。模型返回的候选会经过长度、字库级别、排除名单和重名过滤，再评分保存，实际新增数可能少于请求数。

如果 CC Switch 使用非默认数据目录，可设置 `CC_SWITCH_DB_PATH=/path/to/cc-switch.db`；也可用 `CC_SWITCH_PROVIDER_ID` 指定启动时默认的 provider。服务只读 CC Switch SQLite，不会改写其配置；请不要把 CC Switch 数据库、导出配置或 API key 提交到 Git。

## 数据与评分

- 完整字库：`data/characters-8105.json`
- SQLite 主数据库：`data/names.db`
- 发布时的候选快照：`data/candidates.json`
- 旧版诗词导入进度快照：`data/generation_state.json`
- 数据库构建脚本：`build_database.py`
- 评分逻辑：`scoring.py`
- 本地 API 与生成服务：`api_server.py`
- 前端交互：`app.js`

候选数由词元、历史候选、已发现诗词文件、字级过滤、排除名单和动态评分阈值共同决定，不设固定目标数或上限。阈值按首字分组取分位数，避免候选多的开头挤掉候选少的开头；列表仍按全库综合分排序。规则组合和诗词片段只使用一级常用字和二级规范字；三级规范字保留在完整字库中供检索。每次运行都会从上游动态发现尚未处理或已更新的诗词文件，按批次写入候选并输出新增数、来源计数和首字覆盖数。

四字规则组合会做镜像去重：同一对词元的正反两种排列只保留分高的那个（去掉「云开入云 / 入云云开」这类重复）；完全同名的候选只保留一条。二字候选来自基础词元，三字候选来自共享一字的词元连接。历史候选与诗词原句片段保留来源并单独标记。诗词候选按二至四字连续片段提取，需结合原句语境人工确认。已有候选保留，镜像去重只约束新入库的规则组合。

诗词源使用 [chinese-poetry/chinese-poetry](https://github.com/chinese-poetry/chinese-poetry) 的《全唐诗》和《全宋诗》数据，上游仓库采用 MIT 许可。SQLite 的 `poetry_sources` 表按文件路径和内容 SHA 保存处理进度，首次迁移会读取旧版 `data/generation_state.json`；分片总数不写死，`--poetry-shards` 只控制每次处理的批量大小。在已激活的虚拟环境中，运行 `python build_database.py --poetry-shards 5` 可加快导入；运行 `python build_database.py --watch` 会每 300 秒检查上游新增文件并自动处理，`--interval-seconds` 可调整检查间隔。`--no-grow` 可在不读取新来源时生成本地规则候选、重评分并更新数据库。SQLite 以名称为唯一键追加新候选、更新分数，不删除已有记录；候选 ID 和核验状态在更新时保留。

诗词档案本身是有限语料，因此无法在没有新来源时凭空持续增加独特且有出处的候选。当前机制不设候选数量上限；已有文件会分批导入，上游日后新增或更新文件时，单次运行或 `--watch` 会继续发现并加入新候选。

`8105^4 = 4,315,310,765,550,625`，约 4,315 万亿个四字组合。这个数量不适合逐条物化到本地 SQLite：仅按每条 100 字节估算就超过 400 PB。数据库因此采用两层表示：`characters` 表完整保存 8,105 个字，`combination_space` 表精确保存各字库层级的四字组合数量，`name_candidates` 表累计保存规则组合、历史候选、诗词来源和 AI 候选。

当前评分满分 100，保留一位小数：

| 维度 | 权重 |
| --- | ---: |
| 品牌感 | 28% |
| 读写与韵律 | 20% |
| 诗意与意象 | 20% |
| 独特性 | 18% |
| 注册启发分 | 14% |

评分逻辑在 `scoring.py`，构建脚本只负责调用它。每个维度都由多个连续信号加权得到，而不是按词元类别查表：

- **品牌感**：首词元的动作强度、结尾是否留白、词对稀有度、是否含行业套话。
- **读写与韵律**：真实拼音参与计算，包括四声起伏、平仄交替、连续同调、同音字、声母韵母重复、常用字占比、四字内重字。
- **诗意与意象**：意象词数量、意象字密度、季节与时辰、是否命中成词表、抽象词堆叠。
- **独特性**：字组二元组的稀有度分位、单字稀有度、用字是否全在高频区间。
- **注册启发分**：用字在 8105 字表中的频率秩、是否整词命中常见成语、是否含常见行业词。

分数区间会随候选池变化，因此前端的「优先研究 / 值得保留」分档按当前实际分布取分位（前 10% 与前 35%），不写死 90 分。

“注册启发分”只是根据用字常见度做的初筛，不代表工商登记或商标检索已经通过。正式确定名称前，仍需检查企业字号、商标、域名和社交账号。

## 增加候选

调整 `data/tokens.json`、历史候选来源、评分规则或生僻字筛选边界后，重新运行构建脚本；正常运行会检查并处理尚未入库的诗词文件：

```bash
python build_database.py
```

脚本会更新 `data/names.db`；刷新页面后，前端通过 API 读取最新数据。JSON 文件为发布快照，不会在每次生成时自动更新。升级前请备份自己的 SQLite 数据库；如服务仍在运行，应使用 SQLite 备份功能以包含 WAL 中的数据。

## 本地 API

| 接口 | 用途 |
| --- | --- |
| `GET /api/stats` | 候选数、字库数、长度分布和动态评分分档 |
| `GET /api/candidates` | 候选分页；支持 `length`、`query`、`direction`、`minimum_score`、`sort`、`initial`、`character`、`limit`、`offset` |
| `GET /api/characters` | 字库分页；支持 `query`、`tier`、`limit`、`offset` |
| `GET /api/ai/status` | 当前 AI provider 是否可用、模型和协议 |
| `GET /api/ai/providers` | 返回从 CC Switch 导入的 provider 名称、地址、模型和协议（不含密钥） |
| `POST /api/generate/by-character` | 生成并保存候选；JSON 请求体如 `{"character":"云","count":5,"lengths":[2,3,4],"providerId":"..."}` |

列表的「导出当前页」保存当前查询页的 JSON，最多 60 条。

## 验证

在安装依赖后运行：

```bash
python -m unittest discover -s tests -v
node --check app.js
```

Python 测试使用临时数据库和模拟的 AI 响应，不调用真实模型服务。JavaScript 语法检查需要 Node.js 22；日常运行无需 Node.js。
