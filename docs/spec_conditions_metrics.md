# 工况指标规格（ABS / TCS Demo 范围）

> 本文是 `docs/design.md` 的落地补充：给出首批功能/工况的实际判定指标、信号逻辑名、
> 指标语义与配置草案。后续生成 demo 时，§6 的 YAML 草案可原样落入 `configs/`，
> 语义歧义先看 §7 的标注。

## 1. 原始需求

| 编号 | 工况 | 指标要求 |
| --- | --- | --- |
| 1.1 | ABS 干沥青 100kph 全力制动 | 最大横摆角速度 < ±5°/s；制动距离 <= 40m |
| 1.2 | ABS 洒水玄武岩 60kph 全力制动 | 最大横摆角速度 < ±5°/s；平均减速度 >= 1.5 |
| 1.3 | ABS 洒水瓷砖 50kph 全力制动 | 最大横摆角速度 < ±5°/s；平均减速度 >= 0.8 |
| 2.1 | TCS 干沥青 0→100kph 全油门加速 | 最大横摆角速度 < ±5°/s；加速时间 <= 10s |
| 2.2 | TCS 洒水玄武岩 0→60kph 全油门加速 | 最大横摆角速度 < ±5°/s；加速度 >= 1.6（四驱）/ 0.8（两驱）；打滑量 <= 36（DTCS）/ 54（TCS） |
| 2.3 | TCS 洒水瓷砖 0→50kph 全油门加速 | 最大横摆角速度 < ±5°/s；加速度 >= 0.8（四驱）/ 0.4（两驱）；打滑量 <= 36（DTCS）/ 54（TCS） |

## 2. 信号逻辑名

| 逻辑名 | 含义 | 单位 |
| --- | --- | --- |
| `speed_vbox` | vbox 测试的实际车速 | km/h |
| `distance_vbox` | vbox 测试的距离 | m |
| `wheelSpeed_FL` / `wheelSpeed_FR` / `wheelSpeed_RL` / `wheelSpeed_RR` | 轮速 | km/h |
| `yaw_rate` | 横摆角速度 | °/s |
| `Ax` | 纵向加速度 | m/s² |
| `ABS_Active` | ABS 激活信号，0 未激活 / 1 激活 | – |
| `TCS_Active` | TCS 激活信号，0 未激活 / 1 激活 | – |
| `BrakePedalPos` | 制动踏板深度 | 0~100 |
| `ThrottlePedalPos` | 油门踏板深度 | 0~100 |

## 3. 功能与工况清单

### 3.1 Function

| key | 名称 | segmenter | profiles（档位标签） |
| --- | --- | --- | --- |
| `abs` | ABS 全力制动 | `seg_abs_full_brake` | 无（阈值工况自包含，无档位维度） |
| `tcs` | TCS 全油门加速 | `seg_tcs_full_throttle` | `4WD` `2WD` `DTCS` `TCS` |

### 3.2 Condition

condition_id 命名约定：`{function}_{maneuver}_{surface}_{v0}kph`。

| condition_id | 名称 | maneuver | surface_code | params |
| --- | --- | --- | --- | --- |
| `abs_full_brake_dry_asphalt_100kph` | ABS 干沥青 100kph 全力制动 | `full_brake` | `dry_asphalt` | `v0_kph: 100` |
| `abs_full_brake_wet_basalt_60kph` | ABS 洒水玄武岩 60kph 全力制动 | `full_brake` | `wet_basalt` | `v0_kph: 60` |
| `abs_full_brake_wet_tile_50kph` | ABS 洒水瓷砖 50kph 全力制动 | `full_brake` | `wet_tile` | `v0_kph: 50` |
| `tcs_full_throttle_dry_asphalt_0to100kph` | TCS 干沥青 0→100kph 全油门加速 | `full_throttle` | `dry_asphalt` | `v_target_kph: 100` |
| `tcs_full_throttle_wet_basalt_0to60kph` | TCS 洒水玄武岩 0→60kph 全油门加速 | `full_throttle` | `wet_basalt` | `v_target_kph: 60` |
| `tcs_full_throttle_wet_tile_0to50kph` | TCS 洒水瓷砖 0→50kph 全油门加速 | `full_throttle` | `wet_tile` | `v_target_kph: 50` |

### 3.3 Profile 落位（关键决策）

需求里存在两个**相互独立**的档位维度：

- 驱动形式：`4WD` / `2WD` —— 只作用于**加速度指标**（2.2、2.3）；
- 控制模式：`DTCS` / `TCS` —— 只作用于**打滑量指标**（2.2、2.3）。

