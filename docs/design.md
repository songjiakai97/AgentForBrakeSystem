# 制动系统测试数据分析与标定指导 Agent — 设计文档

## 1. 项目概述

构建一个面向制动系统测试数据的分析与标定指导 Agent，Web 为唯一入口。

### 核心能力

- **数据读取**：解析 MF4、BLF 等格式，按 YAML 配置将物理信号映射为逻辑信号角色。
- **客观指标计算**：按功能与工况配置，计算量化指标。
- **异常识别**：基于可解释规则引擎判定指标状态与异常。
- **标定指导**：规则引擎给出确定性结论后，由 LLM 结合知识库生成标定方向。
- **图表展示**：Web 用 ECharts 交互式时序图，支持多测线叠加与异常窗口高亮。
- **对话式交互**：Web 以聊天为主入口，支持多文件上传、目标识别（渐进披露 + 候选面板）、分析结果卡片、时序图。

### 设计目标

- **可解释性与稳定性**：指标与异常判定由确定性规则完成；LLM 只做意图解析、候选推荐、语言化标定建议。
- **内核独立**：核心分析逻辑独立成包，Web API 只是薄壳；便于单测、复用与替换入口。
- **工具化**：指标实现为可复用纯函数工具，便于单测、替换与追溯。
- **可降级**：无 LLM Key 或网络失败时，离线确定性路由仍可跑通完整链路。
- **可扩展**：新增功能、工况、指标、档位的成本仅为配置行数，不是逻辑复杂度。

---

## 2. 核心模型：功能 → 工况

### 2.1 两级结构

```text
Function（功能）
  ├── Condition（工况 1）
  ├── Condition（工况 2）
  └── Condition（工况 N）
```

- **Function**：一组相关测试能力的集合，决定指标域、事件分段模板、阈值档位、知识库章节。
- **Condition**：一个具体测试脚本，由 `maneuver`、`surface_code`、`params` 唯一确定，必须归属一个功能。
- **Run-sample**：一次分析中，一个文件内检出的一个事件窗口，是结果卡片与指标判定的基本单元。

### 2.2 唯一键约定

- `function_key`：功能唯一键。
- `condition_id`：工况唯一键，全局唯一。
- `metric_key`：指标唯一键。
- `profile`：阈值档位标签，字符串，可自由拼装（如 `<dim_a>_<dim_b>`）。

指标实例键：`{condition_id}.{metric_key}`。

**结构化定位键**：同一功能内 `maneuver + surface_code + params` 必须唯一确定一个工况，
它是 §13 里 `resolve_condition` 的匹配依据（不是工况名相似度）。加载期校验（§6.1）：
缺 `maneuver`/`surface_code` 或同功能内键值重复 → 直接 `ConfigError` 拒绝启动，
因为这两类配置错误会让"该不该弹面板"变成随机行为。`params` 比较做了数值归一
（`100` / `100.0` / `"100"` 同值），避免书写差异造成伪歧义。

### 2.3 功能元数据

功能声明：

| 字段 | 说明 |
| --- | --- |
| `key` | 功能键 |
| `name` | 显示名 |
| `aliases` | 同义词/别名词表，供离线规则与 LLM 参考 |
| `segmenter` | 绑定的事件分段模板 key（模板实现以代码注册表提供，见 §6.3） |
| `profiles` | 可选；该功能支持的档位标签列表，供前端渲染选择面板 |
| `kb_section` | 知识库手册章节引用 |
| `description` | 功能说明 |

---

## 3. 总体架构

```text
Presentation
  Web 前端
        │
        ▼
Web API（FastAPI）
        │
        ▼
Agent 编排引擎
  多轮对话 / 工具路由 / 目标识别（渐进披露）/ 上下文压缩
        │
        ▼
Analysis Core
  Loader / 事件分段 / 指标引擎 / 规则引擎 / 图表 / LLM 服务
        │
        ├── configs/functions.yaml
        ├── configs/conditions.yaml
        ├── configs/metrics.yaml
        ├── configs/rules.yaml
        ├── configs/signals_*.yaml
        └── knowledge/manual + knowledge/cases
```

约束：

- 前端请求全部经由 `agent/` 编排引擎。
- LLM 只负责槽位抽取（从描述里取 maneuver/路面/参数）、语言化标定建议；
  「哪条工况」由配置索引精确匹配决定（§13），模型不在相似工况名之间挑。
- 客观指标与异常判定完全由确定性内核完成。
- 无 LLM Key 或网络失败时，离线确定性路由仍可跑通完整链路。

---

## 4. 技术栈

| 层次 | 选型 | 说明 |
| --- | --- | --- |
| 语言 | Python 3.10+ | 主力逻辑 |
| 数据读取 | asammdf、python-can、cantools | MF4 / BLF / DBC 解码 |
| 数值计算 | numpy、pandas/scipy | 指标计算 |
| Web 后端 | FastAPI + uvicorn | REST API、静态托管 |
| Web 前端 | Vue 3 + ECharts | 对话式 UI、图表 |
| 规则引擎 | 自研规则 DSL / Pydantic 规则表 | 异常判定 |
| LLM | openai SDK，OpenAI Compatible base_url | 标定建议生成 |
| 配置 | pydantic-settings + python-dotenv + pyyaml | 配置加载与校验 |
| 知识摄入 | python-docx / pypdf | Word/PDF → Markdown/结构化案例 |
| 向量检索（可选） | sentence-transformers + FAISS/Chroma | `ENABLE_RAG` 开关，默认关 |

---

## 5. 目录结构

```text
brake-agent/
├── pyproject.toml / requirements.txt
├── .env.example
├── configs/
│   ├── signals_mf4.yaml
│   ├── signals_blf.yaml        # BLF 侧：dbc_files + 三元组候选（§9.6）
│   ├── dbc/
│   │   └── brake_demo.dbc      # demo 唯一 DBC，由 scripts/generate_demo_blf.py 生成后入库
│   ├── functions.yaml          # 功能清单
│   ├── conditions.yaml         # 工况清单，按 function_key 索引
│   ├── metrics.yaml            # 指标模板（全局）
│   └── rules.yaml              # 异常规则（功能级 + 工况级）
├── knowledge/
│   ├── manual/
│   └── cases/
├── brake_analyzer/
│   ├── configs.py
│   ├── loaders/
│   │   ├── base.py
│   │   ├── mf4.py
│   │   └── blf.py
│   ├── events/
│   │   └── segment.py
│   ├── metrics/
│   │   ├── base.py
│   │   ├── tools/
│   │   └── engine.py
│   ├── rules/
│   │   └── engine.py
│   ├── charts/
│   │   └── html.py
│   ├── llm/
│   │   ├── client.py
│   │   ├── prompts.py
│   │   ├── context.py          # 上下文预算与压缩（§6.10）
│   │   ├── kb.py
│   │   └── ingest.py
│   ├── agent/
│   │   ├── engine.py
│   │   └── tools.py
│   ├── pipeline.py
│   └── schemas.py
├── scripts/
│   ├── generate_demo_data.py   # MF4 demo：合成物理剖面
│   └── generate_demo_blf.py    # BLF demo：cantools 建 DBC + python-can 写 BLF（复用同一剖面）
├── web/
│   ├── main.py
│   ├── store.py
│   └── frontend/
│       ├── index.html
│       └── debug.html
└── tests/
    ├── data/                   # mf4/ 与 blf/ 均可再生，不入库
    └── test_*.py
```

---

## 6. 核心模块设计

### 6.1 配置加载（`configs.py`）

