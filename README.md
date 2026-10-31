# 智扫通机器人智能客服

基于 LangChain `create_agent`、LangGraph 的扫地/扫拖机器人客服。后端 FastAPI，前端 Vue 3，包含知识库问答、多轮记忆、工具调用、回复点赞，以及独立管理控制台。

已移除设备引导排障、报告生成/导出和人工转交功能。管理员登录后进入独立界面，8 小时内刷新可恢复登录。

架构取舍、API 清单与实测数据见 [FastAPI 重构说明](docs/FastAPI重构说明.md)。

## 启动

在 Python 3.10.20 环境验证（本机 `D:\anaconda3\envs\time`）。首次需装依赖并构建前端：

```powershell
python -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }

cd web
npm install
npm run build
cd ..

.\start.ps1
```

打开 <http://127.0.0.1:8000>，接口文档在 `/docs`。

`start.ps1` 支持 `-Python`、`-Port`，缺前端产物会自动构建。Bash 下用 `./start.sh`。

开发模式开两个终端（`/api` 已在 vite 里代理到 8000，cookie 同源生效）：

```powershell
python -m server --port 8000 --reload
cd web; npm run dev        # http://localhost:5173
```

编辑 `.env`，配置以下变量：

| 变量 | 用途 |
|---|---|
| `DASHSCOPE_API_KEY` | 必填，DashScope API Key |
| `DASHSCOPE_BASE_URL` | 对话和 embedding 的兼容接口地址 |
| `APP_PASSWORD` | 可选，用户端共享访问密码；留空进入本地演示模式 |
| `ADMIN_PASSWORD` | 启用管理员功能时必填；留空会显示"管理员登录未启用" |
| `TAVILY_API_KEY` | 可选，天气工具的联网搜索来源 |

`.env` 固定从项目根读取，在访问密码判断前加载；已导出的环境变量优先。所有数据路径按项目根解析。

服务启动时在后台线程完成 Agent 装配与知识库检查，uvicorn 立刻开始接收请求，页面会显示初始化进度。首次进入不需要额外等待，冷启动实测 1.4–1.8 秒。

`--workers` 必须为 1：SqliteSaver、向量库连接、取消注册表都是进程内单例，多 worker 会导致会话历史写入竞争、停止信号打不到正确的进程。

没有 `APP_PASSWORD` 时进入明确提示的本地演示模式。共享密码与演示用户下拉框不是正式账号体系；部署为多用户服务时，应从真实登录态注入身份并移除演示身份选择框。

## 目录

| 路径 | 用途 |
|---|---|
| `server/` | FastAPI 后端：路由、SSE 传输、鉴权、生命周期 |
| `web/` | Vue 3 + Vite 前端 |
| `agent/react_agent.py` | Agent 装配、事件流、身份校验、持久化历史 |
| `agent/tools/agent_tools.py` | 当前注册工具：知识检索、联网天气（Tavily）、位置询问、用户 ID |
| `agent/tools/middleware.py` | 并发工具预算、模型预算、上下文裁剪、动态提示词 |
| `rag/vector_store.py` | 切分、向量检索、增量构建与原子发布 |
| `rag/reranker.py` | 重排、超时与失败降级 |
| `rag/rag_service.py` | 摘要/原文两种检索返回路径 |
| `utils/admin_auth.py` | 管理员令牌签发与校验 |
| `utils/model_settings.py` | 模型配置更新、嵌入模型重建及失败恢复 |
| `utils/knowledge_admin.py` | 资料发布的事务层（备份、原子写、回滚） |
| `eval/` | 检索基线、Agent 最终拒答及成本评估 |
| `tests/` | 离线测试，使用模拟模型和临时数据库 |

`get_current_month`、`fetch_external_data`、`enter_report_mode` 及相关配置仍留在源码中，但未注册到当前 Agent，不提供月份查询、个人使用记录取数或报告生成能力。

## 身份与会话

- 用户身份由服务端签发的签名 cookie 决定，请求体里的 `user_id` 一律不采信；切换演示用户也由服务端重签。
- 当前会话 ID 存在 URL 的 `chat` 参数里，刷新后恢复；不保存聊天正文、密码或管理员令牌。
- 会话列表通过 SQL 先筛选 ID，每个会话只读取最新检查点。默认显示 20 个，但"清理其他会话"处理该身份的全部会话。
- 公共会话读、写、删入口检查归属：只接受 `user-{ID}` 或 `user-{ID}-...`；ID 为数字字符串，避免前缀碰撞。
- 删除需要界面二次确认。`checkpoints.sqlite` 保存完整消息，模型调用时裁剪上下文不会删除历史。

