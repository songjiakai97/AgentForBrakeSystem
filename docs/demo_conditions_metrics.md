# 工况指标规格（ABS / TCS Demo 范围）

> 本文是 `docs/design.md` 的落地补充：给出首批功能/工况的实际判定指标、信号逻辑名、
> 指标语义与配置草案。后续生成 demo 时，§6 的 YAML 草案可原样落入 `configs/`。
> 原歧义项已于 2026-09-22 确认，结论见 §7。

## 1. 原始需求

| 编号 | 工况 | 指标要求 |
| --- | --- | --- |
| 1.1 | ABS 干沥青 100kph 全力制动 | 最大横摆角速度 < ±5°/s；制动距离 <= 40m |
| 1.2 | ABS 洒水玄武岩 60kph 全力制动 | 最大横摆角速度 < ±5°/s；平均减速度 >= 1.5 |
| 1.3 | ABS 洒水瓷砖 50kph 全力制动 | 最大横摆角速度 < ±5°/s；平均减速度 >= 0.8 |
| 2.1 | TCS 干沥青 0→100kph 全油门加速 | 最大横摆角速度 < ±5°/s；加速时间 <= 10s |
| 2.2 | TCS 洒水玄武岩 0→60kph 全油门加速 | 最大横摆角速度 < ±5°/s；平均加速度 >= 1.6（四驱）/ 0.8（两驱）；打滑量 <= 36（DTCS）/ 54（TCS） |
| 2.3 | TCS 洒水瓷砖 0→50kph 全油门加速 | 最大横摆角速度 < ±5°/s；平均加速度 >= 0.8（四驱）/ 0.4（两驱）；打滑量 <= 36（DTCS）/ 54（TCS） |

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
| `BrakePedalPos` | 制动踏板深度 | % |
| `ThrottlePedalPos` | 油门踏板深度 | % |

## 3. 功能与工况清单

### 3.1 Function

| key | 名称 | segmenter | profiles（档位标签） |
| --- | --- | --- | --- |
| `abs` | ABS 全力制动 | `seg_abs_full_brake` | 无（阈值工况自包含，无档位维度） |
| `tcs` | TCS 全油门加速 | `seg_tcs_full_throttle` | `4WD` `2WD` `DTCS` `TCS` |

### 3.2 Condition

condition_id 命名约定：`{function}_{maneuver}_{surface}_{v0}kph`。表中仅列关键速度参数，完整 `params`（`v_stop_kph` / `v_start_kph` / `pedal_arm_pct`）见 §5 与 §6.2。

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

- 驱动形式：`4WD` / `2WD` —— 只作用于**平均加速度指标**（2.2、2.3）；
- 控制模式：`DTCS` / `TCS` —— 只作用于**打滑量指标**（2.2、2.3）。

落位规则：