使用 pydantic-settings + pyyaml 加载 `configs/*.yaml`。

`conditions.yaml` 以字典形式加载：`{function_key: [condition, ...]}`。

加载期做以下构建与校验：

- 校验 `conditions.yaml` 顶层键都存在于 `functions.yaml`。
- 校验每个 `condition.id` 全局唯一。
- 校验每个工况的 metrics list 中，同一 `(key, profile)` 不重复。
- 为每个工况构建索引：`metric_index = {metric_key: {default, by_profile}}`。
- 为每个工况计算 `enabled_metrics`（出现过的所有 metric_key 去重）。
- 校验 `metrics.yaml` 覆盖所有被引用的 metric_key。
- 校验 `rules.yaml` 引用的功能、工况、指标存在。
- 校验 `conditions.yaml` 中出现的 profile 值都在功能 `profiles` 列表内（若功能声明了 profiles）。
- 逐功能校验 §2.2 结构化定位键：每个工况都要有 `maneuver`/`surface_code`，且同一功能内
  `maneuver+surface_code+params` 不重复（§13 的解析前提是"命中唯一"，配置歧义必须启动期失败）。

加载失败即报出可读错误，阻止启动。

`signals_blf.yaml` 由同文件的 `load_blf_config(config_dir, logical_names=None)` 加载校验
（与 `load_configs` 分开，因为 BLF 是可选能力，内核启动不强依赖它）：

- `dbc_files` 必须非空，`channel` 必须是 int；`path` 解析成绝对路径后必须存在
  （相对路径先按 `config_dir` 解析，再按仓库根解析，故 yaml 里可写
  `configs/dbc/brake_demo.dbc`，进程从任意 cwd 启动都能找到）。
- 每条候选必须是 `[alias, msg_id, signal_name]` 三元组，alias 在 `dbc_files` 内，
  `msg_id` 符合 `"0x123"` / `"0x123x"` / int 约定。
- 传入 `logical_names`（MF4 侧的逻辑信号集）时校验两侧集合完全一致，避免 BLF 少配信号。
- 只校验结构，不加载 DBC —— 不在内核里硬绑 python-can/cantools；候选解析失败仍由
  `BLFLoader` 逐条回退。

`.env` 管理 `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`OPENAI_MODEL`、`CONTEXT_MAX_TOKENS`、
`CONTEXT_COMPRESS_TOKENS`、`ENABLE_RAG` 等（上下文两项见 §6.10）。

### 6.2 数据读取层（`loaders/`）

- 构造只收配置：信号映射、DBC 等。
- `load(file_path)` 可重复调用。
- 每个逻辑信号角色返回独立 `SignalData`。
- 缺失信号返回空 `ts`/`values`，不抛异常，由指标/规则层标记 missing。
- 信号映射全局共享；功能与工况差异只体现在指标集、参数、阈值。
- 每个 `SignalData` 的 `ts` 统一相对起点偏移，便于跨文件对齐。
- demo 阶段 Web 入口接受 `.mf4` 与 `.blf`：Loader 按扩展名选（`pipeline.make_loader`），
  BLF 走 `signals_blf.yaml` + 唯一一份 demo DBC，映射仍由服务端固定，用户不提供映射配置。
  没装 `python-can`/`cantools` 时 `.blf` 不进上传白名单（`/api/meta.allowed_upload_ext` 随之变化），
  避免收了不能分析的文件。

`SignalData` 契约：

```python
@dataclass
class SignalData:
    ts: np.ndarray                       # 相对 start_timestamp 偏移后的时间轴（秒）
    values: np.ndarray                   # 数值序列（float32）
    choices: Optional[dict] = None       # {raw_int: 枚举字符串}，非枚举通道为 None
    description: str = ""
    unit: str = ""
    start_timestamp: float = 0.0         # 全局最早时间戳，ts 已相对它偏移
```

说明：

- `start_timestamp` 为该文件所有已命中信号的全局最早时间戳；所有信号的 `ts` 统一减去它，保证同一文件内跨信号对齐，`ts[0]` 即相对秒。
- 枚举/字符串通道：物理值为文本时，`values` 存原始整数编码，`choices` 提供 `{raw: label}` 反查；数值通道 `choices = None`。
- 未命中候选的通道仍出现在结果字典中，`ts`/`values` 为空数组，供下游标记 missing。

Loader 实现：

- `MF4Loader(signal_maps)`：基于 asammdf，按 `candidates` 顺序用 `mdf.whereis` 解析首个命中通道；`raw=False` 取物理值，文本采样再以 `raw=True` 回取编码值构造 `choices`。
- `BLFLoader(dbc_files, signal_maps)`：基于 python-can `BLFReader` + cantools 解码。`candidates` 为三元组 `[dbc_alias, msg_id, signal_name]`；`msg_id` 字符串约定：`"0x123"` 标准帧、`"0x123x"` 扩展帧、裸 int 按标准帧兼容处理。单次遍历文件，按 (frame_id, is_extended, channel) 建帧索引，逐帧解码后抽取各信号。
- `make_loader(path, cfg, blf_cfg=None)`（`pipeline.py`）：按扩展名选 Loader，`.blf` → `BLFLoader`，其余 → `MF4Loader`；未给 `blf_cfg` 时分析 `.blf` 直接报"未启用 BLF"。
  `ChatEngine` 按扩展名缓存 Loader 实例（`_loaders`），`BLFLoader` 的 DBC 解析在会话生命周期内只做一次。
- 解码/加载失败（如非 BLF 内容冒充 `.blf`）不抛给用户：Loader 返回空信号，pipeline 补退化窗口，
  指标全 missing、规则命中 data_quality。

跨信号时间对齐的现状与缺口（真实 BLF/MF4 报文周期不一致时才会暴露）：

- Loader 只做「同一文件内的统一相对时间原点」（各信号各自保留原生栅格），不做重采样。
- 需要**点值**的地方已经是插值口径：分段交叉点用线性插值定位，`interp_at` 取窗口端点值，
  `signal_span`/`speed_slope` 都靠它 → 这类指标对栅格不一致天然免疫。
- 需要**窗口内样本**的地方仍按原始栅格，两处已知缺口：
  1. `window_slice` 要求窗口内至少有一个原始采样点，否则判 `None`。信号周期远大于事件时长时
     （如 100 ms 帧 + 30 ms 窗口）会出现「明明可算却判 missing」；
  2. `wheel_slip_max` 以**第一个命中信号的栅格**作参考轴，把其它轮速插到该轴上。慢帧作参考时
     会削掉快帧峰值（demo 剖面实测 19.51 vs 19.98，约 −2.4 %）；参考信号时间跨度不覆盖窗口时
     `np.interp` 在两端静默钳位（实测 vbox 只覆盖中段时 slip 被算成常数差）。
- 结论：对齐应加在**指标层**（共享工具把相关信号插到"最细可用栅格"的公共轴上），
  而不是在 Loader 里预重采样 —— 后者会丢原始采样、放大内存、并对只看端点的指标毫无收益。
  该项未定案前不改判定数值，故列入 §15 待办。

配置 Schema 见 §9.6。

### 6.3 事件分段与时间窗（`events/segment.py`）

接口：

```python
segment(signals, function_cfg, condition_cfg, params) -> list[EventWindow]
```

`EventWindow`：

```python
t_start: float
t_end: float
anchor: float
phases: list[str]
```

- 分段模板由功能通过 `segmenter` 绑定，模板本身在代码中实现并注册（`events/segment.py` 内
  `SEGMENTERS: dict[str, Segmenter]`），新增模板 = 新增一个注册函数，不进入 YAML 配置。
- 无有效事件时返回空窗口，相关指标置 missing。
- 一个文件可含多条同工况事件，每条为一个 run-sample。
- 一次分析 = 一个文件 + 一个工况，但可含多条事件。

### 6.4 指标计算层（`metrics/`）

见 §10 详述。

### 6.5 规则引擎（`rules/engine.py`）

规则基于指标状态触发，不重复比较阈值。

```yaml
- id: <rule_id>
  scope: function | condition
  function: <function_key>
  condition: <condition_id>        # scope=condition 时必填
  metric: <metric_key>
  status: abnormal | missing       # 触发的指标状态
  severity: <severity>             # info | low | high | critical（枚举见 §9.4）
  fault_domain: <domain>           # 枚举见 §9.4
  kb_ref: <section_id>
  message: <可读结论>
