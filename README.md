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

## 区域时刻仓

`regional-init` 把 2 至 8 个既有 `import` 档案合并成一个只读区域仓；各发布方保留自己的 stop/trip/service 命名空间，互不交叉。`--feed KEY=DB` 可重复，KEY 非空、无首尾空白且唯一，输入路径不可重复；只接受基线档案，修订仓会被拒绝。

```sh
.venv/bin/transit-feed regional-init \
  --feed A=run/a.sqlite --feed B=run/b.sqlite \
  --interchanges run/interchanges.csv --db run/regional.sqlite
.venv/bin/transit-feed regional-departures --db run/regional.sqlite \
  --date 2026-01-05 --interchange X --after 00:00:00
```

映射是严格 UTF-8 CSV，列顺序固定为 `feed_key,stop_id,interchange_id`：

```csv
feed_key,stop_id,interchange_id
A,S,X
A,P,X
B,S,X
```

每行引用本 KEY 档案中存在的站点；同一来源站点最多映射一次，不同来源的站点可共用一个 `interchange_id`，未映射站点不进入区域查询。初始化先完整校验全部档案、映射与数据关系，再在目标目录内写临时库并原子发布；NEW 必须不存在（含符号链接），失败不会发布目标或留下临时产物。查询仅读取复制后的数据，输入档案与映射文件可在初始化后删除或移动。

日期按发布方各自 calendar 起止闭区间与周一至周日标志判定，再由该发布方自己的 `calendar_dates` 例外覆盖（1 加开、2 停开）；同名 service 的日历与例外绝不跨发布方合并。`after` 含等号，默认 `00:00:00`；24:xx 仍附着于所查服务日。输出：

```json
{"service_date":"2026-01-05","interchange_id":"X",
 "departures":[{"feed_key":"B","trip_id":"T","stop_sequence":1,
   "departure":"09:00:00","service_date":"2026-01-05"}]}
```

结果按 departure 秒数、`feed_key`、`trip_id`、`stop_sequence` 排序；同名 trip、同一时刻及同一 trip 对同一换乘点的多次站序访问都作为独立记录保留。已登记但当日无班次的换乘点返回空 `departures`，未知换乘点、非法日期/`after`、损坏仓或缺失数据库一律报错。Python 接口 `transit_feed.regional_init(feeds, interchanges_csv, new_db)` 与 `transit_feed.regional_departures(database, service_date, interchange_id, after='00:00:00')` 返回与 CLI 相同的字典；`feeds` 可为 `{KEY: DB}`、`[(KEY, DB)]` 或 `["KEY=DB"]`。非法输入抛出 `ValueError`。

## 站点关闭看板

`stop-board` 在不改写时刻档案的前提下，给出某服务日某站点的计划发车、可服务状态与命中的关闭通知。关闭计划独立保存在 UTF-8 CSV 中，可随时更新；查询每次重读该文件。

```sh
.venv/bin/transit-feed stop-board --db run/timetable.sqlite \
  --closures fixtures/closures.csv --date 2026-01-07 --stop B
```

关闭 CSV 列顺序严格为 `closure_id,stop_id,from,to,reason`，首行表头，样例见 `fixtures/closures.csv`：

```csv
closure_id,stop_id,from,to,reason
C1,B,2026-01-06,2026-01-07,Track works at Hill
C2,B,2026-01-07,2026-01-08,Staff shortage at Hill
```

`closure_id`、`stop_id` 为非空、无首尾空白的标识符；`from`/`to` 是规范 `YYYY-MM-DD` 服务日且 `from<=to`（闭区间）；`reason` 为非空说明。文件内 `closure_id` 唯一；同一站点可有日期重叠的多条通知，各自保留身份与原因，绝不合并。`stop_id` 必须存在于所查档案。错误编码、错误表头、缺列/多列、重复ID、未知站点、非规范或倒置日期、空字段一律拒绝。

