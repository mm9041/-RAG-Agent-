# 智扫通机器人智能客服

基于 LangChain `create_agent`、LangGraph、Streamlit 的扫地/扫拖机器人客服，包含知识库问答、多轮记忆、工具调用、回复点赞，以及独立管理员控制台。

已移除设备引导排障、报告生成/导出和人工转交功能。管理员登录后进入独立界面，8 小时内刷新可恢复登录。

最新功能与操作说明见 [功能与测试说明](功能与测试说明.md)。

## 启动

已在 Python 3.10.20 环境验证。建议在独立虚拟环境中安装 `requirements.txt` 锁定的依赖。
以下为 Windows PowerShell 命令，从 `RAG与agent开发` 目录执行；如果已在 `Agent项目` 目录，跳过第一行。本文所称“项目根”及后续命令的工作目录均为 `Agent项目`。

```powershell
cd "Agent项目"
python -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

编辑项目根的 `.env`，配置以下变量；已有 `.env` 时直接编辑，不要用示例文件覆盖：

| 变量 | 用途 |
|---|---|
| `DASHSCOPE_API_KEY` | 必填，DashScope API Key |
| `DASHSCOPE_BASE_URL` | 对话和 embedding 的兼容接口地址，示例为 `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `APP_PASSWORD` | 可选，用户端共享访问密码；留空进入本地演示模式 |
| `ADMIN_PASSWORD` | 启用管理员功能时必填；留空会显示“管理员登录未启用” |

保存配置后启动：

```powershell
python -m streamlit run app.py
```

在 Bash 中进入项目目录后，同样使用上述 `python -m ...` 命令；首次创建 `.env` 时用 `cp .env.example .env`，已有文件则跳过复制。

`.env` 固定从项目根读取，在访问密码判断前加载；已导出的环境变量优先。
所有数据路径按项目根解析。首次进入用户端时会调用 embedding 建库，后续新页面会话检查文件变化。

启动时先显示标题和登录框，登录通过后才加载 Agent/RAG 重依赖，并显示初始化进度。
同一服务进程中复用 Agent 和向量库连接；每个新页面会话仍检查知识文件变化。
用户 ID、会话 ID 和每轮预算不进入共享资源缓存。模型名称支持运行时更新，具体操作见下方“管理员与模型配置”；修改 `.env`、提示词路径、Agent 预算、切分参数、超时等其他配置后需重启服务。
首次登录后的组件加载仍有冷启动成本，缓存不会消除进程重启后的依赖导入。

没有 `APP_PASSWORD` 时进入明确提示的本地演示模式。共享密码与演示用户下拉框不是正式账号体系；部署为多用户服务时，应从真实登录态注入身份并移除演示身份选择框。

## 管理员与模型配置

设置 `ADMIN_PASSWORD` 并启动服务后，在用户端侧栏“登录用户”下展开“管理员登录”，输入管理员密码进入独立控制台。如果设置了 `APP_PASSWORD`，首次进入用户端时还需先通过访问密码验证。

管理员可管理知识文件、查看反馈与点赞，并在“概览 → 模型配置”中修改模型 ID：

- 聊天、重排模型：点击“保存并应用模型”后用于下一次问答，无需重启；正在运行的问答先完成。
- 嵌入模型：需要勾选重建确认，再点击“保存并应用模型”。系统使用新 embedding 重建知识库，成功后切换；构建失败时恢复原配置和活动集合。
- 模型名称保存到 `config/model.yml` 和 `config/chroma.yml`。名称须与所配置服务兼容，聊天与重排服务是否可用仍以实际调用为准。

管理员登录令牌有效期为 8 小时；有效期内刷新或服务重启可恢复登录。点击“退出管理员”会撤销当前令牌。令牌过期、修改管理员密码并重启服务，或清除浏览器 Cookie 后需重新登录。

## 目录