落位规则：

1. 档位标签只用单维词，不拼 `4WD_TCS` 复合标签。多档位条目通过**重复条目**表达（design.md §9.3 允许同一 key 多条 profile 条目），两维同时受约束时逐组合展开，例如：

   ```yaml
   - {key: acc_1s_max, ok_range: [1.6, null], profile: 4WD}
   - {key: acc_1s_max, ok_range: [0.8, null], profile: 2WD}
   - {key: slip_max,  ok_range: [null, 36], profile: DTCS}
   - {key: slip_max,  ok_range: [null, 54], profile: TCS}
   ```

2. 未声明 profile 的指标条目（通用条目）在任何档位下都生效——横摆角速度即如此。
3. **待 demo 验证的引擎缺口**：一次分析只带一个 `profile` 标量，而 2.2/2.3 需要
   `4WD`+`DTCS` 两维同时生效。两种解法：
   - 引擎侧把单维标签**子串匹配**进复合标签（选 `4WD_DTCS` 时，profile 为 `4WD`
     和 `DTCS` 的条目都命中）——改动最小，推荐；
   - 或按 design.md §2.2 原生的复合标签自由拼装（配置膨胀，需展开全部组合）。
4. 同一 key 的多个 profile 条目全部不匹配、又无通用条目时按 design.md 判 missing；
   demo 数据应始终显式传 profile，避免踩这条路径。

## 4. 指标定义（metrics.yaml 级）

| metric_key | 工具 | 单位 | 输入信号 | 取值语义 |
| --- | --- | --- | --- | --- |
| `yaw_rate_max` | `max_abs` | °/s | `yaw_rate` | 窗口内 \|yaw_rate\| 的最大值（`apply_abs: true`，判定取绝对值） |
| `brake_distance` | `distance_span` | m | `distance_vbox` | 窗口内 distance_vbox 的增量（末值 − 首值） |
| `decel_avg` | `mean_over_window` | m/s² | `Ax` | 窗口内 \|Ax\| 的时间平均（减速段 Ax 为负，取绝对值后比较） |
| `acc_time` | `rise_time` | s | `speed_vbox` | 段起点（油门全按下）至车速首次达到 `v_target_kph` 的用时 |
| `acc_1s_max` | `sliding_mean_max` | m/s² | `Ax`, `speed_vbox` | 窗口内 1s 滑动平均 Ax 的最大值（见 §7-②） |
| `slip_max` | `wheel_slip_max` | km/h | `wheelSpeed_*`, `speed_vbox` | max(各轮轮速) − speed_vbox 的窗口最大值（见 §7-③） |

工况 → 指标映射（含阈值，闭区间语义 `lo <= v <= hi` 为 ok）：

| condition_id | metric_key | ok_range | apply_abs | profile |
| --- | --- | --- | --- | --- |
| `abs_..._dry_asphalt_100kph` | `yaw_rate_max` | `[null, 5]` | true | – |
| | `brake_distance` | `[null, 40]` | false | – |
| `abs_..._wet_basalt_60kph` | `yaw_rate_max` | `[null, 5]` | true | – |
| | `decel_avg` | `[1.5, null]` | true | – |
| `abs_..._wet_tile_50kph` | `yaw_rate_max` | `[null, 5]` | true | – |
| | `decel_avg` | `[0.8, null]` | true | – |
| `tcs_..._dry_asphalt_0to100kph` | `yaw_rate_max` | `[null, 5]` | true | – |
| | `acc_time` | `[null, 10]` | false | – |
| `tcs_..._wet_basalt_0to60kph` | `yaw_rate_max` | `[null, 5]` | true | – |
| | `acc_1s_max` | `[1.6, null]` | false | `4WD` |
| | `acc_1s_max` | `[0.8, null]` | false | `2WD` |
| | `slip_max` | `[null, 36]` | false | `DTCS` |
| | `slip_max` | `[null, 54]` | false | `TCS` |
| `tcs_..._wet_tile_0to50kph` | `yaw_rate_max` | `[null, 5]` | true | – |
| | `acc_1s_max` | `[0.8, null]` | false | `4WD` |
| | `acc_1s_max` | `[0.4, null]` | false | `2WD` |
| | `slip_max` | `[null, 36]` | false | `DTCS` |
| | `slip_max` | `[null, 54]` | false | `TCS` |

## 5. 事件分段模板

