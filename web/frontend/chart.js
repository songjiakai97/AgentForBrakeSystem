/* 共享时序图：多行子图 + 共享时间轴。
 *
 * 主页面（分析卡片覆盖层）和记录库页面用的是同一份服务端 option（charts/html.py），
 * 所以渲染行为也必须只有一份实现：以前各写一套，记录库既没有游标也不能缩放。
 *
 * 这里做三件服务端做不到的事（formatter 是函数，进不了 JSON）：
 *   1. 游标读数：**全部可见信号**按行分组贴指针显示。交给 ECharts 的 axis tooltip
 *      时，多行 link 会把 6 行 12 条塞进一个浮层，高出绘图区就被裁——用户看到的是
 *      「这一行少了几条测线」；只列当前行又读不到别的行。自己画浮层 + 两列排版，
 *      既不裁也不漏。
 *   2. X 轴拖拽框选缩放：按下→拖动出半透明遮罩→松开把 x 量程设成选区。
 *      像素用 convertFromPixel 映射成数据坐标；inside dataZoom 的平移关掉，
 *      拖动才是选区而不是平移。
 *   3. 双击复位到全量程。
 */
(function (global) {
  'use strict';

  function num(v) { return typeof v === 'number' ? v : (parseFloat(v) || 0); }

  // grid 的 left/right 是 px 数、top/height 是百分比，两种都要还原成像素
  function px(v, total) {
    if (typeof v === 'string' && v.indexOf('%') >= 0) return num(v) * total / 100;
    return num(v);
  }

  function fmtVal(v) {
    if (v === null || v === undefined || !isFinite(v)) return '—';
    var a = Math.abs(v);
    if (a >= 10000) return String(Math.round(v));
    if (a >= 1) return String(Number(v.toFixed(3)));
    return String(Number(v.toPrecision(3)));
  }

  // 最近邻取点：series.data 是按时间升序的 [t, v]，二分即可
  function nearest(data, t) {
    if (!data || !data.length) return null;
    var lo = 0, hi = data.length - 1;
    while (lo < hi) {
      var mid = (lo + hi) >> 1;
      if (data[mid][0] < t) lo = mid + 1; else hi = mid;
    }
    var i = lo;
    if (i > 0 && Math.abs(data[i - 1][0] - t) <= Math.abs(data[i][0] - t)) i -= 1;
    return data[i];
  }

  function create(el, rawOption, opts) {
    opts = opts || {};
    var option = JSON.parse(JSON.stringify(rawOption || {}));

    option.textStyle = {color: '#e2e8f0'};
    option.backgroundColor = 'transparent';
    (option.xAxis || []).forEach(function (ax) {
      ax.axisLine = {lineStyle: {color: '#334155'}};
      ax.axisLabel = Object.assign({}, ax.axisLabel, {color: '#94a3b8'});
    });
    (option.yAxis || []).forEach(function (ax) {
      ax.nameTextStyle = Object.assign({}, ax.nameTextStyle, {color: '#94a3b8'});
      ax.axisLabel = Object.assign({}, ax.axisLabel, {color: '#94a3b8'});
    });
    (option.series || []).forEach(function (s) {
      s.lineStyle = Object.assign({width: 1.2}, s.lineStyle);
    });
    if (opts.legend) option.legend = opts.legend;

    // 拖动留给框选缩放：inside 只保留滚轮，否则一拖就是平移
    (option.dataZoom || []).forEach(function (z) {
      if (z.type === 'inside') {
        z.zoomOnMouseWheel = true;
        z.moveOnMouseMove = false;
        z.moveOnMouseWheel = false;
      }
    });

    // 读数和游标线都由下面的 DOM 层负责，ECharts 的 tooltip / axisPointer 全关：
    //   - axisPointer 的 y 轴 label 显示的是「鼠标的 y 值」，跟时间序列无关；
    //   - link 之后每个联动行都会再画一个 x label（一屏好几个「3.68」）和标记点，
    //     看起来像「点不在游标 x 上」。
    // 自己画一条竖线才干净：位置只由 x 决定，跨行贯通，不带任何数值标签。
    option.tooltip = {show: false};
    option.axisPointer = {show: false};

    var chart = echarts.init(el, null, {renderer: 'canvas'});
    chart.setOption(option, true);

    if (getComputedStyle(el).position === 'static') el.style.position = 'relative';

    // ---------------------------------------------------------------- 遮罩
    var mask = document.createElement('div');
    mask.style.cssText = 'position:absolute;top:0;left:0;width:0;height:0;display:none;' +
      'background:rgba(56,189,248,.18);border-left:1px solid rgba(56,189,248,.65);' +
      'border-right:1px solid rgba(56,189,248,.65);pointer-events:none;z-index:6;';
    el.appendChild(mask);

    // ---------------------------------------------------------------- 游标竖线
    var cursor = document.createElement('div');
    cursor.style.cssText = 'position:absolute;top:0;left:0;display:none;' +
      'border-left:1px dashed #64748b;pointer-events:none;z-index:5;';
    el.appendChild(cursor);

    function gridSpan() {
      var rs = grids();
      if (!rs.length) return null;
      return {top: rs[0].y, bottom: rs[rs.length - 1].y + rs[rs.length - 1].h};
    }

    function moveCursor(x) {
      var sp = gridSpan();
      if (!sp) return;
      cursor.style.display = 'block';
      cursor.style.left = Math.round(x) + 'px';
      cursor.style.top = sp.top + 'px';
      cursor.style.height = Math.max(1, sp.bottom - sp.top) + 'px';
    }

    function hideCursor() { cursor.style.display = 'none'; }

    // ---------------------------------------------------------------- 读数浮层
    var readout = document.createElement('div');
    readout.style.cssText = 'position:absolute;top:0;left:0;display:none;pointer-events:none;' +
      'z-index:7;font:11px/1.55 ui-monospace,Menlo,Consolas,monospace;white-space:nowrap;' +
      'background:rgba(15,23,42,.9);border:1px solid #334155;border-radius:6px;' +
      'padding:4px 8px;color:#e2e8f0;';
    el.appendChild(readout);

    function grids() {
      var W = chart.getWidth(), H = chart.getHeight();
      return (option.grid || []).map(function (g) {
        var left = px(g.left, W), right = px(g.right, W);
        var top = px(g.top, H), height = px(g.height, H);
        return {x: left, y: top, w: Math.max(0, W - left - right), h: height};
      });
    }

    function rowAt(x, y) {
      var rs = grids();
      for (var i = 0; i < rs.length; i++) {
        var r = rs[i];
        if (x >= r.x - 2 && x <= r.x + r.w + 2 && y >= r.y - 2 && y <= r.y + r.h + 2) return i;
      }
      return null;
    }

    // 像素 → 时间。finder 必须用 gridIndex：多行一列时 xAxisIndex 找不到唯一的
    // cartesian（每行一个 y 轴），convertFromPixel 会直接返回 NaN。
    function timeAt(x, row) {
      var rs = grids();
      var gi = (row === null || row === undefined || !rs[row]) ? 0 : row;
      var r = rs[gi] || {y: 0, h: 1};
      var pt = chart.convertFromPixel({gridIndex: gi}, [x, r.y + r.h / 2]);
      return (pt && isFinite(pt[0])) ? pt[0] : null;
    }

    // 图例勾掉的行不进读数（ECharts 自己的 tooltip 会做这件事，这里手动对齐）。
    // 主页面是按组勾选的，走的是程序化 setOption，不会触发 legendselectchanged，
    // 所以另外暴露 setLegendSelected 由调用方同步——否则勾掉的行游标还在读数。
    var hiddenGroups = {};
    var rowsOf = {};
    (option.series || []).forEach(function (s) {
      rowsOf[s.groupIndex] = rowsOf[s.groupIndex] || [];
      rowsOf[s.groupIndex].push(s.name);
    });

    function applySelection(sel) {
      sel = sel || {};
      Object.keys(rowsOf).forEach(function (gi) {
        hiddenGroups[gi] = rowsOf[gi].every(function (n) { return sel[n] === false; });
      });
    }
    applySelection((option.legend && option.legend.selected) || {});

    chart.on('legendselectchanged', function (e) {
      applySelection(e.selected);
      readout.style.display = 'none';
    });
    chart.setLegendSelected = function (sel) {
      applySelection(sel);
      readout.style.display = 'none';
      if (!chart.isDisposed()) chart.setOption({legend: {selected: sel || {}}});
    };

    var units = {};
    ((option._datasets) || option.series || []).forEach(function (d) {
      if (d && d.name) units[d.name] = d.unit || '';
    });
    var groupLabels = {};
    ((option._groups) || []).forEach(function (g) { groupLabels[g.index] = g.label; });

    // 读数里的色点必须和曲线同色：ECharts 不给 series 指定颜色时按 seriesIndex
    // 顺序取调色板，这里照同一规则取，避免"名字一个色、线另一个色"。
    var palette = null;
    function colorOf(idx) {
      if (!palette) {
        var got = [];
        try { got = chart.getOption().color || []; } catch (e) { got = []; }
        if (!got.length) got = option.color || [];
        if (!got.length) got = ['#5470c6'];
        palette = got;
      }
      return palette[idx % palette.length];
    }

    // 读数列**全部**可见信号（按行分组，两列排），不是只列鼠标那一行：
    // 早期版本只列当前行，用户读不到别的行；改成全部塞进 ECharts tooltip 又被裁。
    // 自己画浮层才能「全都要 + 不被裁」——两列排版把 6 行 12 条压到 ~180px。
    var idxOf = {};
    (option.series || []).forEach(function (s, i) { idxOf[s.name] = i; });

    function showReadout(row, x, y) {
      var t = timeAt(x, row);
      if (t === null || row === null) {
        readout.style.display = 'none';
        hideCursor();
        return;
      }
      var byGroup = {};
      var order = [];
      (option.series || []).forEach(function (s) {
        if (hiddenGroups[s.groupIndex]) return;      // 整行被勾掉
        var p = nearest(s.data, t);
        if (!p) return;
        if (!byGroup[s.groupIndex]) { byGroup[s.groupIndex] = []; order.push(s.groupIndex); }
        var color = colorOf(idxOf[s.name] || 0);
        byGroup[s.groupIndex].push(
          '<span><span style="color:' + color + '">' + s.name + '</span> ' +
          '<b>' + fmtVal(p[1]) + '</b>' +
          (units[s.name] ? ' <span style="opacity:.5">' + units[s.name] + '</span>' : '') + '</span>');
      });
      if (!order.length) {
        readout.style.display = 'none';
        hideCursor();
        return;
      }
      var html = '<div style="opacity:.75;margin-bottom:2px">t = ' + Number(t).toFixed(3) + ' s</div>';
      order.forEach(function (gi) {
        var label = groupLabels[gi] ? groupLabels[gi] : ('#' + (gi + 1));
        html += '<div style="color:#38bdf8;margin-top:2px">' + label + '</div>' +
                '<div style="display:grid;grid-template-columns:1fr 1fr;column-gap:12px">' +
                byGroup[gi].join('') + '</div>';
      });
      readout.innerHTML = html;
      readout.style.display = 'block';
      moveCursor(x);
      // 贴指针显示，靠近右/下边界时翻到另一侧，任何情况下都不出容器
      var ow = readout.offsetWidth, oh = readout.offsetHeight;
      var W = chart.getWidth(), H = chart.getHeight();
      var lx = x + 14, ty = y + 12;
      if (lx + ow > W - 4) lx = Math.max(4, x - ow - 14);
      if (ty + oh > H - 4) ty = Math.max(4, H - oh - 4);
      readout.style.left = lx + 'px';
      readout.style.top = ty + 'px';
    }

    function hideReadout() { readout.style.display = 'none'; hideCursor(); }

    // ---------------------------------------------------------------- 拖拽缩放
    var drag = null;

    function clampX(x) {
      var rs = grids();
      var g = (drag && drag.row !== null && rs[drag.row]) || rs[0];
      if (!g) return x;
      return Math.min(Math.max(x, g.x), g.x + g.w);
    }

    function showMask(x1, x2) {
      var rs = grids();
      if (!rs.length) return;
      var top = rs[0].y, bottom = rs[rs.length - 1].y + rs[rs.length - 1].h;
      var a = Math.min(x1, x2), b = Math.max(x1, x2);
      mask.style.display = 'block';
      mask.style.left = a + 'px';
      mask.style.top = top + 'px';
      mask.style.width = Math.max(1, b - a) + 'px';
      mask.style.height = Math.max(1, bottom - top) + 'px';
    }

    function hideMask() { mask.style.display = 'none'; }

    // 选区里必须有数据：否则缩放后是空白图，看起来像「数据没了」
    function hasDataIn(lo, hi) {
      return (option.series || []).some(function (s) {
        var d = s.data || [];
        return d.length && d[d.length - 1][0] >= lo && d[0][0] <= hi;
      });
    }

    function zoomTo(t0, t1) {
      var lo = Math.min(t0, t1), hi = Math.max(t0, t1);
      if (!(hi > lo) || !hasDataIn(lo, hi)) return false;
      (option.dataZoom || []).forEach(function (z, i) {
        chart.dispatchAction({type: 'dataZoom', dataZoomIndex: i, startValue: lo, endValue: hi});
      });
      return true;
    }

    function resetZoom() {
      (option.dataZoom || []).forEach(function (z, i) {
        chart.dispatchAction({type: 'dataZoom', dataZoomIndex: i, start: 0, end: 100});
      });
    }

    var zr = chart.getZr();
    zr.on('mousemove', function (e) {
      if (drag) {
        drag.x2 = clampX(e.offsetX);
        showMask(drag.x1, drag.x2);
        hideReadout();
        return;
      }
      showReadout(rowAt(e.offsetX, e.offsetY), e.offsetX, e.offsetY);
    });
    zr.on('globalout', function () { hideReadout(); });
    zr.on('mousedown', function (e) {
      var row = rowAt(e.offsetX, e.offsetY);
      if (row === null) return;                 // 滑条/轴标签上不抢事件
      drag = {row: row, x1: clampX(e.offsetX), x2: clampX(e.offsetX)};
      hideReadout();
      showMask(drag.x1, drag.x2);
    });
    zr.on('dblclick', function () { resetZoom(); });

    function onWinMove(ev) {
      if (!drag) return;
      var r = el.getBoundingClientRect();
      drag.x2 = clampX(ev.clientX - r.left);
      showMask(drag.x1, drag.x2);
    }

    function onWinUp(ev) {
      if (!drag) return;
      var d = drag;
      drag = null;
      hideMask();
      var x2 = d.x2;
      if (ev && typeof ev.clientX === 'number') {
        x2 = clampX(ev.clientX - el.getBoundingClientRect().left);
      }
      // 按下就松开（点击）或抖动几像素不算选区，否则会误缩放到一根线
      if (!(Math.abs(x2 - d.x1) >= 4)) return;
      var t0 = timeAt(d.x1, d.row), t1 = timeAt(x2, d.row);
      if (t0 === null || t1 === null) return;
      if (!zoomTo(t0, t1) || !ev) return;
      // 松手后把读数补回来：缩放完还停在空白处会让人以为图没了
      var rect = el.getBoundingClientRect();
      var px = ev.clientX - rect.left, py = ev.clientY - rect.top;
      showReadout(rowAt(px, py), px, py);
    }

    window.addEventListener('mousemove', onWinMove);
    window.addEventListener('mouseup', onWinUp);

    var ro = null;
    if (global.ResizeObserver) {
      ro = new ResizeObserver(function () {
        if (!chart.isDisposed()) chart.resize();
      });
      ro.observe(el);
    }
    chart.resize();

    chart.destroyChart = function () {
      window.removeEventListener('mousemove', onWinMove);
      window.removeEventListener('mouseup', onWinUp);
      if (ro) ro.disconnect();
      if (mask.parentNode) mask.parentNode.removeChild(mask);
      if (cursor.parentNode) cursor.parentNode.removeChild(cursor);
      if (readout.parentNode) readout.parentNode.removeChild(readout);
      if (!chart.isDisposed()) chart.dispose();
    };
    return chart;
  }

  global.ChartKit = {create: create};
})(window);
