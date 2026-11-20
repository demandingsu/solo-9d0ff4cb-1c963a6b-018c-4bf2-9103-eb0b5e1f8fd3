# 纸样厚度计数核对 API

登记一批纸样的总片数，分次提交互可重叠的厚度区间计数（整数微米，区间
`[lo, hi)`，含起点不含终点）。给定合格区间，返回**同时满足全部有效计数**
时合格片数可达到的最小值和最大值。计算按端点做整数规划，不逐片展开。

补报与已有计数不相容时**整份拒收**（HTTP 409），已有结果保持不变、仍可查询。

旧补报可能失准时，质检员可提交**纸厚复测表**替换：按质检号圈定最多 10 份
可撤回的有效旧补报，在保留复测表与圈定范围外补报的前提下，自动选出**使计数
相容且撤回份数最少**的方案；新表入库与旧表撤回在同一事务内一并生效。
历史补报不会被删除，始终可以查到旧表及其撤回状态。

## 快速开始

```bash
docker compose up --build
```

打开 <http://localhost:8000/docs> 录入、查询与演示替换。
SQLite 数据保存在命名卷 `inspection-data`（容器内 `/data/inspection.db`）中，
重启容器不丢失。

本地不用 Docker 时：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

## 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/inspections` | 登记总片数（`total_pieces`，可选 `note`） |
| `GET` | `/inspections` | 列出全部登记 |
| `GET` | `/inspections/{id}` | 查看登记、补报历史（含已撤回）与替换历史 |
| `PATCH` | `/inspections/{id}` | 更正总片数/备注；新总片数与有效计数冲突则拒收 |
| `POST` | `/inspections/{id}/reports` | 提交一批区间计数 `{"segments":[{"lo","hi","count"}]}` |
| `GET` | `/inspections/{id}/reports` | 查看补报历史；`include_revoked=false` 只看有效补报 |
| `GET` | `/inspections/{id}/bounds?qualified_lo=&qualified_hi=` | 查询合格片数上下界（只按有效补报） |
| `POST` | `/inspections/{id}/replacement-plan` | **试算**替换方案，不落库 |
| `POST` | `/inspections/{id}/replacements` | 提交复测表，原子地入库新表并撤回旧表 |
| `GET` | `/inspections/{id}/replacements` | 查看替换历史 |
| `GET` | `/health` | 健康检查 |

### 基本示例

```bash
curl -X POST localhost:8000/inspections \
  -H 'content-type: application/json' -d '{"total_pieces":100}'

curl -X POST localhost:8000/inspections/1/reports \
  -H 'content-type: application/json' -d '{"segments":[{"lo":40,"hi":60,"count":70}]}'

curl -X POST localhost:8000/inspections/1/reports \
  -H 'content-type: application/json' -d '{"segments":[{"lo":50,"hi":70,"count":60}]}'

curl 'localhost:8000/inspections/1/bounds?qualified_lo=50&qualified_hi=60'
# {"inspection_id":1,"total_pieces":100,"qualified_lo":50,"qualified_hi":60,
#  "min_count":30,"max_count":60}

# 冲突补报：整份 409 拒收，上面的结果不受影响
curl -X POST localhost:8000/inspections/1/reports \
  -H 'content-type: application/json' -d '{"segments":[{"lo":40,"hi":60,"count":71}]}'
# {"detail":"补报与有效计数（或本报告内部）不相容，整份拒收；已有结果保持不变"}
```

### 替换（纸厚复测）示例