同一条通知覆盖该站点和该服务日时，该站该次访问不可服务；同一 trip 在其他站点的访问、其他站点的计划班次与服务日资格都不受影响。关闭区间以字符串比较判定，从不按服务日展开，可跨多年。

```json
{"closed":true,"closure_ids":["C1","C2"],
 "closures":[{"closure_id":"C1","reason":"Track works at Hill"},
             {"closure_id":"C2","reason":"Staff shortage at Hill"}],
 "departures":[{"closure_ids":["C1","C2"],"departure":"24:12:00",
   "service_date":"2026-01-07","serviceable":false,
   "stop_sequence":2,"trip_id":"T1"}],
 "service_date":"2026-01-07","stop_id":"B"}
```

计划发车仍按该服务日 calendar 与 exceptions 判定（起止闭区间、周一至周日标志、1 加开 / 2 停开），每个 `trip_id`/`stop_sequence` 访问都是独立记录。`--after` 省略时 `00:00:00`，格式为 `00:00:00`..`47:59:59` 且含等号；`24:xx` 仍属于原服务日。`departures` 按最终发车秒数、`trip_id`、`stop_sequence` 排序。`closure_ids` 是该站当日全部命中通知 ID，按 Unicode 码点排序；`closures` 与之同序，只含 `closure_id` 与 `reason`。

开放日顶层 `closed=false`，各访问 `serviceable=true` 且 `closure_ids` 为空；关闭日即使没有计划班次也返回 `closed=true` 与通知身份，而不是误报开放。未知站点、非法日期/`after`、缺失或损坏的档案与关闭文件均报错，查询不创建或修改任何路径。Python 接口 `transit_feed.stop_board(database, closures_csv, service_date, stop, after='00:00:00')` 返回同一字典，非法输入抛出 `ValueError`；CLI 统一退出码 2 并写 stderr。

单操作者本地批处理；不承诺并发发布或断电恢复。代码/样例采用MIT许可。

## 车辆轮转核验

`duty-check` 在只读 import 档案之上，用两份独立 CSV 回答“调度员给每辆车排的跨趟轮转能否执行”。档案本身、修订仓与区域仓都不改写；不在轮转表中的行程不进入本次车辆计划。

```sh
.venv/bin/transit-feed import --source fixtures/duty/feed --db run/duty.sqlite
.venv/bin/transit-feed duty-check --db run/duty.sqlite \
  --duties fixtures/duty/duties.csv --links fixtures/duty/links.csv \
  --from 2026-01-06 --to 2026-01-06
# {"connections": [], "duplicate_assignments": [], "feasible": true}

.venv/bin/transit-feed duty-check --db run/duty.sqlite \
  --duties fixtures/duty/duties.csv --links fixtures/duty/links.csv \
  --from 2026-01-07 --to 2026-01-07
# {"connections": [{"kind": "insufficient_turnaround", "vehicle_id": "V1", ...}],
#  "duplicate_assignments": [], "feasible": false}
```

轮转 CSV 精确表头为 `vehicle_id,trip_id,start_date,end_date`，每行把 `trip_id` 在闭区间 `start_date..end_date` 内实际开行的实例分配给 `vehicle_id`。衔接 CSV 精确表头为 `from_stop,to_stop,seconds`，`seconds` 是 0..86400 的十进制非负整数，同一有向站点对只能出现一次（反向需另写一行）。两份文件都是 UTF-8（可带 BOM），严格校验列数与每一行，最多 8,000,000 字节、20,000 条数据行；只有表头的空文件合法。

联动样例 `fixtures/duty`：T1 在 B 站 10:00 结束、T2 从 B 10:10 到 C 11:00、T3 从 C 11:10 发车，`B->C` 调车 90 分钟时三趟可衔接。`fixtures/duty/feed` 的 `calendar_dates` 在 2026-01-07 停开 T2 后，新邻接 T1/T3 只有 70 分钟，于是报 `insufficient_turnaround`，`available_seconds=4200`、`required_seconds=5400`、`shortfall_seconds=1200`；停开只改资格，不会把 T2 认作重复或缺车。