编程调用：

```python
from utils.env import ensure_env_loaded

ensure_env_loaded()

from agent.react_agent import ReactAgent
from rag.vector_store import VectorStoreService
from utils.model_settings import configuration_guard, refresh_model_settings

with configuration_guard():
    refresh_model_settings()
    VectorStoreService().load_document()
    agent = ReactAgent()

answer = agent.answer("滤网怎么维护？", thread_id="user-1001", user_id="1001")
print(answer)
history = agent.load_history("user-1001", "1001")
```

默认 `default` 会话不再允许匿名调用。自定义检查点通过 `ReactAgent(checkpointer=...)` 注入。

## 响应与流式

一轮问答通过 SSE 推送，事件与 `ReactAgent.stream_events` 一一对应：

| 事件 | 含义 |
|---|---|
| `phase` | 当前阶段（理解问题、检索资料、生成回复、核对答复） |
| `content` | 模型输出的文本增量 |
| `tool_start` / `tool_end` | 工具开始/结束，带工具名 |
| `sources` | 本次检索引用的资料 |
| `final` | 最终答复全文 |

本机实测（"你好"）：答复首字 2.1 秒，完整回复 3.0 秒。**瓶颈是模型链路**——首次决策、检索、rerank、核对四段网络往返，换前端框架不影响这部分。

`config/agent.yml` 的 `decision_disable_thinking: true` 默认关闭首次工具决策和闲聊的思考；`answer_disable_thinking` 独立控制拿到工具结果后的模型调用。模型不支持 `enable_thinking` 时，将相关开关及 `config/model.yml` 的 `grounding_disable_thinking` 设为 `false`。

交互聊天使用独立的 `chat_request_timeout: 45`、`chat_max_retries: 0`。停止生成是协作式的：已发出的网络请求仍需等待本次请求返回或超时。

## 数据脱敏

`utils/redact.py` 是所有存储出口的统一过滤层：识别手机号、18 位身份证、16–19 位银行卡、邮箱、订单号（关键词锚定）和带前缀的详细地址，替换为 `138****5678` 这类掩码。

- 日志：`utils/logger_handler.py` 的 `RedactingFormatter` 在 `format()` 末尾调用，`msg`、格式化参数和异常栈一起覆盖（挂在 Formatter 而不是 Filter 上，就是为了拦住 `logger.exception` 的 traceback）。`server` 命名空间的日志复用同一批 handler。
- 反馈：`save_feedback` 在写 `feedback.sqlite` 前处理 `question` / `answer` / `reason`。
- 只作用于存储与日志，**不改**送模型的输入和返回给用户的回答。
- `checkpoints.sqlite` 目前仍保存完整消息，尚未接入脱敏。
- 已知边界：没有关键词前缀的自由文本地址、图片/PDF 内文字不覆盖；上游 `content[:200]` 截断可能把号码切成不足 11 位而漏判。

## 知识库构建与恢复

启动时按文件内容哈希、切分和 embedding 配置决定直接加载、增量更新或全量重建。

1. 构建进程获取跨进程文件锁。
2. 在独立的新 Chroma 集合中构建。增量更新直接复制未变化文件的 embedding，仅重新计算新增/修改文件。
3. 块 ID 根据来源、位置和正文确定。检查实际文件/块计数，并确认源文件在构建期间未变化。
4. 新集合完整后，原子替换 `kb_meta.json`，发布 `active_collection`。
5. 失败时不发布，新启动仍能读取旧集合。

旧集合暂时保留，以免破坏正在执行的查询；因此更新会增加磁盘占用，需要在停服维护时按元数据确认活动集合后清理历史集合。不要直接删除正在运行的 Chroma 段目录。切分规则变化仍需要更新 `SPLITTER_VERSION`。

## 检索与引用

默认：向量粗召回 → 可选距离过滤 → rerank → 取前 k 条原文 → 受约束生成与引用校验 → 最终答复。

`config/chroma.yml` 控制 `k`、`rerank_fetch_k`、重排模型与超时。`max_distance` 默认关闭。重排失败降级为向量顺序，日志记录是否实际重排；侧栏显示索引统计和配置中的模型名称，不代表远端服务实时健康。

