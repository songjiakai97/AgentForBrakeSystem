# 制动系统测试数据分析与标定指导 Agent — 设计文档

## 1. 项目概述

构建一个面向制动系统测试数据的分析与标定指导 Agent，支持 CLI 与 Web 两种入口。

### 核心能力

- **数据读取**：解析 MF4、BLF 等格式，按 YAML 配置将物理信号映射为逻辑信号角色。
- **客观指标计算**：按功能与工况配置，计算量化指标。
- **异常识别**：基于可解释规则引擎判定指标状态与异常。
- **标定指导**：规则引擎给出确定性结论后，由 LLM 结合知识库生成标定方向。
- **图表展示**：Web 用 ECharts 交互图，报告/CLI 用 Matplotlib 静态图。
- **对话式交互**：Web 以聊天为主入口，支持多文件上传、两跳推荐、候选面板、分析结果卡片、时序图。

### 设计目标

- **可解释性与稳定性**：指标与异常判定由确定性规则完成；LLM 只做意图解析、候选推荐、语言化标定建议。
- **CLI / Web 共享内核**：核心分析逻辑独立成包；CLI 与 Web 只是薄壳。
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

- **Function**：一组相关测试能力的集合，决定指标域、默认指标集、事件分段模板、阈值档位、知识库章节。
- **Condition**：一个具体测试脚本，由 `maneuver`、`surface_code`、`params` 唯一确定，必须归属一个功能。
- **Run-sample**：一次分析中，一个文件内检出的一个事件窗口；复现性对比以 run-sample 为基本单元。

### 2.2 唯一键约定

- `function_key`：功能唯一键。
- `condition_id`：工况唯一键，全局唯一。
- `metric_key`：指标唯一键。
- `profile`：阈值档位标签，字符串，可自由拼装（如 `<dim_a>_<dim_b>`）。

指标实例键：`{condition_id}.{metric_key}`。

### 2.3 功能元数据

功能声明：

| 字段 | 说明 |
| --- | --- |
| `key` | 功能键 |
| `name` | 显示名 |
| `aliases` | 同义词/别名词表，供离线规则与 LLM 参考 |
| `segmenter` | 绑定的事件分段模板 |
| `default_metrics` | 默认启用的指标集 |
| `profiles` | 可选；该功能支持的档位标签列表，供前端渲染选择面板 |
| `kb_section` | 知识库手册章节引用 |
| `description` | 功能说明 |

---

## 3. 总体架构

```text
Presentation
  CLI / Web 前端
        │
        ▼
Web API（FastAPI）
        │
        ▼
Agent 编排引擎
  多轮对话 / 工具路由 / 两跳推荐
        │
        ▼
Analysis Core（CLI 与 Web 共享）
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

- Web 与 CLI 都经由 `agent/` 编排引擎。
- LLM 只负责意图解析、候选推荐、语言化标定建议。
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
| CLI | click / typer | 命令入口 |
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
│   ├── signals_blf.yaml
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
│   │   ├── html.py
│   │   └── report.py
│   ├── llm/
│   │   ├── client.py
│   │   ├── prompts.py
│   │   ├── kb.py
│   │   └── ingest.py
│   ├── agent/
│   │   ├── engine.py
│   │   └── tools.py
│   ├── pipeline.py
│   └── schemas.py
├── cli/
│   └── main.py
├── web/
│   ├── main.py
│   ├── store.py
│   └── frontend/
│       ├── index.html
│       └── debug.html
└── tests/
    ├── data/
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

加载失败即报出可读错误，阻止启动。

`.env` 管理 `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`OPENAI_MODEL`、`ENABLE_RAG` 等。

### 6.2 数据读取层（`loaders/`）

- 构造只收配置：信号映射、DBC 等。
- `load(file_path)` 可重复调用。
- 每个逻辑信号角色返回独立 `SignalData`。
- 缺失信号返回空 `ts`/`values`，不抛异常，由指标/规则层标记 missing。
- 信号映射全局共享；功能与工况差异只体现在指标集、参数、阈值。
- 每个 `SignalData` 的 `ts` 统一相对起点偏移，便于跨文件对齐。

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

- 分段模板由功能通过 `segmenter` 绑定，不硬编码到某个动作类型。
- 无有效事件时返回空窗口，相关指标置 missing。
- 一个文件可含多条同工况事件，每条为一个 run-sample。
- 一次分析 = 一个文件 + 一个工况，但可含多条事件。
- 复现性对比 = 跨文件 × 事件的全部同功能同工况 sample 聚合。

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
  severity: <severity>             # 严重度，由规则定义
  fault_domain: <domain>
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
- `report.py`：Matplotlib 静态图 + Markdown/HTML 报告。
  - info 指标在报告中单列，不计入合格率/异常率。

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