```

规则解析顺序：

- 先执行功能级默认规则。
- 再执行工况级覆盖规则。
- 同一指标若被工况级规则命中，以工况级为准。
- 引用 info 指标时启动校验报错，避免误用。

输出 `RuleVerdict[]`，包含：

- 命中规则
- 指标
- 当前状态
- 方向性说明
- 建议的标定方向草稿
- 引用知识库片段

### 6.6 图表（`charts/`）

- `html.py`：将选定信号时间序列转成 ECharts option JSON。
  - 支持多测线叠加、缩放、悬停、异常窗口高亮。
- 本期无静态图/报告导出，Markdown/HTML 报告与 PDF/Excel 导出列入 §15 后续扩展。

### 6.7 编排流水线（`pipeline.py`）

单跑：

```text
load(file)
  → segment(signals, function_cfg, condition_cfg)
  → 每条 EventWindow：
       compute_metrics(signals, function_cfg, condition_cfg, window, metrics_cfg, profile)
       run_rules(metrics, function_cfg, condition_cfg)
       build_charts(signals, metrics, verdicts, windows)
       assemble_analysis(...)
       llm.suggest(...)  # 可选/降级
  → AnalysisResult
```

`AnalysisResult` 一次生成，Web 各视图共用；LLM 生成可选，缺 Key 时跳过。

复现性对比本期不实现，列入 §15 后续扩展。

### 6.8 LLM 服务（`llm/`）

- `client.py`：OpenAI Compatible 客户端，`model` 从 `.env` 读取。
  - 无 Key 时降级为“仅规则结论”模式，界面明确提示。
- `prompts.py`：组装 prompt。分两层——常驻的只有功能层目录与会话状态（§13.1），工况层
  由工具按需注入；标定建议 prompt 仍按「指标集 + `RuleVerdict` + 知识库片段」全量组装。
  工具规格（含 enum）由 `build_tool_specs(cfg, scope_function)` 从配置生成。

要求 LLM：

- 基于证据输出标定方向；
- 写明修改哪个可标定量、方向、预期影响、风险；
- 可标定量只能取自该指标 `tuning_params` 声明的参数（为空时可参考知识库手册），不得编造；
- 区分“确定规则结论”与“建议性判断”；
- 结构化 JSON 输出 `calibration_actions[]`；
- 不得基于 info 指标单独下“异常”结论，只能作为佐证或趋势描述。

其他：

- `context.py`：上下文预算、压缩与一次性内容折叠（§6.10），阈值来自 `.env`。
- `kb.py`：读取手册与案例。默认关键词/规则定位；可选向量检索。
- `ingest.py`：Word/PDF 摄入为 Markdown 或结构化案例。

### 6.9 对话式编排引擎（`agent/`）

所有对话请求都经由 `agent/` 编排。

工具集（四个，规格全部由配置生成，见 §13）：

- `resolve_condition(function_key?, maneuver?, surface_code?, params?)`：按 §2.2 唯一键
  **结构化**定位工况。命中唯一返回 `hop=resolved`；键填不满返回 `hop=condition/function`
  并给候选，由用户确认。取值域来自 `AppConfigs.vocab()`（enum），功能已定时
  `run_analysis_on_files` 的 `condition_id` 进一步限定为 `scope_conditions(function)`。
- `list_conditions(function_key)`：按需给出该功能全部工况（含唯一键属性），供模型填槽；
  结果标 `transient`，只服务当次定位（§6.10）。
- `run_analysis_on_files(condition_id, profile?)`：对会话内全部已上传文件跑指定工况。
- `recommend_targets(query, max_items)`：两跳规则打分，保留为**兜底与离线路径**——
  在线 LLM 不可用、或描述无法转成结构化槽位时使用，与离线路由同一实现，保证降级一致。

说明：

- 工况选择确认与档位选择由前端 `/select` 写入 `Chat.selected_*`，不作为 LLM 工具。
- 文件由前端 chips 展示，`list_files` 不作为 LLM 工具。
- 工具规格由 `prompts.build_tool_specs(cfg, scope_function)` 生成，不写死在代码里；
  新增功能/工况只改 YAML，prompt 与 enum 自动跟随。

编排：

- `ChatEngine.events(chat, text)` 唯一入口，产出统一事件流（delta / message / done）。
  同步等待由 `RunManager` 负责收集，引擎本身不再区分流式与同步两套入口。
- 一轮 = 一个后台任务（`web/runs.py` RunManager）：引擎在独立线程产出事件并按单调
  `seq` 写入缓冲区，SSE 只是缓冲区订阅者。客户端刷新/断网不会中断执行，重连
  `GET /api/runs/{id}/stream?after=<seq>` 可整轮回放或增量续传。
- 真 tool-calling：多轮循环，最多 6 轮。
- 工具 `hop` 的落库规则：`function`/`condition`/`profile` → 落 `target_options` 面板；
  `catalog`（工况清单）与 `resolved` 只作为 `tool` 应答回灌模型，不弹面板；`error` 交还
  模型组织措辞。面板只出现在"信息不足"时，且候选层级由工具保证（`function` 的候选一律是
  功能，不会出现别的功能的工况）。
- 档位守卫：`run_analysis_on_files` 未带 `profile` 且该功能声明了 `profiles` 时，引擎不执行
  分析，改落 `hop=profile` 面板并回一条 `tool` 应答。`hop=resolved` 只保证工况唯一、
  不保证档位唯一，离线路径本来就是"先选档再分析"，两条路径口径必须一致。
- 网络/额度错误自动回退离线路由。
- 流式额外产出 `reasoning`、`tool_call`、`tool_result` 事件。
- 统一消息类型：`text`、`target_options`、`analysis_result`、`attachment`、`error`。

降级：

- 无 `OPENAI_API_KEY` 时完全绕过 LLM。
- 离线路由 + 规则结论仍可跑通整链路。

### 6.10 上下文预算与压缩（`llm/context.py`）

问题：`Chat.messages` 只追加不裁剪，多轮 + 多跳工具往返后送给 LLM 的 `messages`
会一直变大；同时工具结果（分析摘要 JSON、工况清单）本身就可能很大。本节定义长度上限、
压缩策略与"只保留有效内容"的折叠规则，全部由 `.env` 配置。

配置（`.env`，估算 token，非精确 tokenizer 计数）：

| 变量 | 含义 | 默认 |
| --- | --- | --- |
| `CONTEXT_MAX_TOKENS` | 一次 LLM 请求允许的最大上下文；超出即硬截断兜底 | 8000 |
| `CONTEXT_COMPRESS_TOKENS` | 达到该值即触发压缩，把最早的若干轮折叠成一条摘要 | `max(1024, 0.75 × MAX)` |

约束与加载行为：

- `CONTEXT_COMPRESS_TOKENS` 必须严格小于 `CONTEXT_MAX_TOKENS`，否则来不及压缩就撞上
  硬上限；`ContextBudget.from_env()` 直接抛 `ValueError`，Web 启动失败并报可读错误
  （与 §6.1 配置校验同样的"拒绝启动"策略）。
- 非法值（非整数、≤0）同样拒绝启动，不做静默纠正。
- `CONTEXT_COMPRESS_TOKENS` 留空 = 取硬上限的 75%，只调 `CONTEXT_MAX_TOKENS` 也能工作。

Token 估算：不引入 tokenizer 依赖（core/web 环境未必装 openai/tiktoken），按
「CJK 字符 1 token，其余 4 字符 1 token」估算，再加每条消息 4 token 与每次回复
3 token 的固定开销；`assistant` 的 `tool_calls` 参数 JSON 一并计入。估算只用于
判阈值，真上限由 `CONTEXT_MAX_TOKENS` 的硬截断兜住。

压缩流程（`ContextCompressor.fit(messages) -> (messages, note)`）：

0. **先折叠一次性内容**（`prune_transient`，对应"context 只保留有效内容"）：工具 payload
   里标了 `transient: true` 的（`list_conditions` 的工况清单、各候选面板的候选列表）在离开
   最近 `KEEP_RECENT_UNITS`（2）个轮次单元后，正文换成一行存根
   `[已折叠] 一次性候选/清单已折叠：hop=catalog 约 3 项（…）`。**只压正文、保留消息壳**，
   因为删掉整条 `tool` 消息会让 `assistant` 的 `tool_calls` 失去应答而报 400；最近窗口内保留
   原文，否则模型没法照着清单填槽。`resolved`/分析结果不带 `transient`，不会被折叠。
   主要作用点是**同一轮的多跳循环**：清单是模型填槽的中间产物，取到第三条时第一条已经没有
   价值，但还占着每次请求的预算（跨轮则更彻底——面板与工具结果都不回灌，见本节末尾）。
   这一步在阈值之下也会执行并计入 `note`（`… · 先折叠 N 条一次性清单`），因为它本身就是
   有效内容的清理。
1. 折叠后估算值仍 ≤ `CONTEXT_COMPRESS_TOKENS` → 返回折叠结果（没有可折叠内容时
   `note=None`，不触发、不产生步骤）。
2. 超阈值 → 保留开头的固定系统提示（功能目录 + 已上传文件 + 已选工况，§6.8 §13），
   把正文按**轮次单元**切分：一条非 `tool` 消息起头，其后所有 `tool` 结果归入同一单元。
   折叠必须整单元进行，否则 `assistant` 的 `tool_calls` 会与对应 `tool` 消息拆散，
   OpenAI 兼容端点直接报 400。
3. 除最近 `KEEP_RECENT_UNITS`（2）个单元外全部折叠为**一条**摘要消息
   （`role=system`，前缀 `[历史摘要]`，正文声明"非新的用户请求"）。摘要预算取硬上限的
   15%（下限 160 token）。
4. 摘要文本优先由 LLM 生成（`prompts.summarize_request()`，独立子请求，`round=0`
   落 trace）；摘要请求失败或返回空 → 退化为**抽取式**（逐条截断拼接），压缩不会成为
   主链路的新故障点。无 Key 的纯离线路由不发 LLM 请求，因此不触发压缩。
5. 剩余部分装入 `CONTEXT_MAX_TOKENS`：先就地截断最老的超长正文（标记
   `…（超预算已截断）`，每条至少保留 40 token），仍装不下才整单元丢弃最老的；
   **最后一个单元（含本轮提问）永不丢弃**。
6. 没有可折叠的早期内容（首轮或超长单轮）时走同一条裁剪路径。若预算小于系统提示等
   固定开销、裁到下限仍超限，`note` 如实写明「末轮仍超上限」「不裁剪」，不假装压下了
   —— 这属于配置错误，应调大 `CONTEXT_MAX_TOKENS`。系统提示（功能目录）本身不参与裁剪。

增量与幂等：

- 压缩只作用于**发给 LLM 的请求**：`Chat.messages` 原样保留，前端仍显示完整历史与
  全部结果卡片，不因为压缩丢用户可见内容。
- 下一轮的输入若已含摘要，旧摘要并入新摘要，历史里始终最多一条，不会层层套娃。
- 摘要按「待折叠内容 + 预算」哈希缓存（上限 64 条）：多跳工具循环里每轮请求前都会
  `fit`，同一份早期内容只烧一次摘要调用。
- 触发压缩时向 `steps` 落一条 `上下文压缩` 步骤，`detail` 形如
  `上下文 1390 → 362 tok · 摘要覆盖 17 段早期内容 · 截断 1 条超长内容 · 先折叠 2 条一次性清单`，
  前端折叠区可见，用于事后核对 token。同一轮多跳里内容相同则不重复刷同一行。
- `fit()` 不修改入参消息对象（写时复制），与 §6.9 的后台线程并发前提一致。

其他注入口径（同为长度控制，但不由本节配置）：

- 历史回灌只取 `kind ∈ {None, text}` 的 user/assistant 消息，条数上限
  `prompts.HISTORY_TAIL = 40`；条数只是保险丝，真正的长度由 token 预算决定。
  `steps` / `target_options` / `analysis_result` 不进 prompt。
- 单个工具结果写入上下文上限 `prompts.TOOL_PAYLOAD_CLIP_CHARS = 4000` 字。

观测：`GET /api/context/stats?cid=` 返回当前会话上下文的估算 token 与两条阈值的关系；
`GET /api/meta` 的 `context` 字段回显生效的预算与摘要方式（`llm` / `extractive`）。

---

## 7. Web API 设计

### 7.1 内存模型

- **Session**：绑定一个数据文件，缓存信号与各工况分析结果。
- **Chat**：一次人机对话，含 `files`、`messages`、`selected_function`、`selected_condition`、`selected_profile`。

生命周期与回收：

- 每个 Chat 占用独立前端 URL（`/chat/{cid}`），刷新/直达均可恢复到对应对话；
  打开根路径时默认跳转到最近一个 Chat，无 Chat 时自动新建。
  实现走 hash 路由（`/#/chat/{cid}`），服务端静态托管无需 history fallback。
