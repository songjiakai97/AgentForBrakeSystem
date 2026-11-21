"""报告时序图：信号时序 → matplotlib 静态 PNG（design.md §18）。

方案 A（对话定版）：报告是静态文档，ECharts 的交互在 docx 里无意义；
matplotlib 是唯一 pip 装完即跑、不引入外部进程（无头浏览器 / kaleido）的路径。

分组布局、事件窗口截取与降采样不另起口径：直接复用 charts/html.py 给前端
ECharts 的同一套实现（_groups_from_layout / _series_data），保证报告与页面
看到的通道选择、分组、窗口范围、点数上限完全一致——only 传 functions.yaml
的功能信号清单（ABS 不画 TCS_Active），layout 传 signals.yaml 的 chart_layout。
差异只在「画」这一步：markArea → axvspan，分组 grid → 共享 x 轴的多行子图
（hspace=0 紧贴，观感对齐网页端的多行 grid）。
"""

import re
from typing import List, Optional

from ..charts.html import DEFAULT_SERIES, STATUS_COLORS, _groups_by_unit, _groups_from_layout, _series_data

# ECharts 的 rgba() 字符串 → matplotlib 可用的 RGBA 元组（同一套状态色，不另调色）
_RGBA_RE = re.compile(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([\d.]+)\s*\)")

# 中文字体按平台兜底；一个都找不到时 matplotlib 回退 DejaVu，中文变方框但不报错——
# 报告仍能出图，字体问题属于环境配置而非功能失败
_FONT_CANDIDATES = ["Microsoft YaHei", "SimHei", "PingFang SC",
                    "Noto Sans CJK SC", "WenQuanYi Micro Hei"]

RGBA_COLORS = {
    key: tuple(int(c) / 255.0 for c in _RGBA_RE.match(v).groups()[:3])
         + (float(_RGBA_RE.match(v).group(4)),)
    for key, v in STATUS_COLORS.items() if _RGBA_RE.match(v)
}


def _setup_font() -> None:
    import matplotlib

    matplotlib.rcParams["font.sans-serif"] = _FONT_CANDIDATES + \
        list(matplotlib.rcParams["font.sans-serif"])
    matplotlib.rcParams["axes.unicode_minus"] = False


def render_sample_chart(
    signals: dict,
    window: Optional[tuple],
    layout,
    out_png: str,
    title: str = "",
    highlight_status: str = "ok",
    only: Optional[List[str]] = None,
    dpi: int = 150,
) -> bool:
    """一个事件样本 → 一张多行一列 PNG。画了至少一条测线返回 True。

    口径与网页端 build_chart_option 一致：layout（signals.yaml chart_layout）
    分组，only（functions.yaml 的功能信号清单）过滤通道；无 layout 时对 only
    按单位自动分组。window 传 (t_start, t_end)；无效窗口由调用方负责跳过。
    """
    import matplotlib

    # 报告图只落 PNG，绝不进交互后端：TkAgg 在无头服务器上起不来，
    # 在测试进程里还会被前序用例的 Tk 状态弄挂（全量跑实测踩中）
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    _setup_font()
    # 与 build_chart_option 同一套选择逻辑：布局分组 + 功能通道过滤
    groups = _groups_from_layout(signals, layout, only=only) if layout else []
    if not groups:
        groups = _groups_by_unit(signals, only or DEFAULT_SERIES)
    n = max(1, len(groups))
    # 行高按子图数走：版心宽约 6.7in（A4 去 2cm 边距），每行 1.4in 够放下 y 轴名+图例
    fig, axes = plt.subplots(
        n, 1, sharex=True, squeeze=False,
        figsize=(6.7, 1.4 * n + 0.7),
        gridspec_kw={"hspace": 0},
    )
    plotted = 0
    for i, (label, unit, sigs) in enumerate(groups):
        ax = axes[i][0]
        ax.grid(True, axis="y", alpha=0.3)
        ax.set_ylabel(f"{label}" + (f"\n({unit})" if unit else ""), fontsize=9)
        if window:
            t0, t1 = window
            if t0 is not None and t1 is not None and t0 == t0 and t1 == t1:
                ax.axvspan(float(t0), float(t1),
                           color=RGBA_COLORS.get(highlight_status, RGBA_COLORS["ok"]),
                           zorder=0)
        for name in sigs:
            data = _series_data(signals, name, window)
            if not data:
                continue
            ts = [p[0] for p in data]
            vals = [float("nan") if p[1] is None else p[1] for p in data]
            ax.plot(ts, vals, linewidth=1.0, label=name)
            plotted += 1
        if len(sigs) > 1 and plotted:
            ax.legend(fontsize=7, frameon=False, loc="upper right", ncols=2)
    if not plotted:
        plt.close(fig)
        return False
    axes[-1][0].set_xlabel("s", fontsize=9)
    if title:
        fig.suptitle(title, fontsize=10)
    fig.savefig(out_png, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return True