```bash
# 现有 3 份补报：1 号 [0,10)=5，2 号 [10,20)=6，3 号 [20,30)=5
# 复测表 [10,20)=3 —— 与 2 号冲突，只能撤回 2 号一份。

# 1) 试算：返回圈定范围、最少撤回方案与“试算依据”
curl -X POST localhost:8000/inspections/1/replacement-plan \
  -H 'content-type: application/json' \
  -d '{"segments":[{"lo":10,"hi":20,"count":3}]}'
# {
#   "inspection_id": 1, "total_pieces": 100,
#   "basis": {"total_pieces": 100, "active_report_ids": [1,2,3]},
#   "candidates": [{"report_id":1,...},{"report_id":2,...},{"report_id":3,...}],
#   "revoked_report_ids": [2], "solvable": true
# }

# 2) 提交：把试算依据一并回传（期间若有新补报/总片数更正，整次拒收）
curl -X POST localhost:8000/inspections/1/replacements \
  -H 'content-type: application/json' \
  -d '{"segments":[{"lo":10,"hi":20,"count":3}],
       "expected":{"total_pieces":100,"active_report_ids":[1,2,3]}}'
# 201
# {"replacement_id":1,"report":{"id":4,"status":"active",...},
#  "revoked_report_ids":[2],"basis":{...}}
```

* `candidate_report_ids`（可选，最多 10 个）显式圈定可撤回的旧补报；
  省略时自动圈定**新近的 10 份**。圈定范围外的有效补报一律保留。
* 试算无解（`solvable=false`）或提交时无解：`409`，不写入任何数据。
* 提交时带了 `expected` 但当前总片数或有效补报号与试算依据不同：`409`，
  不写入任何数据。不带 `expected` 时按提交时刻的实际有效补报重新试算。
* 撤回是“状态变更”而非删除：`GET /inspections/{id}` 的 `reports` 会列出
  旧表，带 `status="revoked"`、`revoked_at` 与 `revoked_by_replacement_id`。
* 后续补报校验、总片数更正、合格片数上下界都只使用有效补报。

## 计算方法

### 上下界

把所有区间端点（含查询端点与厚度零点）排序，将厚度轴切成原子区间。落在同一
原子区间内的整数厚度在所有计数约束下等价，因此每个原子只需一个非负整数变量
（片数）。设前缀和为 `S_k`：

* 区间 `[lo, hi)` 计数为 `c` ⇔ `S_j - S_i = c`；
* 总片数 `N` ⇔ `S_last - S_0 = N`；
* 原子片数非负 ⇔ `S_k <= S_{k+1}`。

目标“合格区间片数”为 `S_hi - S_lo`。其最大值等于差分约束图上 `lo → hi` 的
最短路，最小值等于 `hi → lo` 最短路取负。约束矩阵为连续一矩阵（全幺模），
最短路给出的整数解即原整数规划的精确最优解。负环 ⇔ 计数互相矛盾。

复杂度只取决于端点数量与图规模，与总片数无关；实现使用 SPFA 求最短路，
负环触发时用 Bellman-Ford 复核，避免误判。

### 最少撤回方案

一份补报内的所有区间整体保留或整体撤回，不能拆开。圈定候选最多 10 份，
按撤回份数 `k = 0, 1, …` 递增枚举全部组合，第一个使“保留的旧补报 + 复测表
+ 圈定范围外补报”相容的 `k` 即最少撤回份数。同一 `k` 内把撤回集合按报告号
从大到小排列后做字典序**降序**枚举，首个可行组合即为并列规则要求的
“第一处编号较大”的一组。总枚举量不超过 `2**10`，且不依赖相容性随撤回
份数的单调假设。相容性判定复用与上下界相同的差分约束算法。

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

* `tests/test_solver.py`：重叠区间、相邻不重叠区间、零计数、冲突检测、
  大数量不展开，以及 40 组与逐片枚举对拍。
* `tests/test_api.py`：登记、补报、跨批次重叠、跨批次冲突整份拒收、
  单份报告内部冲突拒收、拒收后原结果可查、`PATCH` 冲突拒收、参数校验、`/docs`。
* `tests/test_concurrency.py`：并发冲突补报只有一个成功；并发相容补报全部接收。
* `tests/test_replacement.py`：最少撤回、并列取编号较大的一组、显式/自动圈定
  范围、原子回滚、试算期间补报与总片数更正、依据不一致拒收、撤回后旧表仍可
  查、后续校验只用有效补报、并发替换串行化。
* `tests/test_replacement_solver.py`：150 组随机实例与穷举子集对拍。

数据库结构升级：旧库首次启动会自动补 `status`/`revoked_at` 等列（已有补报视为
有效），无需手工迁移。