跨服务日样例 `fixtures/duty-rolling`：D 日 T4 从 A 24:20 到 B 25:00，D+1 日 T5 从 B 00:50 到 C 01:30，同属 V2。只查 2026-01-07 也会报 T4/T5 重叠 10 分钟，`previous.service_date` 仍是 2026-01-06，`start=24:20:00`、`end=25:00:00`。

Python 接口 `transit_feed.duty_check(database, duties_file, links_file, from_date, to_date)` 返回与 CLI 相同的字典；CLI 只向 stdout 输出一份 JSON。业务冲突仍是成功查询，退出 0；输入错误、未知 trip/stop、重复有向规则、非法日期/数值、CSV 损坏、DB 缺失或不是合法 import 档案时抛 `ValueError`（或对应 `OSError`/`sqlite3.Error`），CLI 退出 2、stderr 给出原因、stdout 为空。

## 团体行程订座工作台

`booking-init` 从一个合法 `import` 档案复制出独立订座仓，并为每个 trip 固定一份每服务日容量；修订仓、区域仓和已有订座仓都会被拒绝。NEW 必须不存在（含符号链接），全部校验通过后才原子发布，失败不留下目标或临时文件；发布后可删除 BASE 与容量 CSV，工作台独立运行。样例档案在 `fixtures/booking`：T 为 A09:00→B09:30→C10:00，U 为 B09:35→D10:10，V 为 A09:05→D10:30，三趟各 2 座。

```sh
.venv/bin/transit-feed import --source fixtures/booking/feed --db run/booking-base.sqlite
.venv/bin/transit-feed booking-init --source-db run/booking-base.sqlite \
  --capacities fixtures/booking/capacities.csv --db run/booking.sqlite
# {"bookings": 0, "trips": 3}
```

容量 CSV 是严格 UTF-8（可带 BOM），精确表头 `trip_id,seats`，每个现有 trip 恰一行，不允许遗漏、重复或未知 trip；ID 精确匹配不修剪，`seats` 只接受 ASCII 十进制数字、数值 0..10000（允许前导零，允许 0 座），错误编码、列数错误或非法数值整体拒绝。

请求 JSON 精确包含 `booking_id,origin,destination,service_date,earliest,latest_arrival,party_size,max_transfers,min_transfer_seconds` 九个字段，拒绝重复键、缺字段、多余字段；布尔、浮点和数字字符串都不能用于整数字段。只使用 `service_date` 当天实际开行的 trip 实例（calendar + 加开/停开例外），`24:xx` 仍属于该服务日。相邻腿在同一 `stop_id` 换乘且满足最短换乘秒数（含等号），一个行程最多 `max_transfers+1` 腿、同一 trip 至多乘一次；`fixtures/booking/request.json` 是一个完整请求样例。

### 成功、改道与无座示例