- `DELETE /api/chats/{cid}` 级联删除该 Chat 的全部内存对象：messages、files、
  每文件的 Session（含信号缓存与分析结果缓存）、关联 traces。
- 仅内存存储，服务重启即全部丢失，不做持久化。

消息 kind：

- `text`
- `target_options`
- `analysis_result`
- `attachment`
- `error`

### 7.2 API 列表

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/functions` | 功能清单 |
| GET | `/api/conditions` | 工况清单，可按 `function_key` 过滤 |
| POST | `/api/chats` | 创建对话 |
| GET | `/api/chats` | 对话列表 |
| GET | `/api/chats/{cid}` | 对话详情 |
| DELETE | `/api/chats/{cid}` | 删除对话 |
| POST | `/api/chats/{cid}/files` | 上传一个/多个数据文件（`.mf4`/`.blf`，按扩展名选 Loader） |
| DELETE | `/api/chats/{cid}/files/{fid}` | 移除文件 |
| POST | `/api/chats/{cid}/messages` | 发送用户消息 |
| POST | `/api/chats/{cid}/messages/stream` | SSE 流式发送 |
| GET | `/api/chats/{cid}/run` | 本会话是否有进行中的一轮（刷新后据此重连） |
| GET | `/api/runs/{run_id}/stream?after=<seq>` | 回放/订阅某轮事件，支持断点续传 |
| POST | `/api/chats/{cid}/select` | 统一选择接口：`target_type=function` 或 `condition` |
| GET | `/api/analyses/{analysis_id}` | 单事件样本分析结果 |
| GET | `/api/analyses/{id}/timeseries` | 事件窗口内信号时序 |
| GET | `/api/kb` | 知识库只读浏览（手册 section + 案例清单） |
| GET | `/api/context/stats` | 当前会话上下文的估算 token 与预算阈值（调试用，§6.10） |
| GET | `/api/debug/traces` | 调试 trace 列表 |
| GET | `/api/debug/traces/{tid}` | 单条 trace 详情 |
| GET | `/debug` | 调试页 |

`POST /api/chats/{cid}/select` 请求体：

```jsonc
{
  "target_type": "function | condition",
  "target_key": "<function_key 或 condition_id>",
  "profile": "<profile 标签，可选>"
}
```

- `target_type=function`：写入 `selected_function`，触发第二跳工况精排。
- `target_type=condition`：写入 `selected_condition` 与 `selected_profile`，对全部文件执行分析。

---

## 8. 前端交互设计

### 8.1 布局

- 左侧：对话列表。
- 右侧：聊天主区。
- 底部：输入区，含「+」上传按钮、多行输入、发送。
- 输入区上方：待发送文件 chips、候选面板。

### 8.2 目标确认交互

**① 文件上传**

- 点「+」多选文件。
- 文件仅暂存在输入区上方，不立即上传。
- 点发送时先批量上传，再发送文本。
- demo 阶段接受 `.mf4` 与 `.blf`，其余扩展名前端拒绝并提示；白名单来自 `/api/meta` 的
  `allowed_upload_ext`（未安装 BLF 依赖时不含 `.blf`），前端 `accept` 与校验都按它渲染。
  两套映射（`signals_mf4.yaml` / `signals_blf.yaml`）由服务端固定，用户不提供映射配置。
- 合成样例条带同时列 MF4 与 BLF（`/api/samples` 返回 `{name, format}`），BLF 条目带 `·blf` 标记。

**② 功能面板（`hop=function`）**

- 描述不足以判定功能时弹出，一行一个功能。
- 支持点击或输入数字序号确认。
- 面板只给功能名：该功能下的工况清单只回灌给模型，不混进用户可见选项（§13）。

**③ 工况面板（`hop=condition`）**

- 在选中功能内，唯一键没填满或零命中时弹出，候选一律来自该功能范围。
- 工况所属功能声明了 `profiles` 时，展开档位选择（单下拉），选完再确认。

**④ 其他选项**

- 面板底部提供“其他选项”手动输入框。
- 用户可自由描述未列出的目标，确认后作为新用户消息重新走 §13 识别流程。

**⑤ 确认动作**

- 统一调用 `POST /api/chats/{cid}/select`。
- `target_type=function` 触发下一层识别。
- `target_type=condition` 触发分析并追加 `analysis_result`。

唯一键精确命中（`hop=resolved`）时不再弹工况确认面板；该功能声明了 `profiles` 时仍要先选
档位。面板只在"信息不足"时出现，这是 §13.2 的约束。

### 8.3 结果展示

- 分析结果卡片按 run-sample 分行。
- 每行显示：文件名、事件序号、查看图表、数据链接。**不做行级聚合状态 badge**——
  指标状态在展开明细中逐个展示，每行只显示各指标自身的状态 chip。
- 状态配色：`ok` 绿 / `abnormal` 红 / `missing` 灰 / `info` 蓝。
- 点“查看图表”拉取事件窗口时序，ECharts 多信号叠画。
- 已上传文件在顶部 chips 展示，可移除。

### 8.4 分析状态

- 当前同步返回。
- 前端显示“分析中…”占位气泡。
- 演进规划：状态机 `idle → uploading → choosing_condition → running → done / failed`；本期未引入任务队列。

---

## 9. 配置 Schema

### 9.1 分层

```text
functions.yaml   ← 功能清单：分段模板 key、档位列表、知识库章节
     │ 引用
     ▼
