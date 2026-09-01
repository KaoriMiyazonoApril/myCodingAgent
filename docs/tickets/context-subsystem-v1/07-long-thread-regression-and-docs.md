# 07: 完成长线程回归、恢复验证与架构文档

**What to build:** 一个完整 scripted long Thread 穿过 user、assistant、multiple tools、pressure、prune、
compaction、checkpoint、下一次 request 与 persistence recovery，同时文档准确描述最终 architecture。

**Blocked by:** 06: 串联有界 Context reduction pipeline。

**Status:** completed

- [x] end-to-end request 包含正确 system/project/runtime、synthetic summary、TaskPlan 和 recent raw tail。
- [x] old raw history 不重复发送，tool pairing 合法，canonical durable history 原样完整。
- [x] checkpoint 在 SQLite 恢复后继续 rolling，失败路径不破坏旧 checkpoint。
- [x] Context/AgentLoop/Conversation/Provider/local tools/persistence focused regressions与全量 Python tests 通过。
- [x] Web/CLI checks 按触及范围通过；bubblewrap 环境缺失单独报告；README 与 Context/Thread 文档同步。
- [x] 完成 code review、中文提交与 push，未跟踪的既有生成目录不进入提交。
