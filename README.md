# data-qd-167 · 离线公交时刻数据仓

已实现可离线运行的业务闭环，字段、算法约定和未支持边界详见 [数据契约](docs/contract.md)。样例均为自制合成数据，来源及许可见 fixtures/SOURCES.md。

## 独立安装

Python >=3.11（本批以3.11标准库实测），标准库运行与unittest测试。使用已有uv；setuptools/wheel精确锁定。每个仓库建立独立环境，不共享site-packages。

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-build.lock
uv pip install --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -m unittest discover -s tests -v
```

无uv或构建缓存时，源码同样可直接用标准库运行：`PYTHONPATH=src python3 -m unittest discover -s tests -v`，CLI 为 `PYTHONPATH=src python3 -m transit_feed`。

已有公共缓存时可为两条uv pip install添加 --offline；缓存缺失时需先准备构建工具，不伪称离线成功。修改源码后重新安装。

## 端到端演示

在仓库根目录执行。run/为忽略的运行输出，演示不访问网络、不启动服务或容器。

```sh
.venv/bin/transit-feed import --source fixtures/feed --db run/timetable.sqlite
.venv/bin/transit-feed departures --db run/timetable.sqlite --date 2026-01-06 --stop B
```

预期：导入 stops=3、trips=2、stop_times=5；查询返回 T1，departure=24:12:00。

数据库目标必须不存在，重复演示请指定另一个新数据库路径；查询是只读的。导入先完整校验到临时库，成功后原子发布，已有目标拒绝覆盖；失败退出2，旧目标保留。SQLite档案可脱离输入查询。

## 时刻修订工作台

`revision-init` 从任一合法 import 档案复制出一个独立修订仓；BASE 之后可删除或不再改写，旧 `import`/`departures` 契约完全不变。

```sh
.venv/bin/transit-feed revision-init --source-db run/timetable.sqlite --db run/revised.sqlite
.venv/bin/transit-feed revision-apply --db run/revised.sqlite --file run/batch.json
.venv/bin/transit-feed revised-departures --db run/revised.sqlite --date 2026-01-06 --stop B \
  --after 00:00:00 --known-seq 1
```

批次是顶层严格为 `{"changes":[...]}` 的非空 JSON：

```json
{"changes":[{"op":"put","correction_id":"C1","trip_id":"T1","stop_sequence":2,
  "from":"2026-01-01","to":"2026-12-31","arrival":"24:15:00","departure":"24:15:00"}]}
```

`put` 为该 ID 写入闭区间日期范围与整对 arrival/departure；`retract` 只撤回该 ID 的当前版本。整批要么全部接受、要么全部拒绝：失败退出 2、批次不留下 ID/版本/序号空隙，知识序号不增加。成功输出 `{"known_seq":N}`，同批统一得到前序号+1。

查询按知识序号 K 只看每个 ID 不晚于 K 的最后状态（撤回不贡献值，新 put 的区间外不回退到旧版），先选历史修订再对发车时刻做含等号的 `--after` 过滤，返回最终时刻和来源 `correction_id`（基线为 `null`）。K=0 重现基线，可跨数百年日期范围；省略 K 取当前序号，显式 K 必须是 0 到当前序号的整数。查询只读，失败也不改仓。

Python 接口与 CLI 共用同一实现：`transit_feed.revision_init(source_db, new_db)`、`transit_feed.revision_apply(db, {"changes":[...]})`（也接受批次文件路径）、`transit_feed.revised_departures(db, day, stop, after="00:00:00", known_seq=None)`。错误统一抛出 `ValueError`；CLI 转为退出码 2 和 stderr。

单操作者本地批处理；不承诺并发发布或断电恢复。代码/样例采用MIT许可。
