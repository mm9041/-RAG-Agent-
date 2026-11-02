# FastAPI + Vue 重构说明

2026-10-31。业务内核（`agent/` `rag/` `model/` 与业务 `utils/`）未改动，Streamlit 前端已移除。

> 本文是那次重构的**当时记录**，不是当前状态的说明书。文中模型名、延迟与测试数量随后续迭代已变化（例如"246 → 215"是重构当时的对比，当前为 254；模型已从 `qwen3.8-max` 换成 `deepseek-v4.1-flash-1017`）。最新数据以 [README](../README.md) 为准。

## 快速开始

```powershell
# 首次：安装前端依赖并构建
cd web
npm install
npm run build
cd ..

# 启动（若缺前端产物会自动构建）
.\start.ps1
# 指定解释器 / 端口
.\start.ps1 -Python "D:\anaconda3\envs\time\python.exe" -Port 8000
```

打开 <http://127.0.0.1:8000>。接口文档在 `/docs`。

开发模式要开两个终端（`/api` 已在 vite 里代理到 8000，cookie 同源生效）：

```powershell
# 终端 1
python -m server --port 8000 --reload
# 终端 2
cd web; npm run dev        # http://localhost:5173
```

## 目录

| 路径 | 说明 |
|---|---|
| `server/` | FastAPI 后端：协议转换、鉴权、生命周期 |
| `web/` | Vue 3 + Vite 前端 |
| `agent/` `rag/` `model/` `utils/` | 业务内核，**本次未改动** |
| `tools/probe.mjs` `tools/interact.mjs` | CDP 交互回归脚本（开发期验证用） |

## 复用边界

重构前全项目只有 5 个文件引用 streamlit，且都在 UI 层。下表中的 Streamlit 侧文件已于同日删除：

```
表现层   app.py 414 行 · utils/service_ui.py 180 行 · chat_session · admin_auth 的 cookie 部分   → 已删除
接入层   utils/jobs.py 的后台线程 + 轮询                                                        → 已删除，换成 SSE
内核层   agent/ rag/ model/ utils/（约 2500 行）                                                → 原样复用
```

`utils/admin_auth.py` 没有被整删：它只管令牌签发/校验/撤销（8 小时过期、改密码失效、服务端存摘要），这部分后端仍在用，删掉的只是 `restore`/`logout`/`sync_cookie` 这些 Streamlit 读写层。

`utils/jobs.py` 的 `AnswerJob` 曾负责"后台线程 + queue + snapshot"来绕开 Streamlit 的同步重跑模型，SSE 下整条链路不需要了。原先依赖它的测试改成直接守 agent 层契约（cancel 已置位则这一轮不启动、不污染历史）。

## 四个关键决策

**1. 同步生成器必须放线程。**
`ReactAgent.stream_events` 是同步阻塞的（模型调用在等网络）。在 async 生成器里直接迭代会占住事件循环，一个用户的慢请求会卡住所有人的流。`server/sse.py` 的 `EventBridge` 用专用线程跑生成器，事件经 `asyncio.Queue` 送回循环。

**2. SSE 而不是轮询。**
事件协议与 `stream_events` 一一对应（`phase`/`content`/`tool_start`/`tool_end`/`sources`/`final`），只多了传输层自己的 `start` 与 `error`。前端不能用 `EventSource`（它只支持 GET），所以是 `fetch` + `ReadableStream` 手动解帧。

**3. 初始化的锁必须拆成两把。**
处理建库要几秒到几分钟。第一版用一把 `Lock` 包住整个 bootstrap，结果 `/api/system/status` 全部超时——前端连"正在初始化"都看不到。现在 `_boot_lock` 只串行化初始化，`_state_lock` 只保护字段读写。

**4. 先监听、后预热。**
lifespan 起后台线程装配 Agent，uvicorn 立刻开始接请求，前端轮询 `/api/system/status` 显示阶段。初始化失败也能进界面看错误、点"重新初始化"，不像旧版白屏。

## 安全边界

- **身份只从签名 cookie 读**，请求体里的任何 `user_id` 都不采信；切换演示用户也由服务端重签。
- 管理员令牌仍在 sqlite 里存摘要，HttpOnly + SameSite=Strict。
- SPA 兜底路由校验 resolve 后的路径必须在 `web/dist` 内，防 `../../.env` 读取。
- 不提供"列出全部反馈"的用户端接口——`feedback_rows()` 含他人问答，只挂在管理员路由下。

## API（30 个）

```
POST   /api/chat                     SSE 推送问答事件
POST   /api/chat/cancel              显式停止生成
GET    /api/sessions                 会话列表
POST   /api/sessions                 新建会话
GET    /api/sessions/{id}/history    历史（含点赞状态）
DELETE /api/sessions/{id}            删除会话
POST   /api/sessions/clear-others    清理其他会话
POST   /api/feedback/like            点赞
GET    /api/feedback/liked           查询点赞状态
GET    /api/auth/me | login | logout | switch
GET    /api/system/status | info     就绪状态 / 知识库信息
POST   /api/system/retry             重新初始化
GET    /api/admin/overview | knowledge | feedback | models | me
POST   /api/admin/login | logout | knowledge/preview | knowledge/publish
PUT    /api/admin/models             应用模型配置（换 embedding 会重建索引）
DELETE /api/admin/knowledge/{name}
GET    /api/admin/knowledge/versions/{name}[/{version}]
```

## 实测