| segmenter key | 绑定功能 | 起点 | anchor | 终点 |
| --- | --- | --- | --- | --- |
| `seg_abs_full_brake` | abs | `BrakePedalPos >= 80` 的首个时刻 | 同起点 | 车速降至 ≈ 0（`speed_vbox < 1`） |
| `seg_tcs_full_throttle` | tcs | 起步（`speed_vbox < 1`）且 `ThrottlePedalPos >= 95` 的首个时刻 | 同起点 | `speed_vbox >= v_target_kph` 或有效事件内首次回落 |

- 起点判据的阈值（80 / 95 / 1 km/h）为工程默认值，demo 阶段可在 params 覆盖。
- `ABS_Active` / `TCS_Active` 不参与分段，作为窗口内激活校验的证据信号（见 rules）。

## 6. 配置草案

### 6.1 configs/functions.yaml

```yaml
functions:
  - key: abs
    name: ABS 全力制动
    aliases: [ABS, 防抱死, 全力制动, 紧急制动, 制动距离, 低附制动]
    segmenter: seg_abs_full_brake
    default_metrics: [yaw_rate_max]
    kb_section: abs_manual
    description: 高/低附着路面的初速全力制动，评价横摆稳定性、制动距离与减速度能力

  - key: tcs
    name: TCS 全油门加速
    aliases: [TCS, DTCS, 牵引力控制, 防滑, 全油门加速, 加速时间, 打滑]
    segmenter: seg_tcs_full_throttle
    default_metrics: [yaw_rate_max]
    profiles: [4WD, 2WD, DTCS, TCS]
    kb_section: tcs_manual
    description: 低附路面起步全油门加速，评价横摆稳定性、加速能力与驱动轮打滑量
```

### 6.2 configs/conditions.yaml

```yaml
abs:
  - id: abs_full_brake_dry_asphalt_100kph
    name: ABS 干沥青 100kph 全力制动
    maneuver: full_brake
    surface_code: dry_asphalt
    params:
      v0_kph: 100
    metrics:
      - {key: yaw_rate_max,  ok_range: [null, 5],  apply_abs: true}
      - {key: brake_distance, ok_range: [null, 40], apply_abs: false}

  - id: abs_full_brake_wet_basalt_60kph
    name: ABS 洒水玄武岩 60kph 全力制动
    maneuver: full_brake
    surface_code: wet_basalt
    params:
      v0_kph: 60
    metrics:
      - {key: yaw_rate_max, ok_range: [null, 5], apply_abs: true}
      - {key: decel_avg,    ok_range: [1.5, null], apply_abs: true}

  - id: abs_full_brake_wet_tile_50kph
    name: ABS 洒水瓷砖 50kph 全力制动
    maneuver: full_brake
    surface_code: wet_tile
    params:
      v0_kph: 50
    metrics:
      - {key: yaw_rate_max, ok_range: [null, 5], apply_abs: true}
      - {key: decel_avg,    ok_range: [0.8, null], apply_abs: true}

tcs:
  - id: tcs_full_throttle_dry_asphalt_0to100kph
    name: TCS 干沥青 0→100kph 全油门加速
    maneuver: full_throttle
    surface_code: dry_asphalt
    params:
      v_target_kph: 100
    metrics:
      - {key: yaw_rate_max, ok_range: [null, 5],  apply_abs: true}
      - {key: acc_time,     ok_range: [null, 10], apply_abs: false}

  - id: tcs_full_throttle_wet_basalt_0to60kph
    name: TCS 洒水玄武岩 0→60kph 全油门加速
    maneuver: full_throttle
    surface_code: wet_basalt
    params:
      v_target_kph: 60
    metrics:
      - {key: yaw_rate_max, ok_range: [null, 5], apply_abs: true}
      - {key: acc_1s_max, ok_range: [1.6, null], profile: 4WD}
      - {key: acc_1s_max, ok_range: [0.8, null], profile: 2WD}
      - {key: slip_max,   ok_range: [null, 36], profile: DTCS}
      - {key: slip_max,   ok_range: [null, 54], profile: TCS}

  - id: tcs_full_throttle_wet_tile_0to50kph
    name: TCS 洒水瓷砖 0→50kph 全油门加速
    maneuver: full_throttle
    surface_code: wet_tile
    params:
      v_target_kph: 50
    metrics:
      - {key: yaw_rate_max, ok_range: [null, 5], apply_abs: true}
      - {key: acc_1s_max, ok_range: [0.8, null], profile: 4WD}
      - {key: acc_1s_max, ok_range: [0.4, null], profile: 2WD}
      - {key: slip_max,   ok_range: [null, 36], profile: DTCS}
      - {key: slip_max,   ok_range: [null, 54], profile: TCS}
```

### 6.3 configs/metrics.yaml