| 路径 | 用途 |
|---|---|
| `app.py` | 登录、会话管理、实时答复和引用展示 |
| `agent/react_agent.py` | Agent 装配、事件流、身份校验、持久化历史 |
| `agent/tools/agent_tools.py` | 当前注册工具：知识检索、模拟天气、模拟位置、用户 ID |
| `agent/tools/middleware.py` | 并发工具预算、模型预算、上下文裁剪、动态提示词 |
| `utils/service_ui.py` | 管理员入口、控制台、模型配置界面与点赞 |
| `utils/model_settings.py` | 模型配置更新、嵌入模型重建及失败恢复 |
| `utils/admin_auth.py` | 管理员令牌签发、恢复与撤销 |
| `rag/vector_store.py` | 切分、向量检索、增量构建与原子发布 |
| `rag/reranker.py` | 重排、超时与失败降级 |
| `rag/rag_service.py` | 摘要/原文两种检索返回路径 |
| `eval/` | 检索基线、Agent 最终拒答及成本评估 |
| `tests/` | 离线测试，使用模拟模型和临时数据库 |

`get_current_month`、`fetch_external_data`、`enter_report_mode` 及相关配置仍留在源码中，但未注册到当前 Agent，不提供月份查询、个人使用记录取数或报告生成能力。

## 身份与会话

- 用户 ID 由调用方通过 `user_id` 传入，再经 `ToolRuntime.context` 注入工具；当前 `get_user_id` 工具无需模型传入身份，模型 schema 不包含 `user_id` 或 `runtime`。
- 公共会话读、写、删入口检查归属：只接受 `user-{ID}` 或 `user-{ID}-...`；ID 为数字字符串，避免前缀碰撞。
- 会话列表通过 SQL 先筛选 ID，每个会话只读取最新检查点。默认显示 20 个，但“清理其他会话”处理该身份的全部会话。
- 删除仍需要界面二次确认。`checkpoints.sqlite` 保存完整消息，模型调用时裁剪上下文不会删除历史。