| 指标 | 结果 |
|---|---|
| 冷启动到就绪 | 1.39–1.76 秒（知识库无变化时，多次测量） |
| 首字（"你好"） | 2.1 秒（客户端实测，含网络往返） |
| 完整回复 | 3.0 秒 |
| 前端产物 | JS 242 KB / gzip 99.7 KB，CSS 15.8 KB |

**必须说清楚的一点**：首字 2.1 秒和旧版实测（0.875–2.593 秒）是同一量级，因为瓶颈是模型首次决策 + 检索 + rerank + 核对四段网络往返，换框架动不了这块。而且当前模型是 `qwen3.8-max`，旧记录里是 `qwen3.7-flash`，两者不可直接对比。

这次重构真正改善的是：冷启动（服务起来就绪，用户零等待）、交互响应（无 rerun 全脚本重跑）、流式顺滑度（token 直推而不是 0.2 秒轮询）。

## 已知差异

1. 会话选择存在 URL 的 `?chat=`，不再有 `?user=` —— 身份在 cookie 里。
2. 工具调用前的"思考"收进折叠面板（旧版直接丢弃），可以展开看。
3. 纯 Streamlit 的 `AppTest` 用例已随前端删除；其中仍有价值的业务断言（停止语义、令牌过期、流式增量、点赞入库）已改写保留，测试数 246 → 215。
4. 部署形态变成"构建 + 单进程 uvicorn"，`--workers` 必须为 1（SqliteSaver、向量库连接、取消注册表都是进程内单例）。

## 回滚

已不提供 Streamlit 入口，前端代码已从工作区删除。需要时从 git 历史取回：

```powershell
git log --oneline -- app.py
git checkout <commit> -- app.py utils/service_ui.py utils/jobs.py utils/app_resources.py utils/chat_session.py
```

恢复后 `requirements.txt` 要加回 `streamlit`。注意两套**不能同时运行**：共用 `checkpoints.sqlite` / `feedback.sqlite` / `chroma_db`，配置锁（`.config_guard.sqlite`）与索引锁会互相等待，且两端同时写检查点会竞争。

## 验证记录

本次重构在本机 `D:\anaconda3\envs\time`（Python 3.10.20，fastapi 0.141.1 / uvicorn 0.52.4）实测通过：

- 冷启动、SSE 事件序列（`start → phase×3 → content×N → phase → final`）正常
- 真实问答往返：首字 2.1s / 完成 3.0s
- 停止生成：按钮 → cancel 接口 + 断连双路，输入框正确恢复
- 点赞：置灰并显示"已点赞"
- 引用面板：6 处引用头、18 条片段可展开
- 管理端：401 拦截、登录、概览/文件/反馈三页渲染正常
- 路径穿越与越权接口均已封堵
- 删除 Streamlit 前端后：215 项离线测试连续三次通过，服务启动与问答链路正常

## 2026-10-31 追加改动

清理 Streamlit 之后又补了一轮，每项都先量了数据再改。

### 引用校验：区分"没引用"和"引错编号"

`valid_citations` 原先要求回答里必须出现 `[数字]`，否则回退成原文堆叠。后果是：一个内容正确、只是没标引用的回答，会被替换成一大段原文，可读性反而更差。现在拆成两件事：

- 引用了不存在的编号、或引用格式损坏 → 仍然回退（这是幻觉风险，必须拦）
- 完全没有写引用 → 保留答案，只是前端不显示引用徽章

实现是新增 `has_invalid_citations()`，`valid_citations()` 保留给旧调用方。

### 角色模型分离

`config/model.yml` 新增 `decision_model_name` / `grounding_model_name`，留空即跟随主模型。实测：

| 配置 | 闲聊首字 | HEPA 首字 | 吸力首字 |
|---|---:|---:|---:|
| 全量 `qwen3.8-max` | 1.61s | 0.83s | 1.23s |
| decision 换 flash | 0.77s | 0.50s | 0.66s |
| grounding 也换 flash | 0.84s | 2.52s | 1.88s |

decision 换小模型稳定快 40–50% 且三次都正确触发检索，采用；grounding 换 flash 会让同一问题从 195 字膨胀到 1342 字、"200 字内"的约束失效，final 反而更慢，不采用。

### 长会话分页

`GET /api/sessions/{id}/history` 新增 `limit` / `before`，返回 `start` / `total` / `has_more`。前端只渲染最近 60 条，向上滚动加载更早的并补偿 `scrollTop`。

**`start` 不是可选项**：点赞的 `answer_id` 里含消息在**完整历史**中的下标，用页内下标会让翻页后的点赞写到别的消息上。

### 前端新增交互

- 历史向上翻页
- 重新生成：后端 `POST /api/sessions/{id}/truncate` 用 `RemoveMessage` 把上一轮（提问 + 回答）真正从检查点删掉，再重发同一个问题；否则历史里会留下重复提问
- 复制回答
- 正文里的 `[n]` 可点击，展开引用面板并定位高亮对应片段
- `StageBar` 的 `stopping` 从硬编码改为真实停止状态

### 测试

218 项（新增 `test_truncate_last_turn_removes_question_and_answer` 与 `tests/test_role_models.py`）。

`tests/__init__.py` 会清空角色模型配置：否则生产配置里一旦配了 `decision_model_name`，测试中的 `patch('...get_chat_model')` 就被绕过，代码会去构造真实客户端，导致 6 个用例失败。