```yaml
metrics:
  - key: yaw_rate_max
    tool: max_abs
    category: stability
    unit: °/s
    inputs: [yaw_rate]
    description: 事件窗口内横摆角速度绝对值峰值

  - key: brake_distance
    tool: distance_span
    category: performance
    unit: m
    inputs: [distance_vbox]
    description: 事件窗口内 vbox 距离增量（制动距离）

  - key: decel_avg
    tool: mean_over_window
    category: performance
    unit: m/s²
    inputs: [Ax]
    description: 事件窗口内纵向加速度绝对值的时间平均（平均减速度）

  - key: acc_time
    tool: rise_time
    category: performance
    unit: s
    inputs: [speed_vbox]
    description: 段起点至车速首次达到工况目标速度的用时

  - key: acc_1s_max
    tool: sliding_mean_max
    category: performance
    unit: m/s²
    inputs: [Ax, speed_vbox]
    description: 窗口内 1s 滑动平均纵向加速度的最大值

  - key: slip_max
    tool: wheel_slip_max
    category: traction
    unit: km/h
    inputs: [wheelSpeed_FL, wheelSpeed_FR, wheelSpeed_RL, wheelSpeed_RR, speed_vbox]
    description: 窗口内 max(四轮轮速) − 实际车速 的最大值（打滑量）
```

### 6.4 configs/rules.yaml

```yaml
# 功能级：两功能共用同一组稳定性判定
- {id: abs_yaw_abnormal,   scope: function, function: abs, metric: yaw_rate_max, status: abnormal, severity: critical,  fault_domain: stability,     kb_ref: abs_manual,  message: 制动过程横摆超限，车辆稳定性不足，检查前后轴制动力分配与介入时序}
- {id: abs_yaw_missing,    scope: function, function: abs, metric: yaw_rate_max, status: missing,  severity: low,       fault_domain: data_quality, kb_ref: abs_manual,  message: yaw_rate 信号缺失，稳定性判定不成立}
- {id: tcs_yaw_abnormal,   scope: function, function: tcs, metric: yaw_rate_max, status: abnormal, severity: critical,  fault_domain: stability,     kb_ref: tcs_manual,  message: 加速过程横摆超限，检查单轮制动干预与驱动扭矩爬升速率}
- {id: tcs_yaw_missing,    scope: function, function: tcs, metric: yaw_rate_max, status: missing,  severity: low,       fault_domain: data_quality, kb_ref: tcs_manual,  message: yaw_rate 信号缺失，稳定性判定不成立}

# 工况级：指标异常 → 具体标定域结论
- {id: abs_dist_abnormal,  scope: condition, function: abs, condition: abs_full_brake_dry_asphalt_100kph, metric: brake_distance, status: abnormal, severity: high, fault_domain: brake_performance, kb_ref: abs_manual, message: 高附着制动距离超限，检查增压响应与初始夹紧力}
- {id: abs_decel_abnormal, scope: condition, function: abs, condition: abs_full_brake_wet_basalt_60kph,   metric: decel_avg,      status: abnormal, severity: high, fault_domain: brake_performance, kb_ref: abs_manual, message: 低附平均减速度不足，放宽 ABS 压力降幅/延长降压窗口}
- {id: abs_decel_tile_abnormal, scope: condition, function: abs, condition: abs_full_brake_wet_tile_50kph, metric: decel_avg,     status: abnormal, severity: high, fault_domain: brake_performance, kb_ref: abs_manual, message: 瓷砖面减速度不足，降低介入压力台阶并检查轮速判据灵敏度}
- {id: tcs_time_abnormal,  scope: condition, function: tcs, condition: tcs_full_throttle_dry_asphalt_0to100kph, metric: acc_time,  status: abnormal, severity: high, fault_domain: powertrain_interaction, kb_ref: tcs_manual, message: 0-100 加速时间超限，检查 TCS 是否过早限制扭矩}
- {id: tcs_acc_abnormal,   scope: condition, function: tcs, condition: tcs_full_throttle_wet_basalt_0to60kph,   metric: acc_1s_max, status: abnormal, severity: high, fault_domain: traction, kb_ref: tcs_manual, message: 玄武岩加速能力不足，提高允许滑移率目标}
- {id: tcs_slip_abnormal,  scope: condition, function: tcs, condition: tcs_full_throttle_wet_basalt_0to60kph,   metric: slip_max,   status: abnormal, severity: high, fault_domain: traction, kb_ref: tcs_manual, message: 玄武岩打滑量超限，收紧扭矩爬升或提前滑移干预}
- {id: tcs_acc_tile_abnormal, scope: condition, function: tcs, condition: tcs_full_throttle_wet_tile_0to50kph, metric: acc_1s_max, status: abnormal, severity: high, fault_domain: traction, kb_ref: tcs_manual, message: 瓷砖加速能力不足，提高允许滑移率目标}
- {id: tcs_slip_tile_abnormal, scope: condition, function: tcs, condition: tcs_full_throttle_wet_tile_0to50kph, metric: slip_max,  status: abnormal, severity: high, fault_domain: traction, kb_ref: tcs_manual, message: 瓷砖打滑量超限，收紧扭矩爬升或提前滑移干预}
```