conditions.yaml  ← 按 function_key 索引的工况清单：动作/路面/参数 + 指标 list
     │ 引用
     ▼
metrics.yaml     ← 指标模板：工具绑定、category、unit、tuning_params
     │ 引用
     ▼
signals_*.yaml   ← 逻辑信号角色 → 物理信号映射
```

### 9.2 functions.yaml

```yaml
functions:
  - key: <function_key>
    name: <功能名>
    aliases: [<同义词>, ...]
    segmenter: <segmenter_key>       # 实现在 events/segment.py 代码注册表中
    profiles: [<profile_a>, <profile_b>, ...]   # 可选；该功能支持的档位标签
    kb_section: <section_id>
    description: <说明>
```

### 9.3 conditions.yaml

顶层为按 `function_key` 索引的字典；每个键对应一个工况列表。每个工况的 `metrics` 是一个 list，每个条目直接定义判定条件。

```yaml
<function_key>:
  - id: <condition_id>
    name: <工况名>
    maneuver: <maneuver>
    surface_code: <surface_code>       # 仅元数据，供展示与 LLM 参考
    params:
      <param_key>: <value>
    metrics:
      # 通用条目：所有档位通用
      - key: <metric_key_a>
        ok_range: [<lo>, <hi>]
        params: {<tool_param>: <value>}

      # 单边下界
      - key: <metric_key_b>
        ok_range: [<lo>, null]

      # 单边上界
      - key: <metric_key_c>
        ok_range: [null, <hi>]

      # 特定档位条目
      - key: <metric_key_d>
        ok_range: [<lo_a>, null]
        profile: <profile_a>
      - key: <metric_key_d>
        ok_range: [<lo_b>, null]
        profile: <profile_b>

      # 无硬性规定：省略 ok_range
      - key: <metric_key_e>
        params: {<tool_param>: <value>}

<another_function_key>:
  - id: <condition_id>
    name: <工况名>
    maneuver: <maneuver>
    surface_code: <surface_code>
    params:
      <param_key>: <value>
    metrics:
      - key: <metric_key_f>
        ok_range: [<lo>, <hi>]
```

字段说明：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `key` | 是 | 指标键 |
| `ok_range` | 否 | `[lo, hi]`，`null` 表示该侧无界；省略表示无硬性规定 |
| `profile` | 否 | 档位标签；省略表示通用条目 |
| `params` | 否 | 传给指标工具的参数 |

说明：

- 不存在全局 `surfaces` 段。
- 不存在 `surface_defaults` 段。
- 每个工况的阈值与判定条件完全自包含。
- `surface_code` 仅作工况元数据，不驱动任何阈值继承。
- 边界统一为闭区间 `[lo, hi]`，即 `lo <= v <= hi` 为 ok。
- 判定值即工具输出的 `value`，引擎不做取绝对值等符号加工；需要绝对值语义
  （如横摆峰值、减速度幅值）由工具自身输出保证。
- 同一 `metric_key` 可有多条条目，通过 `profile` 区分档位。
- 若同 `metric_key` 既有通用条目又有 profile 条目，profile 精确匹配优先，无匹配回退通用。

### 9.4 metrics.yaml

```yaml
metrics:
  - key: <metric_key>
    tool: <tool_name>
    category: <category>
    unit: <unit>
    inputs: [<signal_role>, ...]
    tuning_params: [<param_key>, ...]
    description: <说明>