编程调用：完成依赖安装和 `.env` 配置后，将以下代码保存为项目根的 Python 脚本并运行。示例会先检查并按需构建知识库，再调用在线模型；对话历史写入默认 `checkpoints.sqlite`。

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
print(history)
```

默认 `default` 会话不再允许匿名调用。自定义检查点通过 `ReactAgent(checkpointer=...)` 注入，历史列表和删除使用同一实例。

## 知识库构建与恢复

启动时按文件内容哈希、切分和 embedding 配置决定直接加载、增量更新或全量重建。

1. 构建进程获取跨进程文件锁。
2. 在独立的新 Chroma 集合中构建。增量更新直接复制未变化文件的 embedding，仅重新计算新增/修改文件。
3. 块 ID 根据来源、位置和正文确定。检查实际文件/块计数，并确认源文件在构建期间未变化。
4. 新集合完整后，原子替换 `kb_meta.json`，发布 `active_collection`。
5. 失败时不发布，新启动仍能读取旧集合；检索服务会在后续查询前刷新已发布集合。

不再先删除当前可用集合。旧集合暂时保留，以免破坏正在执行的查询；因此更新会增加磁盘占用，需要在停服维护时按元数据确认活动集合后清理历史集合。不要直接删除正在运行的 Chroma 段目录。
旧格式元数据可加载；完整性清单不匹配时自动重建。切分规则变化仍需要更新 `SPLITTER_VERSION`。

## 检索与引用

当前普通问答默认：向量粗召回 → 可选距离过滤 → rerank → 取前 k 条原文 → 受约束生成与引用校验 → 最终答复。

`config/chroma.yml` 控制 `k`、`rerank_fetch_k`、重排模型与超时。`max_distance` 默认关闭，不能在换 embedding 后直接沿用未经标定的距离阈值。重排失败降级为向量顺序，日志记录是否实际重排；侧栏显示索引统计和配置中的模型名称，不代表远端服务实时健康。

`config/agent.yml` 的 `rag_raw_documents: true` 开启原文路径：普通问答直接把检索原文交给 Agent，减少一次摘要调用；设为 `false` 则先生成检索摘要。当前默认启用原文路径，并配合受约束生成与引用校验；不能把召回到内容等同于内容能回答问题。

引用按块 ID 去重，保留完整原文和 PDF 页码；历史重新加载后仍显示资料。模型被要求保留 `[数字]` 短引用，但界面列出的仍是检索参考资料，不能仅凭列表断言每条结论都得到资料支持。旧历史中只有短片段的记录不会自动恢复成完整原文。

天气和位置工具目前是模拟数据，输出明确标记，不代表实时天气或用户真实位置。

## Agent 预算

| 配置 | 默认值 | 行为 |
|---|---:|---|
| `max_model_calls` | 12 | 每轮外层模型调用上限，最后一次移除工具以生成答复 |
| `turn_timeout` | 180 秒 | 调用边界检查时间预算，超出后生成可持久化的结束提示 |
| `context_max_chars` | 24000 | 按完整回合裁剪旧消息，保持当前工具消息配对 |

工具总预算为 10 次，其中检索最多 4 次，定义在 `agent/runtime.py`；当前其余已注册工具只受总预算约束。预算检查与预留在锁内完成，避免并发超发。达到预算后从模型可选工具中移除对应工具。

时间预算是协作式检查，不能强制取消已经运行的工具，SDK 重试也可能使实际总耗时超过预算。它不是严格的进程级超时。外层模型日志记录耗时和 token 信息；端到端评估通过回调统计 Agent 链路 token。

## 验证与评估

离线回归：

```bash
python -m unittest discover -s tests -q
```

测试覆盖真实 LangGraph 工具身份注入、工具 schema、并发预算、停止收尾、非 token 流答复、历史引用、跨身份隔离、20 个以上会话清理、Chroma 构建失败恢复和重复写入防护等。

检索质量需要在线调用模型：

```bash
python -m eval.retrieval_eval --set=all --judge --refusals
python -m eval.retrieval_eval --set=all --judge --refusals --snapshot
python -m eval.retrieval_eval --set=all --judge --refusals --check-baseline
# 固定题集和语料，允许配置变化的对照实验：
python -m eval.retrieval_eval --set=all --judge --refusals --check-baseline --compare-config
```

指纹包含语料、提示词和题集哈希，以及检索配置。旧基线没有新字段时报告“未验证”，必须重新评估，不能把旧数字补字段后冒充新结果。漏跑模型判据、已有集合或已有拒答检查会失败。配置变化时重新拍快照不会继承旧配置下的其他集合成绩。

默认检查对配置变化报过期；显式 `--compare-config` 在题集和语料相同的前提下继续比较。关键词回退为提示，模型判据/拒答回退为失败。检索评估仍会按当前配置初始化知识库；实验切分或 embedding 配置时，请在独立项目副本运行。

`--refusals` 检查的是检索摘要工具。另有评估完整 Agent 最终回答、延迟和 token 的命令：

```bash
python -m eval.agent_eval --mode summary --output eval/agent-summary.json
python -m eval.agent_eval --mode raw --output eval/agent-raw.json
```

运行前需先通过应用成功初始化知识库。该命令使用内存会话，不写真实对话历史，不更新知识库。它会调用在线模型及裁判，结果保留最终答复供人工核查；裁判与生成模型同源，有误判可能。token 统计不含裁判调用。

## 配置与限制

- `config/model.yml`：模型、单次请求超时、重试参数。
- `config/chroma.yml`：知识库、切分、重排与检索参数。
- `config/agent.yml`：Agent 预算、原文/摘要路径开关；演示月份和外部记录路径是未接入当前 Agent 的遗留配置。
- `config/prompts.yml`：提示词文件路径。
- SQLite 备份应使用 SQLite backup API，不能在运行期间只复制主文件而忽略 WAL。
- 上游 LangChain/Pydantic 序列化警告仍可能出现；不影响已验证的工具执行，但升级依赖后应重跑测试。