### 6.5 configs/signals_mf4.yaml（demo 用）

```yaml
# demo：物理通道名 = 逻辑名（合成数据按此命名写入 MF4）
# 接入真实数据时，只需在 candidates 前面追加真实通道名
signal_maps:
  speed_vbox:      {description: vbox 实际车速, unit: km/h, candidates: [speed_vbox]}
  distance_vbox:   {description: vbox 距离,     unit: m,    candidates: [distance_vbox]}
  wheelSpeed_FL:   {description: 左前轮速,      unit: km/h, candidates: [wheelSpeed_FL]}
  wheelSpeed_FR:   {description: 右前轮速,      unit: km/h, candidates: [wheelSpeed_FR]}
  wheelSpeed_RL:   {description: 左后轮速,      unit: km/h, candidates: [wheelSpeed_RL]}
  wheelSpeed_RR:   {description: 右后轮速,      unit: km/h, candidates: [wheelSpeed_RR]}
  yaw_rate:        {description: 横摆角速度,    unit: deg/s, candidates: [yaw_rate]}
  Ax:              {description: 纵向加速度,    unit: m/s2,  candidates: [Ax]}
  ABS_Active:      {description: ABS 激活,      unit: "",    candidates: [ABS_Active]}
  TCS_Active:      {description: TCS 激活,      unit: "",    candidates: [TCS_Active]}
  BrakePedalPos:   {description: 制动踏板深度,  unit: "",    candidates: [BrakePedalPos]}
  ThrottlePedalPos: {description: 油门踏板深度, unit: "",    candidates: [ThrottlePedalPos]}
```

BLF 侧（`signals_blf.yaml`）待真实 DBC 到位后填写 `[dbc_alias, msg_id, signal_name]` 映射，逻辑名保持不变。

## 7. 语义歧义标注（生成 demo 前确认）

| # | 位置 | 歧义 | 本文档采用 |
| --- | --- | --- | --- |
| ① | 1.2/1.3「平均减速度」 | 未给单位 | **m/s²**（按时间均值 Δv/Δt；若按距离均方根 v₀²/2d 口径需另定工具）；若实为 g 单位，阈值改为 `[0.153] / [0.082]` |
| ② | 2.2/2.3「加速度」 | 瞬时还是平均 | **1s 滑动平均 Ax 的最大值**；若应为窗口均值，直接换绑 `decel_avg` 工具 |
| ③ | 2.2/2.3「打滑量」 | 参照哪个轮、名义速度怎么算 | **max(四轮轮速) − speed_vbox 的窗口最大值**，与车型驱动形式无关；若只统计驱动轮，params 加 `axle: front/rear` |
| ④ | 1.1「制动距离 <= 40m」 | 起点是踩踏板还是车辆停稳 | `distance_vbox` 在 [踏板判据点, 停稳] 窗口内的增量 |
| ⑤ | 2.1「加速时间 <= 10s」 | 从起步计时还是含反应时间 | 从段起点（油门全判据按下）计时至首次达到 100 km/h |
| ⑥ | 单位换算 | 车速 km/h、加速度 m/s² 并存 | 打滑量以 km/h 计（轮速−车速）；涉及运动学的工具内部统一换算 m/s |
| ⑦ | 两维 profile | 单 profile 标量 vs 两维同时生效 | 见 §3.3-3：demo 引擎实现子串匹配，配置保持单维标签 |

## 8. Demo 验收口径

1. 六工况各造 1 条合成数据（MF4）：含正常样本 + 每条指标越限样本，端到端跑
   `brake analyze`，状态判定与 §4 表一致。
2. 2.2 用 `--profile 4WD` / `2WD` / `DTCS` / `TCS` 各跑一次，验证 pick 回退与 §3.3 两维匹配。
3. 无 `OPENAI_API_KEY` 环境下六工况全部跑通（纯规则链路）。
4. 信号缺失注入（删除 yaw_rate 通道）→ 状态 missing、规则命中 data_quality。