1. 档位标签只用单维词，不拼 `4WD_TCS` 复合标签。多档位条目通过**重复条目**表达（design.md §9.3 允许同一 key 多条 profile 条目），两维同时受约束时逐组合展开，例如：

   ```yaml
   - {key: acc_avg,    ok_range: [1.6, null], profile: 4WD}
   - {key: acc_avg,    ok_range: [0.8, null], profile: 2WD}
   - {key: slip_max,   ok_range: [null, 36], profile: DTCS}
   - {key: slip_max,   ok_range: [null, 54], profile: TCS}
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

统一约定：速度交叉点（v0、0.8 kph、v_target）的**线性插值定位由 segmenter 完成**，
写入 `EventWindow` 的 `t_start`/`t_end`；指标工具只在给定窗口内做纯计算，不再自行找交叉点。
这样每个交叉点只插值一次，且各指标共享同一致时间窗。

| metric_key | 工具 | 单位 | 输入信号 | 取值语义 |
| --- | --- | --- | --- | --- |
| `yaw_rate_max` | `max_abs` | °/s | `yaw_rate` | 窗口内 \|yaw_rate\| 最大值 |
| `brake_distance` | `signal_span` | m | `distance_vbox` | 窗口内距离增量 = `d(t_end) − d(t_start)` |
| `decel_avg` | `speed_slope` | m/s² | `speed_vbox` | 窗口速度变化率幅值 `\|v_end − v_start\|/(t_end − t_start)`，恒非负 |
| `acc_time` | `window_duration` | s | `speed_vbox` | 窗口时长 `t_end − t_start` |
| `acc_avg` | `speed_slope` | m/s² | `speed_vbox` | 同 `decel_avg`，恒非负 |
| `slip_max` | `wheel_slip_max` | km/h | `wheelSpeed_FL/FR/RL/RR`, `speed_vbox` | 窗口内 `max(四轮轮速) − speed_vbox` 的最大值 |

工具契约与 design.md §10.7 一致：`(signals, condition, window, params) -> {"value": ...}`，
缺信号或窗口退化（`t_end <= t_start`）返回 `{"value": None}`，由引擎判 missing。
`value` 即最终判定值，引擎不做符号加工：`max_abs` / `speed_slope` 输出幅值（恒非负），
`wheel_slip_max` 保持 km/h，与阈值同单位；`speed_slope` 内部完成 km/h→m/s 换算。

工况 → 指标映射（含阈值，闭区间语义 `lo <= v <= hi` 为 ok）：

| condition_id | metric_key | ok_range | profile |
| --- | --- | --- | --- |
| `abs_..._dry_asphalt_100kph` | `yaw_rate_max` | `[null, 5]` | – |
| | `brake_distance` | `[null, 40]` | – |
| `abs_..._wet_basalt_60kph` | `yaw_rate_max` | `[null, 5]` | – |
| | `decel_avg` | `[1.5, null]` | – |
| `abs_..._wet_tile_50kph` | `yaw_rate_max` | `[null, 5]` | – |
| | `decel_avg` | `[0.8, null]` | – |
| `tcs_..._dry_asphalt_0to100kph` | `yaw_rate_max` | `[null, 5]` | – |
| | `acc_time` | `[null, 10]` | – |
| `tcs_..._wet_basalt_0to60kph` | `yaw_rate_max` | `[null, 5]` | – |
| | `acc_avg` | `[1.6, null]` | `4WD` |
| | `acc_avg` | `[0.8, null]` | `2WD` |
| | `slip_max` | `[null, 36]` | `DTCS` |
| | `slip_max` | `[null, 54]` | `TCS` |
| `tcs_..._wet_tile_0to50kph` | `yaw_rate_max` | `[null, 5]` | – |
| | `acc_avg` | `[0.8, null]` | `4WD` |
| | `acc_avg` | `[0.4, null]` | `2WD` |
| | `slip_max` | `[null, 36]` | `DTCS` |
| | `slip_max` | `[null, 54]` | `TCS` |

## 5. 事件分段模板

交叉点定位一律在 `speed_vbox` 上对相邻样本做线性插值，故窗口端点是亚采样精度的。

| segmenter key | 绑定功能 | `t_start` | `t_end` | anchor |
| --- | --- | --- | --- | --- |
| `seg_abs_full_brake` | abs | `speed_vbox` 下降沿穿越 `v0_kph` 的时刻 | `speed_vbox` 下降沿穿越 `v_stop_kph`（默认 0.8）的时刻 | `t_start` |
| `seg_tcs_full_throttle` | tcs | `speed_vbox` 上升沿穿越 `v_start_kph`（默认 0.8）的时刻 | `speed_vbox` 上升沿穿越 `v_target_kph` 的时刻 | `t_start` |

事件搜索范围（判"这一段是不是一个有效事件"）用踏板信号，但最终窗口由速度交叉点裁剪：

- abs：在 `BrakePedalPos >= pedal_arm_pct`（默认 80%）持续为真的区间内，搜索 v0 → v_stop 的下降穿越对。
- tcs：在 `ThrottlePedalPos >= pedal_arm_pct`（默认 95%）且 `speed_vbox` 单调上升的区间内，搜索 v_start → v_target 的上升穿越对。

边界行为：

- 交叉点任一找不到 → 不产出该 `EventWindow`（或产出后窗口退化），相关指标 missing，规则命中 `data_quality`。
- v0 通常对应 100 kph 工况，但实测常从 105 kph 开始踩刹车；因此 `v0_kph` 取**工况标称初速**（100/60/50），从该点起算，不取数据段起点。
- 同一文件内可检出多个事件，每个为一个 run-sample。

参数默认值集中在此，工况 `params` 可覆盖：

| params key | 默认 | 说明 |
| --- | --- | --- |
| `v_stop_kph` | 0.8 | 制动终止速度 |
| `v_start_kph` | 0.8 | 加速起始速度 |
| `pedal_arm_pct` | abs 80 / tcs 95 | 踏板激活判据，单位 % |

`ABS_Active` / `TCS_Active` 不参与分段与指标计算，作为激活校验的证据信号（后续可作为 info 类指标扩展）。

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
      v_stop_kph: 0.8
      pedal_arm_pct: 80
    metrics:
      - {key: yaw_rate_max,   ok_range: [null, 5]}
      - {key: brake_distance, ok_range: [null, 40]}

  - id: abs_full_brake_wet_basalt_60kph
    name: ABS 洒水玄武岩 60kph 全力制动
    maneuver: full_brake
    surface_code: wet_basalt
    params:
      v0_kph: 60
      v_stop_kph: 0.8
      pedal_arm_pct: 80
    metrics:
      - {key: yaw_rate_max, ok_range: [null, 5]}
      - {key: decel_avg,    ok_range: [1.5, null]}

  - id: abs_full_brake_wet_tile_50kph
    name: ABS 洒水瓷砖 50kph 全力制动
    maneuver: full_brake
    surface_code: wet_tile
    params:
      v0_kph: 50
      v_stop_kph: 0.8
      pedal_arm_pct: 80
    metrics:
      - {key: yaw_rate_max, ok_range: [null, 5]}
      - {key: decel_avg,    ok_range: [0.8, null]}

tcs:
  - id: tcs_full_throttle_dry_asphalt_0to100kph
    name: TCS 干沥青 0→100kph 全油门加速
    maneuver: full_throttle
    surface_code: dry_asphalt
    params:
      v_start_kph: 0.8
      v_target_kph: 100
      pedal_arm_pct: 95
    metrics:
      - {key: yaw_rate_max, ok_range: [null, 5]}
      - {key: acc_time,     ok_range: [null, 10]}

  - id: tcs_full_throttle_wet_basalt_0to60kph
    name: TCS 洒水玄武岩 0→60kph 全油门加速
    maneuver: full_throttle
    surface_code: wet_basalt
    params:
      v_start_kph: 0.8
      v_target_kph: 60
      pedal_arm_pct: 95
    metrics:
      - {key: yaw_rate_max, ok_range: [null, 5]}
      - {key: acc_avg,      ok_range: [1.6, null], profile: 4WD}
      - {key: acc_avg,      ok_range: [0.8, null], profile: 2WD}
      - {key: slip_max,     ok_range: [null, 36], profile: DTCS}
      - {key: slip_max,     ok_range: [null, 54], profile: TCS}

  - id: tcs_full_throttle_wet_tile_0to50kph
    name: TCS 洒水瓷砖 0→50kph 全油门加速
    maneuver: full_throttle
    surface_code: wet_tile
    params:
      v_start_kph: 0.8
      v_target_kph: 50
      pedal_arm_pct: 95
    metrics:
      - {key: yaw_rate_max, ok_range: [null, 5]}
      - {key: acc_avg,      ok_range: [0.8, null], profile: 4WD}
      - {key: acc_avg,      ok_range: [0.4, null], profile: 2WD}
      - {key: slip_max,     ok_range: [null, 36], profile: DTCS}
      - {key: slip_max,     ok_range: [null, 54], profile: TCS}
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
    tool: signal_span
    category: performance
    unit: m
    inputs: [distance_vbox]
    description: 窗口终止与起始的 vbox 距离之差（v0_kph → v_stop_kph 制动距离）

  - key: decel_avg
    tool: speed_slope
    category: performance
    unit: m/s²
    inputs: [speed_vbox]
    description: 窗口内车速变化率幅值（恒非负），与平均减速度/平均加速度阈值直接比较

  - key: acc_time
    tool: window_duration
    category: performance
    unit: s
    inputs: [speed_vbox]
    description: 窗口时长，即 v_start_kph → v_target_kph 加速时间

  - key: acc_avg
    tool: speed_slope
    category: performance
    unit: m/s²
    inputs: [speed_vbox]
    description: 窗口内平均加速度（与 decel_avg 同工具、同为非负幅值，仅阈值方向不同）

  - key: slip_max
    tool: wheel_slip_max
    category: traction
    unit: km/h
    inputs: [wheelSpeed_FL, wheelSpeed_FR, wheelSpeed_RL, wheelSpeed_RR, speed_vbox]
    description: 窗口内 max(四轮轮速) − 实际车速 的最大值（打滑量）
```

