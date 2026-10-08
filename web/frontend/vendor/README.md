# 前端第三方库（本地 vendor）

这些库原先走 unpkg / jsdelivr CDN，在受限网络下拉取失败，导致 Markdown 渲染不生效。
现在随仓库本地提供，`index.html` 直接引用 `./vendor/*.js`。

| 文件 | 包 | 版本 |
| --- | --- | --- |
| `vue.global.prod.js` | vue | 3.4.38 |
| `echarts.min.js` | echarts | 5.5.1 |
| `marked.min.js` | marked | 12.0.2 |
| `purify.min.js` | dompurify | 3.1.6 |
