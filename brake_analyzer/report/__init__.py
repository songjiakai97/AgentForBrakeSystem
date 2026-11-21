"""分析报告生成（design.md §14）：已落库的分析结果 → Word（docx）报告。

- generator：收集会话内分析结果 → 视图模型 → docxtpl 填充模板；
- charts：事件窗口时序图的 matplotlib 静态渲染（方案 A，见对话记录）；
- 模板本体由 scripts/make_report_template.py 程序化产出后入库，运行期只读。
"""