```

不再包含 `better` 字段，方向由 `ok_range` 表达。

`tuning_params` 是**指标 → 可标定量的粗映射**（必填，可为空列表）。demo 阶段仅以字符串
列出参数名，不引入独立配置文件；它随指标条目一起注入 prompt，约束 LLM 只能在这些参数
里给标定方向，避免自由编造不存在的标定量。接入真实标定库后再升级为带单位、当前值域
的结构化定义。

规则枚举：

```yaml
severity: info | low | high | critical
fault_domain: stability | brake_performance | traction | powertrain_interaction | data_quality
```

- `info`：结论性说明，不计入异常。
- `low`：数据质量或可忽略偏差。
- `high`：明确影响性能的偏差，需标定调整。
- `critical`：安全问题（如横摆失控），必须优先处理。

### 9.5 rules.yaml

```yaml
- id: <rule_id>
  scope: function | condition
  function: <function_key>
  condition: <condition_id>        # scope=condition 时必填
  metric: <metric_key>
  status: abnormal | missing
  severity: <severity>
  fault_domain: <domain>
  kb_ref: <section_id>
  message: <结论>
```

### 9.6 signals_*.yaml

#### signals_mf4.yaml

```yaml
signal_maps:
  <逻辑名称>:
    description: <字符串，可选>
    unit: <字符串，可选>
    candidates:
      - <候选通道名1>
      - <候选通道名2>
      - ...
```

- `candidates` 为 MF4 中的物理通道名，按顺序尝试，首个 `whereis` 命中即采用。
- 全部候选未命中：该逻辑信号返回空 `ts`/`values`，不报错。

#### signals_blf.yaml

```yaml
dbc_files:
  <dbc_alias>:
    path: <dbc 文件路径>
    channel: <int，CAN 通道号>

signal_maps:
  <逻辑名称>:
    description: <字符串，可选>
    unit: <字符串，可选>
    candidates:
      - [<dbc_alias>, <msg_id>, <signal_name>]
      - ...
```

- `dbc_files`：CAN 报文库清单；`path` 指向 `.dbc` 文件，`channel` 为该 DBC 对应的 CAN 通道号，用于帧索引匹配。
- `candidates` 每条为三元组 `[dbc_alias, msg_id, signal_name]`，按顺序解析，首个在 DBC 中可解析的即采用。
- `msg_id` 约定：字符串 `"0x123"` 标准帧、`"0x123x"`（十六进制 + `x` 后缀）扩展帧、裸 int 按标准帧兼容处理。
- 三元组解析失败（alias 不存在、frame_id 查不到、signal 不在报文中）尝试下一候选；全部失败返回空 `ts`/`values`，不报错。

demo 约定（`configs/signals_blf.yaml` + `configs/dbc/brake_demo.dbc`）：

- **一份 DBC 管全部 12 个逻辑信号**，alias = `BRAKE`，`channel: 0`；所有 BLF 帧也发在通道 0
  （loader 按 `(frame_id, is_extended, channel)` 匹配，两侧必须一致）。
- DBC 由 `scripts/generate_demo_blf.py` 用 cantools 定义后 `dump_file` 生成，**入库**（4 KB，
  是数据契约的一部分）；BLF 输出到 `tests/data/blf/`，与 MF4 一样不入库、可再生。
- 实车信号名 = 逻辑名（不做重命名），状态位 1 bit 带 `INACTIVE/ACTIVE` 枚举表，
  报文 8 字节标准帧、逐信号连续排布（结构上不可能重叠），`check_layout()` 生成即自检。

  | 帧 | 内容 | 量化 |
  |---|---|---|
  | 0x100 / 0x101 | wheelSpeed_FL/FR、RL/RR | 16bit × 0.01 km/h |
  | 0x102 | speed_vbox / distance_vbox / Ax | 0.01 km/h、0.05 m、有符号 0.01 m/s² |
  | 0x103 | BrakePedalPos / ThrottlePedalPos / ABS_Active / TCS_Active | 8bit × 0.5 %、1bit × 1 |
  | 0x104 | yaw_rate | 16bit 有符号 × 0.01 deg/s |

- factor 的选择原则是「不改变 demo 判定」：distance 0.05 m 相对 40 m 阈值、踏板 0.5 % 相对
  80/95 % 的 `pedal_arm_pct`、速度 0.01 km/h 相对滑移量阈值都留了数量级余量。
- 横摆单独占一帧，`abs_wet60_no_yaw.blf` 整帧不发，用来验证 BLF 路径下的 missing 口径。
- 发送周期 10 ms，与 MF4 demo 的时间栅格一致；物理剖面直接复用 `gen_abs_run`/`gen_tcs_run`，
  且 BLF 场景的 `seed` 显式取该场景在 MF4 `scenario_matrix()` 里的下标 —— 剖面同源，
  两条加载路径的指标差异才只来自 CAN 量化（`tests/test_blf_demo.py` 卡住状态一致 + 数值容差）。

---

## 10. 指标判定与 pick 实现

### 10.1 状态集合

```text
ok | abnormal | missing | info
```

- **ok**：有 `ok_range` 且落在区间内。
- **abnormal**：有 `ok_range` 但落在区间外。
- **missing**：信号缺失、工具无法计算，或所选条目为 `None`。
- **info**：无 `ok_range`，仅记录数值。

规则引擎只对 `abnormal` / `missing` 触发。

### 10.2 MetricResult

```python
@dataclass
class MetricResult:
    key: str                  # {condition_id}.{metric_key}
    name: str
    category: str
    value: float | None
    unit: str
    ts_range: tuple | None
    status: str               # ok | abnormal | missing | info
    raw: dict
    ok_range: tuple | None    # 实际使用的区间
    profile: str | None       # 实际使用的档位
```

### 10.3 加载期：建索引

为每个工况构建：

```python
# metric_index: {metric_key: {"default": entry | None, "by_profile": {profile: entry}}}
def build_index(metrics: list[dict]) -> dict:
    index = {}
    for entry in metrics:
        key = entry["key"]
        slot = index.setdefault(key, {"default": None, "by_profile": {}})
        profile = entry.get("profile")
        if profile is None:
            if slot["default"] is not None:
                raise ConfigError(f"指标 {key} 出现多条默认条目")
            slot["default"] = entry
        else:
            if profile in slot["by_profile"]:
                raise ConfigError(f"指标 {key} 的 profile {profile} 重复")
            slot["by_profile"][profile] = entry
    return index


# enabled_metrics: 出现过的所有 metric_key，去重且保持首次出现顺序
def collect_enabled(metrics: list[dict]) -> list[str]:
    seen = []
    for entry in metrics:
        if entry["key"] not in seen:
            seen.append(entry["key"])
    return seen
```

索引挂到 `ConditionConfig.metric_index`，`enabled_metrics` 挂到 `ConditionConfig.enabled_metrics`。

### 10.4 运行期：pick

```python
def pick(condition_index: dict, metric_key: str, profile: str | None) -> dict | None:
    slot = condition_index.get(metric_key)
    if slot is None:
        return None
    if profile is not None:
        entry = slot["by_profile"].get(profile)
        if entry is not None:
            return entry
    return slot["default"]   # 可能为 None
```

行为：

- profile 精确匹配 → 返回该条。
- 无匹配 → 回退通用条目。
- 无通用条目 → 返回 `None`，指标置 missing。

### 10.5 运行期：`_classify`

```python
def _classify(value: float | None, entry: dict) -> str:
    if value is None:
        return "missing"
    if "ok_range" not in entry:
        return "info"
    lo, hi = entry["ok_range"]
    ok = (lo is None or value >= lo) and (hi is None or value <= hi)
    return "ok" if ok else "abnormal"