复现性对比：

- `pipeline.compare_group(samples, function_key, condition_id)`
- `samples` 是该功能下该工况的全部 run-sample。

聚合：

- 逐指标 `run_stats`：均值、中位数、标准差、极差、CV%。
- 叠画相同信号曲线，按 anchor 对齐。
- 指标分组柱状图、CV% 条形图。
- 对明显离群样本标 outlier。
- 产出 `GroupResult`。

`AnalysisResult` 一次生成，CLI/Web 共用；LLM 生成可选，缺 Key 时跳过。

### 6.8 LLM 服务（`llm/`）

- `client.py`：OpenAI Compatible 客户端，`model` 从 `.env` 读取。
  - 无 Key 时降级为“仅规则结论”模式，界面明确提示。
- `prompts.py`：将功能、工况、指标集、`RuleVerdict`、知识库片段组装成 prompt。

要求 LLM：

- 基于证据输出标定方向；
- 写明修改哪个可标定量、方向、预期影响、风险；
- 区分“确定规则结论”与“建议性判断”；
- 结构化 JSON 输出 `calibration_actions[]`；
- 不得基于 info 指标单独下“异常”结论，只能作为佐证或趋势描述。

其他：

- `kb.py`：读取手册与案例。默认关键词/规则定位；可选向量检索。
- `ingest.py`：Word/PDF 摄入为 Markdown 或结构化案例。

### 6.9 对话式编排引擎（`agent/`）

Web 与 CLI 都经由 `agent/` 编排。

工具集：

- `recommend_targets(query, max_items)`：两跳推荐入口，返回功能候选、工况候选或已解析结果。
- `run_analysis_on_files(condition_id)`：对会话内全部已上传文件跑指定工况。

说明：

- 工况选择确认与档位选择由前端 `/select` 写入 `Chat.selected_*`，不作为 LLM 工具。
- 文件由前端 chips 展示，`list_files` 不作为 LLM 工具。
- 离线确定性路由内部可保留等价辅助方法，但不暴露给 LLM。

编排：

- `ChatEngine.run(chat, text)` 同步入口。
- `ChatEngine.stream(chat, text)` SSE 流式入口。
- 真 tool-calling：多轮循环，最多 6 轮。
- 网络/额度错误自动回退离线路由。
- 流式额外产出 `reasoning`、`tool_call`、`tool_result` 事件。
- 统一消息类型：`text`、`target_options`、`analysis_result`、`attachment`、`error`。

降级：

- 无 `OPENAI_API_KEY` 时完全绕过 LLM。
- 离线路由 + 规则结论仍可跑通整链路。

---

## 7. Web API 设计

### 7.1 内存模型

- **Session**：绑定一个数据文件，缓存信号与各工况分析结果。
- **Chat**：一次人机对话，含 `files`、`messages`、`selected_function`、`selected_condition`、`selected_profile`。

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
| POST | `/api/chats/{cid}/files` | 上传一个/多个数据文件 |
| DELETE | `/api/chats/{cid}/files/{fid}` | 移除文件 |
| POST | `/api/chats/{cid}/messages` | 发送用户消息 |
| POST | `/api/chats/{cid}/messages/stream` | SSE 流式发送 |
| POST | `/api/chats/{cid}/select` | 统一选择接口：`target_type=function` 或 `condition` |
| GET | `/api/analyses/{analysis_id}` | 单事件样本分析结果 |
| GET | `/api/analyses/{id}/timeseries` | 事件窗口内信号时序 |
| POST | `/api/groups` | 复现性对比统计 |
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

### 8.2 两跳推荐交互

**① 文件上传**

- 点「+」多选文件。
- 文件仅暂存在输入区上方，不立即上传。
- 点发送时先批量上传，再发送文本。

**② 第一跳：功能选择**

- 用户描述后，`recommend_targets` 返回 `hop=function` 或多个功能候选。
- 面板按功能行渲染，一行一个功能。
- 支持点击或输入数字序号确认。
- 功能唯一时自动进入第二跳。

**③ 第二跳：工况选择**

- 在选中功能内精排工况。
- 返回 `hop=condition` 时，面板按工况行渲染。
- 工况唯一时 `hop=resolved`，后端自动分析。
- 工况所属功能声明了 `profiles` 时，展开档位选择（单下拉），选完再确认。

**④ 其他选项**

- 面板底部提供“其他选项”手动输入框。
- 用户可自由描述未列出的目标，确认后作为新用户消息重新走两跳推荐。

**⑤ 确认动作**

- 统一调用 `POST /api/chats/{cid}/select`。
- `target_type=function` 触发第二跳。
- `target_type=condition` 触发分析并追加 `analysis_result`。