`Ax`、`ABS_Active`、`TCS_Active` 已在 signals 中定义但本期无指标绑定：`Ax` 与
`speed_slope` 的积分口径等价，故未重复设指标；两个激活信号留作规则佐证与图表叠加。

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
- {id: tcs_acc_abnormal,   scope: condition, function: tcs, condition: tcs_full_throttle_wet_basalt_0to60kph,   metric: acc_avg, status: abnormal, severity: high, fault_domain: traction, kb_ref: tcs_manual, message: 玄武岩加速能力不足，提高允许滑移率目标}
- {id: tcs_slip_abnormal,  scope: condition, function: tcs, condition: tcs_full_throttle_wet_basalt_0to60kph,   metric: slip_max,   status: abnormal, severity: high, fault_domain: traction, kb_ref: tcs_manual, message: 玄武岩打滑量超限，收紧扭矩爬升或提前滑移干预}
- {id: tcs_acc_tile_abnormal, scope: condition, function: tcs, condition: tcs_full_throttle_wet_tile_0to50kph, metric: acc_avg, status: abnormal, severity: high, fault_domain: traction, kb_ref: tcs_manual, message: 瓷砖加速能力不足，提高允许滑移率目标}
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
  BrakePedalPos:   {description: 制动踏板深度,  unit: "%",   candidates: [BrakePedalPos]}
  ThrottlePedalPos: {description: 油门踏板深度, unit: "%",   candidates: [ThrottlePedalPos]}
