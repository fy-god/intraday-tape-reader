# 未运行项与原因

## 真实盘中 soak
**未运行**。非交易时段 + `spirit_index` 默认关闭。
合成 tick 会污染 Precision/Recall 样本，**拒绝伪造**。

## 044/043 的真实生产数据
**unavailable**。这两个 ID 的证据全部来自**合成轮样本**与
真实 `Engine._route_raw_presence` 调用；
生产里 `index` route 的真实 -- 需要 `spirit_index.enabled=true`
且处于交易时段。

## 真实 Precision / Recall / 漏事件率 / 交易收益
**unavailable**。需要多交易日真实 manifest。