```sh
# P 订 B→C：占用 T 的 B→C 后半程（2 座恰好占满该区间）
printf '%s' '{"booking_id":"P","origin":"B","destination":"C","service_date":"2026-11-20","earliest":"08:00:00","latest_arrival":"11:00:00","party_size":2,"max_transfers":1,"min_transfer_seconds":300}' > run/p.json
.venv/bin/transit-feed booking-create --db run/booking.sqlite --file run/p.json
# accepted=true，arrival=10:00:00，单腿 T 站序 2→3

# Q 订 A→D：T 前半程仍空，可乘 T+U，10:10 到达（成功示例）
.venv/bin/transit-feed booking-create --db run/booking.sqlite \
  --file fixtures/booking/request.json

# R 再订 A→D：T+U 的余座已被 P、Q 占满，自动改乘 V，10:30 到达（改道示例）
printf '%s' '{"booking_id":"R","origin":"A","destination":"D","service_date":"2026-11-20","earliest":"08:00:00","latest_arrival":"11:00:00","party_size":2,"max_transfers":1,"min_transfer_seconds":300}' > run/r.json
.venv/bin/transit-feed booking-create --db run/booking.sqlite --file run/r.json

# 取消 Q 后，S 尝试 A→C：T 后半程仍被 P 占满，正常业务失败（无座示例，退出 0）
.venv/bin/transit-feed booking-cancel --db run/booking.sqlite --booking Q
printf '%s' '{"booking_id":"S","origin":"A","destination":"C","service_date":"2026-11-20","earliest":"08:00:00","latest_arrival":"11:00:00","party_size":2,"max_transfers":1,"min_transfer_seconds":300}' > run/s.json
.venv/bin/transit-feed booking-create --db run/booking.sqlite --file run/s.json
# {"accepted": false, "arrival": null, "legs": [], ...}，不扣任何余座、不占用 S

# W 此时可订 A→D，再次走 T+U；重放已取消的 Q 只返回原 10:10 收据，不恢复占用
.venv/bin/transit-feed booking-show --db run/booking.sqlite --booking Q
# {"status": "cancelled", "receipt": {...原 10:10 成功收据...}}
```

### 区间座位、决胜与取消重放口径

- 座位按 `(trip_id, service_date, 相邻站序对)` 管理。从站序 a 乘到 b 的腿必须在其覆盖的**每个**相邻区间都有至少 `party_size` 个余座，订座成功后同时占用；容量不按整趟一次扣减，a→b 与 b→c 可由不同团体复用同一座位。任一区间的合计用量（所有未取消且覆盖该区间的成功预订人数之和）始终不超过该 trip 容量；团体不可拆分。
- 选路决胜依次为：最终到达秒数较早者优先；乘车腿数较少者优先；仍相同则按各腿 `(trip_id, board_sequence, alight_sequence)` 列表逐项字典序（trip_id 按 Unicode 码点、站序按整数）。忽略容量的最佳路线无座时继续考虑其余路线；较晚到达换乘站但腿数更少的前缀不会被剪枝，因此不会漏掉受换乘上限约束的唯一可行方案。
- 成功收据精确含 `booking_id,accepted,party_size,service_date,arrival,legs`；每腿精确含 `trip_id,board_sequence,alight_sequence,from_stop,to_stop,departure,arrival`。无座是 `accepted=false, arrival=null, legs=[]` 的正常结果，退出 0，不留部分腿或占用，该 ID 之后可重新尝试。
- 成功 ID 永久绑定首次请求的全部字段与原收据：同 ID 同请求重放只返回原收据，不重新选路、不再次扣座，取消后依然如此；同 ID 改动任一字段即报错（即使已取消、即使新请求无车可乘）。先做完整请求校验，再检查身份与重放，只有全新 ID 才按当前余座选路。
- `booking-cancel` 对有效预订一次性释放全部腿并永久保留请求与收据，重复取消返回相同结果且不会再次释放；未知 ID 报错。`booking-show` 只读返回 `{booking_id,status,receipt}`。每次 create/cancel 都是一个整体事务，跨进程重启后身份、取消状态、原收据与余座承诺仍然有效。

Python 接口与 CLI 共用实现并返回相同的 JSON 可序列化字典：`transit_feed.booking_init(source_db, capacities_csv, new_db)`、`booking_create(database, request)`（`request` 为 Python 字典，也接受请求文件路径）、`booking_cancel(database, booking_id)`、`booking_show(database, booking_id)`。非法输入抛 `ValueError`（或对应 `OSError`/`sqlite3.Error`），CLI 退出 2、stderr 说明原因、stdout 为空；每条成功命令 stdout 只有一份 JSON。单操作者本地工作台，不提供联网服务、多写者协议或自动候补。