```

BLF 侧（`signals_blf.yaml`）待真实 DBC 到位后填写 `[dbc_alias, msg_id, signal_name]` 映射，逻辑名保持不变。

## 7. 指标口径确认结论（2026-09-22）

原歧义项已全部确认，以下为定稿口径：

| # | 项目 | 定稿 |
| --- | --- | --- |
| ① | 平均减速度（1.2 / 1.3） | 单位 **m/s²**；由 `speed_slope` 在 [v0_kph, v_stop_kph] 窗口上算 `\|Δv\|/Δt`，输出即幅值 |
| ② | 平均加速度（2.2 / 2.3） | 定义为**平均加速度**，单位 m/s²，非 1s 滑动峰值；同用 `speed_slope`，metric_key 为 `acc_avg` |
| ③ | 打滑量 | 定稿：**max(四轮轮速) − speed_vbox 的窗口最大值**，单位 km/h；与驱动形式无关 |
| ④ | 制动距离（1.1） | 起点 = `speed_vbox` **下降穿越工况标称初速 v0_kph**（100 kph 工况即使实际从 ~105 kph 才踩刹车，也从 100 kph 起算）；终点 = **下降穿越 0.8 kph**；两交叉点均线性插值；值为窗口内 `distance_vbox` 增量 |
| ⑤ | 加速时间（2.1） | 起点 = `speed_vbox` **上升穿越 0.8 kph**，终点 = 上升穿越 `v_target_kph`，线性插值；值为窗口时长 |
| ⑥ | 单位换算 | 打滑量保持 km/h；`speed_slope` 内部 km/h→m/s 换算后再除时间 |
| ⑦ | 两维 profile | 配置保持单维标签，引擎 pick 侧做子串匹配（见 §3.3-3） |

踏板信号 `BrakePedalPos` / `ThrottlePedalPos` 单位为 **%**（0~100 即 0%~100%），
只用于事件有效性判据（§5），不进入指标数值计算。

## 8. Demo 验收口径

1. 六工况各造 1 条合成 MF4 数据：正常样本 + 每条指标越限样本各一份，走 Web 链路
   （上传文件 → 两跳推荐 → `/select` → 分析结果卡片），状态判定与 §4 表一致。
2. 合成数据须覆盖 §5 的交叉点场景：ABS 数据初速高于标称 v0（如 105 kph 起踩），
   验证制动距离确实从 v0_kph 插值点起算而非数据起点；TCS 数据含起步前的静止段，
   验证计时起点为 0.8 kph 上升穿越。
3. 2.2 分别选 `4WD` / `2WD` / `DTCS` / `TCS` 档位各跑一次，验证 pick 回退与 §3.3 两维匹配。
4. 无 `OPENAI_API_KEY` 环境下六工况全部跑通（纯规则链路）。
5. 信号缺失注入（删除 `yaw_rate` 通道）→ 状态 missing、规则命中 `data_quality`。
6. 交叉点不可达注入（车速从未到 100 kph 或从未降到 0.8 kph）→ 窗口退化、指标 missing。

