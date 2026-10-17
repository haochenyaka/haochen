# Phase 3.1 spike：BGE-M3 + bge-reranker-v2-m3 可行性

状态：spike 完成（TEI CPU 实测数据见 §4.3）
日期：2026-10-17
上游：[remaining-work-plan.md](../remaining-work-plan.md) Phase 3.1 · [remediation-plan.md](../remediation-plan.md) §P3.1

## 1. 结论

1. **CPU-only 可运行，但只有 embedding 侧适合在线使用**。BGE-M3（1024 维）单条查询嵌入 TEI p50 **0.122s**；索引侧吞吐约 **140–158 token/s**（23 个分块 / 8408 token 需 53–61s）。语义 sanity 正常（1024 维输出）。
2. **推荐 TEI 作为 embedding 服务形态**：`ghcr.io/huggingface/text-embeddings-inference:cpu-1.9`，本地目录只读挂载，启动 68–90s，常驻约 2.1GiB；镜像可离线导入，运行时不依赖外网。
3. **rerank 在本机 CPU 上不可用于同步链路**：生产分块平均 365 token，23 个候选 TEI 实测 **53–59s**、PyTorch 16 线程 **33–37s**；30 个候选预计 **65s 以上**。此前“30 候选 3–4s”是 45 字短候选的结果，**不能代表真实分块**。P3.1 建议：本地只落 embedding；rerank 保留 `LlmReranker`/`KeywordReranker` 降级链，或改为 GPU、更小模型，并配合截断与严格 top-N 再评估。
4. **BGE-M3 官方权重只有 `pytorch_model.bin`**，TEI 需要 safetensors → 需要一次性转换（已验证：391 个张量，2.12 GiB）。转换后 TEI 可直接从本地目录加载。
5. **批量上限必须与调用方对齐**：TEI `--max-client-batch-size` 对 `/v1/embeddings` 和 `/rerank` 同时生效。设为 8 时后端 `EMBED_BATCH_SIZE=32` 会直接 422；设为 32 后 23 条单请求验证通过（60.9s）。P3.1 要么把 TEI 上限设为 ≥32，要么把后端批大小降到 8。
6. **接入面必须拆配置**：`MODEL_BASE_URL` 当前同时服务 chat / embedding / rerank；P3.1 要新增 `EMBEDDING_BASE_URL`、`RERANK_BASE_URL`（及对应 key），否则 chat 会打到 BGE 服务。rerank 还要新增 `HttpReranker`（POST `/rerank`），现有 `LlmReranker` 保留作降级。
7. **运行环境有一个独立风险**：本机 Docker Desktop 的 registry 拉取通道被内部代理卡死（所有 `docker pull` 均无进展），与 TEI 无关。落地时需要预置镜像/离线导入，或修复 Docker 代理；详见 §6。

## 2. 环境与网络

| 项 | 实测 |
| --- | --- |
| CPU / 内存 | 16 核 / 16GB 宿主 |
| GPU | 无（`nvidia-smi` 不存在） |
| Docker | 29.8.0，daemon 配置 HTTP/HTTPS 代理 `http.docker.internal:3128`；**VM 内存上限 7.58GB**，当前服务已占约 2.5GB |
| ghcr.io | manifest 可达，blob 直连约 30 KB/s |
| Docker Hub | 不可达 |
| huggingface.co | 直连超时 |
| hf-mirror.com | API 可达；大文件走 xet CDN，约 0.9 MB/s |
| ModelScope | 可达，大文件约 6 MB/s |
| 国内 ghcr 镜像 | `ghcr.m.daocloud.io` manifest 可达，blob 约 190 KB/s |

## 3. 选型对比