### 8.3 结果展示

- 分析结果卡片按 run-sample 分行。
- 每行显示：文件名、事件序号、状态 badge、查看图表、数据链接。
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
functions.yaml   ← 功能清单：分段模板、默认指标、档位列表、知识库章节
     │ 引用
     ▼
conditions.yaml  ← 按 function_key 索引的工况清单：动作/路面/参数 + 指标 list
     │ 引用
     ▼
metrics.yaml     ← 指标模板：工具绑定、category、unit
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
    segmenter: <segmenter_key>
    default_metrics: [<metric_key>, ...]
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
        apply_abs: false
        params: {<tool_param>: <value>}

      # 单边下界
      - key: <metric_key_b>
        ok_range: [<lo>, null]
        apply_abs: false

      # 单边上界，取绝对值后比较
      - key: <metric_key_c>
        ok_range: [null, <hi>]
        apply_abs: true

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
        apply_abs: false
```

字段说明：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `key` | 是 | 指标键 |
| `ok_range` | 否 | `[lo, hi]`，`null` 表示该侧无界；省略表示无硬性规定 |
| `apply_abs` | 否 | 默认 `false`；`true` 表示先对指标值取绝对值再比较 |
| `profile` | 否 | 档位标签；省略表示通用条目 |
| `params` | 否 | 传给指标工具的参数 |

说明：

- 不存在全局 `surfaces` 段。
- 不存在 `surface_defaults` 段。
- 每个工况的阈值与判定条件完全自包含。
- `surface_code` 仅作工况元数据，不驱动任何阈值继承。
- 边界统一为闭区间 `[lo, hi]`，即 `lo <= v <= hi` 为 ok。
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
    description: <说明>
```

不再包含 `better` 字段，方向由 `ok_range` 表达。

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
    apply_abs: bool
    value_used: float | None  # 取绝对值后的判定值
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
    v = abs(value) if entry.get("apply_abs") else value
    lo, hi = entry["ok_range"]
    ok = (lo is None or v >= lo) and (hi is None or v <= hi)
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
                ok_range=None, apply_abs=False,
                value_used=None, profile=profile,
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
            apply_abs=bool(entry.get("apply_abs")),
            value_used=(abs(value) if value is not None and entry.get("apply_abs") else value),
            profile=entry.get("profile"),
        ))
    return results
```

### 10.7 工具契约

所有指标工具统一：

- 输入：`(signals, condition, window, params)`
- 输出：`dict`，主值放在 `"value"` 键，其余中间量自由放置。
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

## 13. 两跳推荐流程

```text
用户 query
  → recommend_targets(query)
     第 1 跳：功能识别
       ├─ 功能唯一 → 自动进入第 2 跳
       └─ 功能多个 → hop=function
            → 前端功能面板
            → POST /select {target_type: function}
            → 第 2 跳

     第 2 跳：工况精排
       ├─ 工况唯一 → hop=resolved
       │    → 后端自动选择并分析
       └─ 工况多个 → hop=condition
            → 前端工况面板
            → POST /select {target_type: condition, profile}
            → 分析
```

`recommend_targets` 返回：

| hop | 含义 | 前端动作 |
| --- | --- | --- |
| `function` | 多个功能候选 | 弹功能面板 |
| `condition` | 某功能内多个工况候选 | 弹工况面板 |
| `resolved` | 功能+工况唯一 | 自动分析 |
| `error` | 未知功能/错误 | 提示兜底 |

规则兜底：

- 功能：同义词命中打分 + 功能下工况名命中加分。
- 工况：关键词打分，动作词加权。
- LLM 失败时自动落到规则打分。

候选规模服务端强剪，上限 `min(max(1, n), 8)`。

---

## 14. 部署与运行

`.env`：

```text
OPENAI_BASE_URL
OPENAI_API_KEY
OPENAI_MODEL
ENABLE_RAG=false
```

CLI：

```bash
brake analyze -f <data_file> -p <signals_config> -c <condition_id> [--profile <profile>]
brake compare -c <condition_id> -f <run1> -f <run2> ... [--profile <profile>]
```

Web：

```bash
python -m uvicorn web.main:app --port 8000
```

依赖分组：

- `core`
- `cli`
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

### 后续扩展

- 更多格式：ASC/ARXML。
- 车型 profile 中心化管理。
- 知识库管理后台。
- 批量/脚本化扫描。
- 导出 PDF/Excel 报告。
- 异步任务化。

### 已排除项

- 同一文件多工况分析。
- 指标级三级预警（warn 下沉到规则层 `severity`）。
- 全局路面默认阈值（阈值工况自包含）。
- 多维 context 匹配（统一为 profile 字符串）。
