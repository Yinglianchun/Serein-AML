# 从交错对话到可追溯 Event

**持续归线、延迟结算与来源归属的系统案例研究**

**ChiYouyu · Haven**
中文 v0.19 · 2026-09-27 · 仓库阅读副本。

> 此目录是 Serein-AML 携带的公开论文阅读副本，来自 Serein 公共仓库；源码快照与文件清单记录在 [`upstream-serein.json`](../../upstream-serein.json)。论文的历史实验、作者署名及结论未改写为比赛结果。当前 AML 路径与简化范围见 [AML 流程说明](../aml-flow.md)。

[阅读 PDF](pdf/event-memory-paper.zh-CN.pdf) · [阅读 Markdown](manuscript.zh-CN.md) · [补充表格 PDF](pdf/event-memory-supplementary-tables.zh-CN.pdf) · [补充表格 Markdown](supplementary-tables.md)

论文讨论三个问题：交错消息怎样持续归入已有经历，为什么经历边界需要延迟结算，以及来源角色和续接关系怎样被保留并接受追溯。案例同时报告来源保留、误收、角色丢失与中断；自动摘要不能保证完全准确，手动修订后的结果不计作自动生成正确。

当前 PDF 入口指向随仓库保存的阅读版，封面署名为 **ChiYouyu · Haven**。

本次更新以公开仓库提交 `26ffe639a17348079c7bba9f4c496eca063ff180` 为实现核对基点，补充阅读范围、显式来源归属、正文材料取舍、逐图转录和失败恢复，更新图 1。E1–E10 的条件、计数和结论保留；没有新增模型实验、部署核验或准确率成绩。§3.8 区分 9 月历史实现与当前公开版，§6.3 列出新机制待验证的问题。文件名带 v0.18 的旧 PDF 保留为历史副本，当前版本使用上方稳定入口。

## 与公开版的关系

论文报告开发中的历史实现及 E1–E10 各自的执行条件，不是当前发行版的全套功能验收。文中的 Bridge、历史 Ombre-Brain 后端、部署日期与研究记录保留原意。**公开版及 AML 的自动 Event 直接保存到 Serein，不进入 Bridge 收件箱**；公开版设计参见[所导入文档快照的自动 Event 说明](https://github.com/Yinglianchun/Serein/blob/c673cb6141e1d1122b8dcb1f8ebb66b8d8c857c5/docs/automatic-events.md)，比赛版配置以本仓库 [README](../../README.md) 为准。

## 本目录包含什么

- 中文正文，保留原有结论、实验计数和参考文献。
- 图 1：处理职责；图 2：合成案例中的共享来源与跨日续接。
- 表 S1–S5：研究问题、对象、动作、案例与执行范围。
- PDF 生成脚本及矢量图，用于重建阅读版。

历史附录、运行回执、研究 ZIP、原始请求与回答、私人聊天和截图没有复制进本目录。未随附档案的引用保留名称，移除指向旧工作树的链接；外部论文链接和本目录内的图表链接保留。这里不是完整可重跑的证据包，也没有为缺失材料虚构公开地址。

## 重建 PDF

安装 Python、`reportlab`、`pypdf` 和 `svglib`，运行：

```sh
python docs/paper/typesetting/build_reading_pdf.py
```

脚本默认使用 Windows 宋体、黑体和 Times New Roman。其他环境可通过 `--body-font`、`--heading-font`、`--latin-font` 指定已有字体。图 1 的 SVG 会以所选标题字体重建为矢量 PDF，再嵌入正文。PDF 输出至本目录 `pdf/`。这是单栏阅读版。
