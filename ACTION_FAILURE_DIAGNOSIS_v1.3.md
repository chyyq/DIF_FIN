# 自动更新失败检查（2026-11-24）

本轮只读检查chyyq/DIF；未修改、推送或重新运行远端任务。

## 已确认的证据

- 最近公开刷新任务[Update #232](https://github.com/chyyq/DIF/actions/runs/45217260334/job/137841656419)在中国时间11月24日00:30启动，约1分39秒执行完毕；测试成功、行情生成成功、Commit public market data失败。之前数次任务同样失败，并非一直计算30分钟。
- 公开工作流/作业API可验证失败步骤；完整作业日志需要登录，日志API返回403。因此目前没有获取到Git服务器拒绝推送的原始完整文字。
- [仓库树API](https://api.github.com/repos/chyyq/DIF/git/trees/main?recursive=1)显示data/audit.jsonl已达103,597,445字节，约98.8MiB；距100MiB仅1,260,155字节。现有生成器每轮将所有事件快照追加到此文件。这是导致新增提交过大的明确风险，也是目前最强的失败原因推断；不将推断冒充完整日志结论。
- [GitHub官方说明](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)明确阻止超过100MiB的单文件。
- [网站公开快照](https://chyyq.github.io/DIF/data/latest.json)仍是11月10日16:37生成、行情日期11月10日，meta.parameterVersion为1.1.0。生成成功但Git写回失败，无法进入随后部署。

## 本地修复

发布前使用scripts/publish_data.py将旧audit逐行无损压缩为不超过8MiB原始内容的gzip片段，data/audit.jsonl保留最后一个小片段。data/audit-archive/manifest.json保存顺序、SHA256与字节数；没有删除研究记录，没有重写Git历史。旧Git blob本身仍低于100MiB，可以保留原有历史。

所有data文件在提交前做90MiB体积保护。Git权限拒绝、分支规则拒绝、远端更新冲突及网络失败分别输出明确的错误代码；网络重试有次数与单次时限。不会强制推送或用旧行情覆盖并发的新行情。工作流写回步骤最多3分钟，只在main运行。

本地回归使用临时bare Git仓库验证真实commit/push，不连接GitHub：验证公开数据成功写回、未暂存源代码不被夹带、源代码暂存时拒绝、并发远端数据不被强制覆盖，以及超过90MiB的合成audit在轮转后仍完整可还原并可推送。轮转仅在实际发布时执行，本地现有data未被改写。

完整Git日志仍有访问限制。若新工作流依然失败，Actions注释和步骤摘要将直接显示具体失败分类，便于区分体积之外的权限或分支策略问题；当前不能声称云端已经修复成功。