```

### 10.6 引擎签名

```python
def compute_metrics(
    signals,
    function_cfg,
    condition_cfg,
    window,
    metrics_cfg,
    profile: str | None = None,
) -> list[MetricResult]:
    index = condition_cfg.metric_index
    enabled = condition_cfg.enabled_metrics

    results = []
    for metric_key in enabled:
        tmpl = metrics_cfg.get(metric_key)
        if tmpl is None:
            continue

        entry = pick(index, metric_key, profile)
        if entry is None:
            results.append(MetricResult(
                key=f"{condition_cfg.id}.{metric_key}",
                name=tmpl.name, category=tmpl.category,
                value=None, unit=tmpl.unit, ts_range=None,
                status="missing", raw={},
                ok_range=None, profile=profile,
            ))
            continue

        try:
            tool = get_tool(tmpl.tool)
            raw = tool(signals, condition_cfg,
                       (window.t_start, window.t_end),
                       entry.get("params"))
            value = raw.get("value")
        except Exception:
            raw, value = {}, None

        status = _classify(value, entry)

        results.append(MetricResult(
            key=f"{condition_cfg.id}.{metric_key}",
            name=tmpl.name, category=tmpl.category,
            value=value, unit=tmpl.unit,
            ts_range=(window.t_start, window.t_end),
            status=status, raw=raw,
            ok_range=entry.get("ok_range"),
            profile=entry.get("profile"),
        ))
    return results