| 维度 | TEI | Infinity |
| --- | --- | --- |
| 实现 | Rust（CPU 镜像为优化后的 candle） | Python / PyTorch |
| 镜像来源 | ghcr.io（本机需镜像或离线导入） | Docker Hub（本机当前不可达） |
| 权重格式 | safetensors 必需 | 支持 HF 原生格式（含 `.bin`） |
| embedding 端点 | `/embed`；`/v1/embeddings`（OpenAI 兼容） | `/embeddings`（OpenAI 兼容） |
| rerank 端点 | `/rerank` | `/rerank` |
| CPU 友好度 | 高（动态批处理、静态二进制） | 中（PyTorch 运行时 + 依赖重） |
| 本机实测吞吐 | 约 140–158 token/s（8 物理核满载） | 约 257 token/s（PyTorch 16 线程实测，快约 1.6 倍） |
| 结论 | **首选（部署简单、内存可控）** | 备选（若需要榨干 CPU 吞吐） |

若显式启用 BGE rerank，降级链为：`HttpReranker`（BGE）→ `LlmReranker`（chat 打分）→ `KeywordReranker`（词面）；默认链路从 `LlmReranker` 开始。

## 4. 实测数据

### 4.1 模型获取与转换

- 权重来源：ModelScope（`BAAI/bge-m3`、`BAAI/bge-reranker-v2-m3`），两个大文件并发下载约 6 MB/s。
- BGE-M3：官方仓库无 safetensors；`pytorch_model.bin` → safetensors 转换后 **391 个张量 / 2.12 GiB / 1024 维**。
- reranker：官方仓库自带 `model.safetensors`（2.12 GiB），无需转换。
- 模型目录合计约 **4.3 GiB**，不进入 git；需下载脚本/校验和/离线包。

### 4.2 PyTorch CPU 基准（16 线程）

| 指标 | 结果 |
| --- | --- |
| embedding 加载 | 4.6s |
| reranker 加载 | 5.6s |
| 单条查询嵌入（p50） | **0.095s**（min 0.090 / max 0.104） |
| 23 个分块批量嵌入（平均 527 字） | **31.0s** |
| reranker 常驻内存（RSS，仅该进程） | **2.9 GB** |
| rerank 10 候选 | 1.10s |
| rerank 15 候选 | 2.01s |
| rerank 20 候选 | 2.22s |
| rerank 30 候选 | 3.06s（另一次 4.18s） |
| rerank 相关片段排名 | 10/15/20/30 候选下均为 **第 1** |
| rerank 23 个真实分块（平均 527 字 / 365 token） | **32.7–36.6s** |

> 上表 rerank 的 10–30 候选数字使用 45 字左右的合成干扰项，只反映短文本场景；把候选换成知识库真实分块后延迟上升一个数量级（最后一行）。**生产评估与容量规划必须以真实分块数据为准。**

检索语义 sanity（query → passage，余弦）：

| 片段 | 分数 |
| --- | --- |
| 早餐供应时间 7:00–10:00 | **0.7638（rank 1）** |
| 客房清洁服务时间 | 0.5929 |
| 退房时间 | 0.5501 |
| 房型介绍 | 0.4971 |
| 停车场 | 0.4714 |

### 4.3 TEI 运行时实测

镜像：`ghcr.io/huggingface/text-embeddings-inference:cpu-1.9`（离线导入，约 657MB）。embedding / reranker 各起一个容器，模型目录只读挂载，`--auto-truncate`；运行参数经一轮调优后为 `TOKIO_WORKER_THREADS=8`、`RAYON_NUM_THREADS=16`、`--tokenization-workers 2`、`--max-batch-tokens 4096`。推理时容器 CPU 约 800–820%，即 16 逻辑核中约 8 个物理核满载，继续加线程无收益。

| 指标 | embedding（bge-m3） | reranker（bge-reranker-v2-m3） |
| --- | --- | --- |
| 启动到 `/health` 200 | 68–90s | 90s |
| 常驻内存 | 2.10GiB（批量时 2.37GiB） | 2.10–2.27GiB |
| 输出 | 1024 维，`/embed` 与 `/v1/embeddings` 结构正常 | `[{index, score}]`，相关片段排第 1（短候选 score 0.9904） |
| 小请求 | 3 条短文本 0.278s | 8 条短候选 1.76s |
| 单条查询 | p50 **0.122s**（min 0.106 / max 0.178） | — |
| 23 个真实分块（527 字 / 8408 token） | 3×8 分批 **55.7s**（4 线程）；单请求 23 条 **60.9s**（调优后） | **59.4s**（4 线程）→ **53.4s**（调优后） |
| 30 个短候选（45 字） | — | 6.8s（4×8 分批） |