`config/agent.yml` 的 `rag_raw_documents: true` 开启原文路径：普通问答直接把检索原文交给 Agent，减少一次摘要调用；设为 `false` 则先生成检索摘要。

引用按块 ID 去重，保留完整原文和 PDF 页码；历史重新加载后仍显示资料。模型被要求保留 `[数字]` 短引用，但界面列出的仍是检索参考资料，不能仅凭列表断言每条结论都得到资料支持。

天气工具通过 Tavily 联网搜索，返回网页摘要、来源链接和"可能滞后"的提示。Tavily 的 `include_answer` 实测会把抓到的旧页面复述成"今天"，因此已关闭，只给原始摘要。

位置工具没有定位能力：`get_user_location` 只会返回 `[需要用户城市]` 提示，由模型在回答中询问用户所在城市。`get_weather` 会拒绝"这里""当前"这类非城市名入参。

## Agent 预算

| 配置 | 默认值 | 行为 |
|---|---:|---|
| `max_model_calls` | 12 | 每轮外层模型调用上限，最后一次移除工具以生成答复 |
| `turn_timeout` | 180 秒 | 调用边界检查时间预算，超出后生成可持久化的结束提示 |
| `context_max_chars` | 24000 | 按完整回合裁剪旧消息，保持当前工具消息配对 |

工具总预算为 10 次，其中检索最多 4 次，定义在 `agent/runtime.py`。预算检查与预留在锁内完成，避免并发超发。

时间预算是协作式检查，不能强制取消已经运行的工具。外层模型日志记录耗时和 token 信息；端到端评估通过回调统计 Agent 链路 token。

## 验证与评估

离线回归：

```bash
python -m unittest discover -s tests -q
```

当前 215 项。覆盖真实 LangGraph 工具身份注入、工具 schema、并发预算、停止收口、非 token 流答复、历史引用、跨身份隔离、20 个以上会话清理、Chroma 构建失败恢复、管理员令牌过期与撤销、日志脱敏等。

> 已知偶发：`IndexTests` 里 Chroma 的临时目录读写偶尔报 `Nothing found on disk`，重跑即过，与业务逻辑无关。

检索质量需要在线调用模型：

```bash
python -m eval.retrieval_eval --set=all --judge --refusals
python -m eval.retrieval_eval --set=all --judge --refusals --snapshot
python -m eval.retrieval_eval --set=all --judge --refusals --check-baseline
```

指纹包含语料、提示词和题集哈希，以及检索配置。旧基线没有新字段时报告"未验证"，必须重新评估。

评估完整 Agent 最终回答、延迟和 token：

```bash
python -m eval.agent_eval --mode summary --output eval/agent-summary.json
python -m eval.agent_eval --mode raw --output eval/agent-raw.json
```

## 配置与限制

- `config/model.yml`：模型、单次请求超时、重试参数。
- `config/chroma.yml`：知识库、切分、重排与检索参数。
- `config/agent.yml`：Agent 预算、原文/摘要路径开关。
- `config/prompts.yml`：提示词文件路径。

### 角色模型（可选的提速手段）

一次带检索的问答只有两次模型调用：**首次工具决策** → **证据核对与受约束生成**（有资料时 `bound_model` 会短路掉草稿生成）。`config/model.yml` 允许把前者交给更快的模型：

```yaml
decision_model_name: "qwen3.7-flash"   # 首次工具选择
grounding_model_name: ""               # 留空 = 跟随 chat_model_name
```

2026-10-31 实测（`tools/bench.py`，数据在 `eval/bench-*.json`）：

| 配置 | 闲聊首字 | HEPA 首字 | 吸力首字 |
|---|---:|---:|---:|
| 全量 `qwen3.8-max` | 1.61s | 0.83s | 1.23s |
| decision 换 flash | **0.77s** | **0.50s** | **0.66s** |

`decision` 换小模型约快 40–50%，三次都正确触发了检索。**`grounding` 不建议换**：同一问题输出从 195 字涨到 1342 字，"200 字内"的约束被无视，反而更慢。

两个键留空即恢复改动前行为。测试会忽略这两个配置（见 `tests/__init__.py`），避免假模型注入失效。

- SQLite 备份应使用 SQLite backup API，不能在运行期间只复制主文件而忽略 WAL。
- 上游 LangChain/Pydantic 序列化警告仍可能出现；升级依赖后应重跑测试。