```

### 10.7 工具契约

所有指标工具统一：

- 输入：`(signals, condition, window, params)`
- 输出：`dict`，主值放在 `"value"` 键，其余中间量自由放置。
- `value` 即最终判定值，引擎不做符号加工；绝对值语义（幅值、峰值类指标）
  由工具输出保证（如 `max_abs` 返回绝对值峰值、`speed_slope` 返回变化率幅值）。
- 无信号或无法计算时返回 `{"value": None}`，不抛异常。

这样引擎无需硬编码工具名到主值字段的映射。

### 10.8 多档的边界规则

| 情况 | 行为 |
| --- | --- |
| 只有通用条目 | 所有档位都用通用条目 |
| 只有 profile 条目 | 匹配档位 → 用；不匹配 → missing |
| 通用 + profile | 匹配档位用 profile；否则用通用 |
| 同一 `(key, profile)` 多条 | 加载期报错 |
| 同一 key 多条通用 | 加载期报错 |
| 某档省略 `ok_range` | 该档下该指标状态 info |
| `profile=None` 且无通用 | missing |

---

## 11. 知识库设计

### 11.1 两类内容

| 类型 | 性质 | 可信度 | 用途 | 引用方式 |
| --- | --- | --- | --- | --- |
| 标定手册 | 规范性 | 高 | 规则依据、LLM 规范出处 | `kb_ref` → `section_id` |
| 历史案例 | 参考性 | 参考 | 提供相似先例 | 结构化过滤 + 可选向量检索 |

分层目的：防止 LLM 把历史案例当硬性规范。

### 11.2 目录

```text
knowledge/
├── manual/
│   ├── index.yaml
│   └── <function_key>/*.md
└── cases/
    ├── index.yaml
    └── CASE-*.yaml
```

### 11.3 案例 Schema

```yaml
case_id: <case_id>
meta:
  vehicle: {<key>: <value>}
  date: <date>
  engineer: <name>
function: <function_key>
condition: <condition_id>
profile: <profile>
run_count: <n>
anomaly:
  metric: <metric_key>
  observed: <value>
  unit: <unit>
  status: abnormal | missing
root_cause_hypothesis: <text>
tuning_actions:
  - param: <param_key>
    direction: increase | decrease
    delta: <value>
    effect: <text>
    risk: <text>
validation: {metric: <metric_key>, after: <value>}
outcome: {status: <status>, note: <text>}
learnings: <text>
```

### 11.4 检索与注入

- 手册：按 Markdown 标题切 section，规则经 `kb_ref` → `section_id` 取原文段。
- 案例：`index.yaml` 提供过滤子集，候选再打分/向量检索取 top-k。
- LLM 注入结构：
  - 规则触发结论 + 对应手册 section，标注“规范依据”；
  - 相似历史案例 top-k 摘要，标注“参考，可推广，注意差异”；
  - 指示 LLM 只能把手册当硬约束。

### 11.5 治理

- 案例入库需人工审核。
- 手册 section 带版本号。
- 前端 `GET /api/kb` 只读展示。

### 11.6 Word/PDF 摄入与可选向量检索

- 支持 Word、PDF、Markdown。
- 规范类 → Markdown + section。
- 历史报告类 → 结构化案例 YAML。
- 向量检索仅作为案例/宽松语义检索增强，不作为规范依据核心检索。
- 轻量档：FAISS / Chroma；规模档：Qdrant / Milvus。

---

## 12. 调试观测

- 一次 LLM API 请求 = 一条 trace。
- Trace 记录：`request`、`reply`、`tool_calls`、实际执行工具摘要。
- 上下文压缩的摘要子请求同样落 trace，`round=0`、`user_text=context_summary`（§6.10），
  据此可区分主对话请求与摘要请求。
- 仅内存 `TraceStore`，上限可配（默认 500）。
- 接口：
  - `GET /api/debug/traces`
  - `GET /api/debug/traces/{tid}`
  - `GET /debug`
- 离线路由不产生 trace。
- `/debug` 为独立页面，不影响主链路。

Trace 结构：

```jsonc
{
  "trace_id": "<id>",
  "chat_id": "<id>",
  "user_text": "<本轮用户输入>",
  "ts": <timestamp>,
  "model": "<model>",
  "round": <n>,
  "finish_reason": "tool_calls | stop",
  "request": [ {"role": "system", "content": "..."}, ... ],
  "reply": {
    "content": "...",
    "tool_calls": [ {"id": "...", "name": "...", "arguments": "{...}"} ]
  },
  "tools": [ {"name": "...", "arguments": {...}, "ok": true, "summary": "..."} ]
}
```

---

## 13. 目标识别与渐进披露

工况规模会到 100+ 条。让模型一次性看完全部工况再挑，既烧 token，又不可靠：相邻工况名
只差一个数字，选错了没有任何机制能发现。因此本节的设计原则是——**模型只做"从描述里抽
维度取值"这件它擅长的事，"哪条工况"交给配置索引做确定性的精确匹配**。

### 13.1 两层目录

| 层 | 位置 | 内容 | 成本随什么涨 |
| --- | --- | --- | --- |
| 功能层 | **常驻** system prompt（`function_catalog_text(cfg)`，默认不含工况） | `key`、`name`、`aliases`、档位、该功能工况**条数** | 功能数 |
| 工况层 | **按需**注入（`list_conditions`，仅当轮可见） | 该功能下全部工况 + 唯一键属性 | 单次请求，用完折叠 |

常驻部分刻意不含工况名。用 10 功能 × 15 工况 = 150 条的合成配置实测：功能层 276 token，
把工况全拼进去是 1870 token（每条请求都要重付）；按需注入时单个功能的清单按工况数线性
增长（15 条约 1000 token），只在该轮的多跳循环里可见，用完折叠（§13.4）。工具规格与工况数
无关（150 条配置下 702 token）。`include_conditions=True` 的完整拼法保留给离线展示与对比。

工具规格同样不进 prompt 正文：`build_tool_specs(cfg, scope_function)` 从配置生成 enum，
常驻成本由**维度数**（`vocab()` 的 maneuver/surface_code/param_keys）决定，与工况数无关；
功能已定时 `condition_id` 的 enum 收窄到该功能的工况，非法 id 在结构上不可能出现。

### 13.2 识别流程

```text
用户描述
  → 模型判定功能（只看常驻功能层）
  → resolve_condition(function_key, maneuver, surface_code, params)
       ├─ 唯一命中          → hop=resolved  → run_analysis_on_files
       │                                     （功能声明 profiles 时先确认档位）
       ├─ 命中多条（键不全）  → hop=condition → 弹该功能下的工况面板，用户确认
       ├─ 跨功能歧义/功能未定 → hop=function → 只弹功能面板（不泄露工况清单）
       └─ 槽位抽不出来       → list_conditions(function_key) 取清单后重试；
                               仍不行 → recommend_targets 规则打分兜底
```

约束（写进 `SYSTEM_CHAT`，不只是文档里的期望）：

- 匹配语义：`maneuver`/`surface_code` 精确相等，`params` 按**子集**匹配（只报出 `v0_kph`
  也能定位，不要求复述全部参数），空槽位视为"未填"而不是"匹配任意"。因此填得越满越可能
  唯一命中，填不满只会退化成面板，不会选错。
- **只有 `hop=resolved` 才允许直接开分析**；`hop=condition/function` 表示信息不足，
  必须交用户确认，模型不得自选一条——这是防"静默选错工况"的硬约束。
- 命中了需要档位的功能但未带 `profile` → 引擎守卫拦下，弹档位面板（§6.9），
  与离线路径同一口径。
- 歧义回到哪一层就只暴露那一层的选项：功能未定时不给工况清单，避免面板里混进
  别的功能的工况。
- `hop=catalog`（工况清单）是给模型填槽的中间产物，不作为 `target_options` 落给用户。

### 13.3 hop 语义

| hop | 产生者 | 含义 | 前端动作 |
| --- | --- | --- | --- |
| `function` | resolve / recommend | 功能待定或多功能候选 | 弹功能面板 |
| `condition` | resolve / recommend | 某功能内多条候选或零命中兜底 | 弹工况面板（该功能范围内） |
| `catalog` | list_conditions | 工况清单，仅模型可见 | 不弹面板 |
| `resolved` | resolve / recommend | 唯一键精确命中 | 直接分析（有 profiles 时先选档位） |
| `error` | 两者 | 未知道况/功能 | 提示兜底 |

### 13.4 一次性内容的生命周期

清单与候选是"用完即弃"的内容：留着既占预算，又会让后面几轮把旧候选当成可选项。工具把
这类 payload 标 `transient: true`，`ContextCompressor` 在它们离开最近窗口后压成一行存根
（只压正文、保留消息壳，见 §6.10 第 0 步）。

两个层面的收益，别混为一谈：

- **同一轮内**：多跳工具循环里每跳都会重发此前的清单，取到第三条时第一条已经没有价值，
  折叠让后续请求不再为它付费。
- **跨轮**：`steps` / `target_options` / 工具往返本来就不回灌进下一轮的 prompt（§6.10
  末尾的注入口径），所以跨轮不是本节折叠的主战场；`prune_transient` 的跨轮作用主要是
  兜住"同一轮跑到一半就撞压缩阈值"的情形。

因此"渐进披露"省的是**每条请求的常驻成本**（工况目录不再进 system prompt）与
**多跳循环的重复成本**，不是靠折叠省跨轮 token。

### 13.5 规则兜底（离线路径）

`recommend_targets` 保留两跳规则打分：功能同义词命中 + 功能下工况名命中加分；工况按
关键词与动作/路面词加权；唯一性判据仍是"分差 ≥ 1.0"，否则出面板。在线 LLM 不可用或
描述无法转成结构化槽位时走这条路径，与离线确定性路由同一实现，保证降级行为一致。

候选规模服务端强剪，上限 `min(max(1, n), 8)`。

---

## 14. 部署与运行

`.env`：

```text
OPENAI_BASE_URL
OPENAI_API_KEY
OPENAI_MODEL
CONTEXT_MAX_TOKENS=8000          # 上下文硬上限（估算 token），§6.10
CONTEXT_COMPRESS_TOKENS=6000     # 触发压缩的阈值，必须小于上一项
ENABLE_RAG=false
```

Web：

```bash
python -m uvicorn web.main:app --port 8000
```

Demo 数据（`tests/data/` 不入库，克隆后首次 `pytest` 由 `tests/conftest.py` 自动生成）：

```bash
python scripts/generate_demo_data.py      # → tests/data/mf4（22 个 MF4）
python scripts/generate_demo_blf.py       # → configs/dbc/brake_demo.dbc + tests/data/blf（6 个 BLF）
python scripts/generate_demo_blf.py --period 0.02   # 改发送周期（默认 10 ms，与 MF4 栅格一致）
```

依赖分组：

- `core`
- `web`
- `llm`

避免无 LLM 环境装重依赖。

---

## 15. 风险与后续扩展

### 风险

- 数据无统一基准时间/采样率。
- LLM 稳定性。
- 大文件内存压力。
- 档位标签拼装可能变长，需要命名规范约束。
- 槽位抽取质量只能在有 Key 的环境里实测；本仓库无 Key 时跑的是离线规则路径，
  §13 的"命中唯一才开跑"能保证选错时是弹面板而不是静默分析，但抽错维度仍会出现。
- 结构化解析依赖配置纪律：同功能内唯一键重复会在加载期失败（§2.2），新增工况时
  这是最先要看的报错。
- demo DBC 的帧 id、factor 与 10 ms 周期是自定义的（§9.6）：真实 DBC 到位后量化可能更粗，
  `tests/test_blf_demo.py` 的状态一致断言要重跑，判定阈值余量不足的指标（如 distance）
  可能需要改 factor 而不是放宽容差。

### 后续扩展

- 复现性对比：跨文件 × 事件的同工况 sample 聚合统计与叠画（本期已移除）。
- 报告导出：Matplotlib 静态图 + Markdown/HTML 报告，进而 PDF/Excel。
- 更多格式：ASC/ARXML；BLF 上传已开放（服务端固定 demo DBC），下一步是允许用户自备 DBC
  并与会话内文件配对上传。
- 跨信号时间对齐（重采样）：当前只在需要点值的场合插值，峰值类指标仍按"首个命中信号的栅格"
  取样；真实数据里快慢帧混布会让峰值被慢帧削低，见 §6.2 的对齐缺口与 §15 待办。
- 车型 profile 中心化管理。
- `tuning_params` 升级为结构化标定库（带单位、当前值、值域）。
- 知识库管理后台。
- 批量/脚本化扫描。
- 异步任务化。
- 工况再上量（>200 条/功能）时，`list_conditions` 可加参数按 maneuver/路面预筛，
  避免单次清单过长；当前按需注入 + 用完折叠在 10 功能 × 20 工况量级内不需要。

### 已排除项

- 同一文件多工况分析。
- 指标级三级预警（warn 下沉到规则层 `severity`）。
- 全局路面默认阈值（阈值工况自包含）。
- 把工况目录写成 SKILL.md 这类文档机制：文档要靠人记得去读，且模型会照着文档里的
  旧例子选；工具 + 加载期校验能让"选错"在结构上不可能，因此不走文档路线。
- 让模型在工况名之间做相似度挑选来定目标（150 条量级实测会把同名不同速度的工况
  并列在候选里，无法区分）；`recommend_targets` 的打分只作兜底，不作定案依据。
- 多维 context 匹配（统一为 profile 字符串）。