关键现象：

1. **吞吐是算力上限，不是 HTTP 开销**。真实分块下 embedding 约 140–158 token/s，rerank 同量级；23 候选拆成 3 个请求与合并成 1 个请求总耗时接近。
2. **PyTorch 16 线程比 TEI 快约 1.6 倍**（23 分块：embedding 31.0s，rerank 32.7–36.6s），但内存与依赖更重；两者都远高于交互可接受范围。
3. **客户端批量上限**：`--max-client-batch-size 8` 时，23 条 embedding 与 30 条 rerank 均返回 422；改为 32 后 23 条 embedding 单请求 200。reranker 日志另有 `Backend does not support a batch size > 4`，限制的是后端的并发批次数，不改变单请求文本条数。
4. **rerank 输入长度是主成本**：527 字分块平均 365 token；截断到 256 字约 177 token，按同吞吐估算可降到约一半，但仍需数秒到数十秒，不能解决交互延迟问题。
5. **两个模型同时驻留约 4.4–4.6GiB**，叠加现有服务约 2.5GiB 后接近本机 Docker VM 的 7.58GB 上限。

## 5. 与现有代码的接入点

| 位置 | 现状 | P3.1 改动 |
| --- | --- | --- |
| `app/config.py:31-32,58-59` | embedding/rerank 复用 `MODEL_BASE_URL` / `MODEL_API_KEY` | 新增 `EMBEDDING_BASE_URL`、`EMBEDDING_API_KEY`、`RERANK_BASE_URL`、`RERANK_API_KEY`；`MODEL_*` 只给 chat/judge |
| `app/knowledge/embedding.py:142` | `build_embedding_provider()` 用 `MODEL_BASE_URL` | 改用 `EMBEDDING_BASE_URL`；TEI 的 OpenAI 兼容端点是 `/v1/embeddings`，base URL 需带 `/v1` 或改拼接逻辑 |
| `app/knowledge/embedding.py:84` | `base_url + "/embeddings"` | 明确约定 base URL 含 `/v1`（compose 注入 `http://bge-embed:80/v1`） |
| `app/knowledge/rerank.py:197` | 仅 `LlmReranker` / `KeywordReranker` | 新增 `HttpReranker`：POST `/rerank`，body `{query, texts}`，响应 `[{index, score}]`；按 `RERANK_BASE_URL` 选择，失败降级。**CPU 下默认不启用**，见 §6 |
| `app/eval/kb_runner.py:112-122` | identity 为 `openai-compatible:*` / `llm:*` | 改为 `bge-m3:1024` 与 `bge-reranker-v2-m3`，否则 `retrieval_config` 与指纹不可追溯 |
| `app/knowledge/milvus_store.py:58` | 集合名由 provider identity 派生 | 换 provider 后自动生成新集合（如 `haochen_kb_bge_m3`），维度 1024；需一次全量重建 |
| `app/knowledge/task_handlers.py:37` | `EMBED_BATCH_SIZE=32` | TEI `--max-client-batch-size` 必须 ≥32，否则 422；也可把后端批大小降到 8。索引耗时按 §4.3 约为每 23 个分块 55–61s |
| `deploy/docker-compose.yml` | 无 BGE 服务 | 新增 `bge-embed`、`bge-rerank`（`profiles: ["knowledge"]`），模型目录只读挂载，healthcheck `/health`，不暴露宿主端口；两者同时驻留约 4.5GiB |
| CI | 默认 dev 确定性实现 | 保持不变；BGE 只在真实环境变量显式配置时启用 |

