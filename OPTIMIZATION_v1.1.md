# LBR 1.1 低价偏好与自适应回踩

本次按用户2026年11月2日的要求修改选股层，不修改浏览器内已经确认的成交、费用或冻结持仓目标。七交易日备选批次、买后七交易日提醒以及30% / 30% / 40%实际成交登记保持一致。

## 入选与排序

先通过主板非ST、流动性、足量日线、当前上升趋势、近期下跌风险以及至少一档有效风险收益比筛选，再按趋势质量分档，同档中较低单股价格优先。默认优先30元以内，超过60元排除；可在config/strategy.json的selectionRules中调整。较低股价不会覆盖趋势或风险否决。

涨停回撤和非涨停趋势回踩共用这些门槛。非涨停事件标记为TREND_PULLBACK，涨停价为空，不伪造涨停日期或涨停事件。实际涨跌停价仍需核验。行情、证券状态、公告、账户或盘中承接证据不足时只显示WATCH参考，不能升级为BUY_READY。

## 每只股票自己的价格区间

使用决策日之前的20个完整非复权日线，计算最近14个真实波幅的算术均值、相对ATR、历史滚动回撤分位数、近期确认低点与MA10/MA20附近的支撑关系。波动大的股票通常需要更宽的观察区间，波动小的股票不必固定等待11%或13%。缺数据时没有固定比例备用买点。

A档回撤取1.5倍ATR与历史回撤中位数的较大值，限制在2%—10%；B档取2.5倍ATR、历史回撤75%分位和A档加半个ATR的较大值，限制在3.5%—14%。这些范围和倍数是可配置研究初值，没有统计最优或收益保证。买入上限同时受触发价上浮、0.1ATR和高点限制；支撑失效或风险收益比不足的档位只作观察，不强行改价使其通过。

预估TP1/TP2保留原修复网格；每笔实际成交仍以当时已知的高点、确认低点和实际成交信息冻结，不能用以后出现的低点反写历史。动态方案变化会递增事件版本并使旧触价记录失效。

## 避免下跌中机械低吸

近5日和10日亏损阈值随相对ATR变化，同时保留上界；近期高点大幅回落、ATR过高、跌破日线支撑、连续下跌并低于MA20或当日急跌会否决新入选。除了原事件前的趋势，还复核当前趋势和最近完整交易日的日线，避免用较久以前的上涨趋势解释当前急跌。

## 运行与GitHub验证

后端仍只用Python标准库。正常运行python scripts/update_data.py；网页刷新读取其公开JSON。数据源故障保留旧数据并显示过期。每轮全市场报价之后，最多补充120只股票的历史数据，在原16次工作日调度和210秒请求预算内运行；这不等于对全市场每只股票都完成了日线分析。

新增Check LBR code工作流，在Python 3.11、Node 22的Ubuntu runner中运行后端和前端测试，并验证根目录首页、相对模块、公开JSON及Python 3.11语法。Update LBR market data每次抓取前也执行后端回归；Deploy LBR Pages打包前执行站点验证。三条工作流已用actionlint 1.7.12检查。

手动上传请将仓库内容放在main根目录，保留.github/workflows，并将仓库默认分支设为main，定时任务才会从该分支运行（[GitHub调度说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)）。Pages来源选择GitHub Actions，Actions允许写入公开行情。先运行Check LBR code，再运行Update LBR market data；更新成功后会自动触发Deploy LBR Pages。

本次没有可用的GitHub写入连接，因此云端Actions执行结果仍需用户手动确认，不能把本地验证称为云端运行成功。

## 学习样本

所有事件继续留样，包括未入选和未成交样本；新增按事件类型与定价方法分组，使用买入时快照，避免把两类样本或前后版本无区别混合。规则候选仍受独立成交样本、时间外验证和市场阶段覆盖约束，不自动修改正式门槛。

ATR概念参考[Fidelity ATR说明](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/atr)与[相对ATR说明](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/atrp)。这些来源支持波动指标的定义，不证明本策略的参数或盈利能力。
