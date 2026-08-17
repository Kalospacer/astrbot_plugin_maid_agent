"""事件溯源会话后端的纯 Python 层。

包结构：

- contracts.py   事件/帧/消息词表与构造器
- rpc.py         四象限 RPC 信封与错误码表
- event_log.py   append-only 事件日志
- projections.py 投影注册表
- history.py     按消息边界分页的历史读取
- hub.py         events.mux / events.host 帧扇出
- store.py       磁盘 session store + 概要索引
- drivers.py     会话执行引擎（包 AstrBot subagent runner，产出事件流）
- api/           RPC 方法实现（sessions / presets / host）
"""