## 6. 环境风险与处置

1. **Docker registry 拉取被代理卡死**：本机 `docker pull`（ghcr、Docker Hub、国内镜像）在 manifest 后均无进展；同一网络下 curl 可下载 blob。处置：在可联网机器拉取后 `docker save`/`docker load`，或修复 Docker Desktop 代理，或离线镜像包随仓库发布。**这是本机环境问题，不是 TEI 选型问题。**
2. **模型文件体积**：约 4.3 GiB，禁止进 git；提供下载/校验脚本，或构建带模型的私有镜像。
3. **rerank 延迟（阻塞性）**：生产分块下 23 候选需 53–59s，且 rerank 位于同步问答链路（`retrieval.py:146`）。本机 CPU 不能启用 BGE rerank。处置顺序：① P3.1 默认保留 `LlmReranker`；② 确有本地化需求时改用 GPU 或更小 reranker；③ 若仍用 CPU，必须把候选降到个位数并截断输入，且先设定可接受的 P95 延迟预算再验证。
4. **索引吞吐**：约 140–158 token/s，23 个 527 字分块需 55–61s；一个 1000 分块的知识库约需 40–45 分钟。属一次性成本，但重建窗口和任务超时必须按此配置。
5. **批量上限不一致**：`EMBED_BATCH_SIZE=32` 与 TEI 默认 `--max-client-batch-size 8` 冲突（实测 422）。compose 中显式设为 32，或把后端降到 8；`HttpReranker` 也必须按上限分批候选，不能一次性提交 30 条。
6. **内存**：两个模型常驻约 4.4–4.6GiB；本机 Docker VM 只有 7.58GB（现有服务约 2.5GB），同时驻留后余量不足 1GB。落地前必须：把 Docker Desktop 内存调到 10–12GB，或 embedding/rerank 分机运行，或只常驻 embedding、rerank 按需启动。
7. **质量回退风险**：FAQ 数据集上关键词重排可能已很强；换真实 provider 后若 `chunk_recall@10` 不升反降，保留 dev provider 与旧集合作回滚，不强行切换基线。

## 7. P3.1 落地清单

1. 配置拆分：`EMBEDDING_*` / `RERANK_*` + `.env.example` + compose 注入。
2. `HttpReranker` + 单测（成功、非 2xx、结构异常、超时降级）；按 TEI 上限分批候选。
3. identity 与集合命名更新；旧集合保留，重建脚本复用 `knowledge/recovery.py`。
4. compose 增加 BGE 服务与模型目录约定；补模型下载/转换/校验脚本；`--max-client-batch-size` 与后端批大小对齐。
5. 全量重建知识库（按约 40–45 分钟/1000 分块预留窗口）→ 跑 169 题 KB 评估 → 对比 dev vs BGE 的 `chunk_recall@10` / `primary@1`。
6. 更新 `docs/eval/kb-baseline.json` 与 README；配置指纹变化后旧报告按既有机制作废。
7. **rerank 默认仍走 `LlmReranker`/`KeywordReranker`**；BGE rerank 作为显式开关保留。只有在 GPU 或更小模型上把 P95 延迟压到预算内，才切换默认链路。

**验收结果（2026-10-17）**：清单 1–6 已完成。20 篇文档 / 95 个分块全量重建至 `haochen_kb_bge_m3`；169 题评估落库报告 ID 4，`chunk_recall@10` **0.9941**、`primary@1` **0.6746**（旧 dev 基线 0.9675 / 0.6036，报告 2/3 已作废）。rerank 保持 `keyword`，`bge-reranker-v2-m3` 未进入本次基线。

## 8. 未验证项

- 并发查询下的尾延迟与长稳内存（本轮均为串行单请求）。
- GPU 下的吞吐与显存占用（本机无 GPU）。
- 截断输入、候选降到个位数后的 rerank 延迟与质量损失。
- 与现有 `LlmReranker` 的延迟/成本对比（需真实 API 与同一批问题）。
