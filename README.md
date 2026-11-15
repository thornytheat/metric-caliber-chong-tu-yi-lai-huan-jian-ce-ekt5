# metricring · 指标口径注册与依赖门禁

指标体系组的注册表门禁工具：把「同一个 GMV 在三个看板算法不同」这类口径事故挡在发布之前，
并在改口径前算清下游影响面。

## 能力

- **指标注册**：指标名 + 版本 + 表达式（`metric:`/`dim:` 引用、`sum/avg/count` 包裹）+ 单位/聚合/维度/负责人/是否发布，落 SQLite（命名卷 `metricring-data`），启动幂等 seed。
- **公式解析与归一化**：去空白、去冗余括号、去 `*1`、常量化、统一前缀；只做结构等价变换，`a/b` 与 `a/b*100` 归一化后仍然不同。
- **依赖图 + 环检测**：节点是「指标@版本」，边是表达式里的 `metric:` 引用；DFS 三色找环并给出环路径与涉及指标。
- **同名异口径**：同名指标按归一化表达式分组，>1 组即 conflict，列出各组版本与差异摘要。
- **变更门禁**：改表达式/维度/聚合 → 算全部传递下游，分 `breaking`（移除下游依赖的维度、改聚合、引入环、引用未发布版本）与 `requires_sync`。
- **发布校验**：被引用版本必须 `published=true`，且不得处于环中、不得与已发布基线口径不同、不得引用撤场中/已撤场版本。
- **版本撤场**：受理后新引用立即被拒；已发布公式仍按原版本取数并列出待迁清单；清单清空后一次性切换为「已撤场」；全程单事务落库，中断可恢复、重试幂等。

## 核心规则（后端真算）

| 规则 | 模块 | 归一化/判定口径 |
|---|---|---|
| 公式解析 | `app/expr.py` | 递归下降，`+ - * / ( )`、`metric:name[@ver]`、`dim:name`、`sum/avg/count` |
| 归一化 | `app/expr.py` | 去空白、丢冗余括号（按优先级重新加）、消 `*1`/`1*`/`/1`/`+0`、数字常量化、前缀小写 |
| 依赖图 | `app/graph.py` | 节点 `name@version`；裸引用 `metric:x` 解析到「最新已发布版本」，`metric:x@2` 精确解析 |
| 环检测 | `app/graph.py` | DFS 白/灰/黑三色，灰色邻居即环，按节点集合去重 |
| 口径冲突 | `app/conflict.py` | 同名 → 归一化分组 → 组数 > 1 即 conflict |
| 变更门禁 | `app/gate.py` | 传递下游 + `breaking`/`requires_sync` 判定 |
| 发布门禁 | `app/gate.py` | `unpublished_reference` / `missing_reference` / `cycle_member` / `caliber_conflict` / `decommissioning_reference` |
| 撤场过渡 | `app/decommission.py` | `transitioning`（新引用拒绝、旧公式照常取数）→ 待迁清单清空 → `decommissioned`（单事务切换）；`UNIQUE(name,version)` + 条件更新保证重试幂等 |

## 撤场过渡（可审查、可恢复、幂等）

注册表行仍只有「发布/未发布」两态，撤场是叠加在 `decommissions` 表上的版本级生命周期：

```
（无记录） --受理--> transitioning 撤场过渡中 --待迁清单清空--> decommissioned 已撤场
```

- **受理（POST /api/decommission）**：写撤场记录（受理时指标行 + 待迁清单快照）。受理后**新引用立即被拒**——登记、发布、迁移、解析（strict）四处门禁都会拦截指向过渡中版本的引用（含裸引用解析到它的情况）；**已发布公式仍按原版本取数**——目标版本保持 published，依赖图解析不变。
- **待迁清单**：「最新发布版本仍传递依赖目标版本」的指标名，由依赖图实时重算（未发布草稿不参与取数，只提示）。迁移动作（POST /api/decommission/migrate）为待迁指标登记并发布一个不再引用目标版本的新版本，单事务完成；裸引用下游会随替代版本发布级联清空。
- **一次性切换（POST /api/decommission/finalize）**：清单为空才放行，单事务完成状态翻转 + 旧版本下线（published=false）；未清空返回 409 与剩余清单。
- **可恢复**：三个动作各自一个 SQLite 事务，中途失败整体回滚；重启后 `GET /api/decommission` 还原过渡现场，记录 snapshot 保留受理时旧版取数口径，历史体检快照不受影响。
- **幂等**：`UNIQUE(name, version)` + `ON CONFLICT DO NOTHING` —— 受理重试不重复生成记录；迁移按「清单已清空」判定 —— 重试不重复登记版本；切换用 `UPDATE ... WHERE state='transitioning'` —— 重试不重复写事件。

样例数据 `sample_data/metrics.json`：24 个指标版本 / 20 个指标名，故意包含
**3 组同名异口径**（gmv 三口径、uv 两口径、conversion_rate 原值与百分比）、
**2 条依赖环**（cpa↔cpc、unit_price↔gmv_unit）、
**4 个跨版本依赖**（gmv@2→gmv@1、uv@2→uv@1、pay_gmv_share→gmv@3、uv_per_gmv→uv@2）、
**2 个未发布依赖**（gmv@3、uv@2）。

## 启动

```bash
cd batch-20261114b/projects/gb-626
cp .env.example .env          # 可选，默认 18215
docker compose up -d --build
curl -s http://127.0.0.1:18215/health
docker compose down -v        # 重建数据库：删掉 metricring-data 卷即可
```

## 演示步骤

1. 打开 `http://127.0.0.1:18215/` → 点「执行注册表体检」→ 结果页：3 组同名异口径、2 条依赖环、2 个发布阻断、逐条依据（原始表达式 vs 归一化表达式）。
2. `/workbench` 选 `gmv_daily@1`，把维度从 `dt,channel` 改成 `dt` → 「计算变更影响面」：6 个下游中有 2 个 breaking（`gmv_7d`、`pay_gmv_share` 按 channel 分组），其余 4 个 requires_sync。
3. `/review` 登记修正后的 `gmv_daily@2` 并发布 → 门禁放行；再试发布 `gmv@3`（支付口径）→ 被同名异口径拦截。
4. `/review` 撤场面板受理 `gmv@3` 撤场 → 待迁清单列出 `pay_gmv_share@1`；新登记引用 `gmv@3` 的公式被 400 拒绝；迁移 `pay_gmv_share` 到 `gmv@2` 净额口径后清单清空 → 「切换已撤场」放行，`gmv@3` 停止取数，撤场单与历史体检仍可回看。

## 页面

| 路由 | 内容 |
|---|---|
| `/` | 使用者/场景/输入、注册表概览、冲突与环预览、演示步骤、体检按钮 |
| `/workbench` | 选指标、编辑表达式/维度/聚合、解析归一化、影响面、上游依赖树、注册表全量 |
| `/result/{check_id}` | 冲突 3 组、环 2 条、发布阻断、跨版本依赖、全部指标归一化前后对比 |
| `/review` | 最近体检、变更影响面（breaking / requires_sync）、发布按钮、撤场过渡与待迁清单、版本历史与历史体检 |

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/api/registry` | 注册表 + 统计（冲突/环/阻断计数） |
| GET | `/api/metrics` | 指标列表 |
| POST | `/api/parse` | `{expression}` → 归一化、引用清单、不可解析引用；语法错误 400 |
| POST | `/api/check` | 执行体检并落库，返回完整结论 |
| GET | `/api/check/{id}` | 读取体检结果；不存在 404 |
| POST | `/api/impact` | `{name,version?,expression,aggregation?,dimensions?}` → 影响面；指标不存在 404、表达式非法 400 |
| POST | `/api/registry` | 登记新版本；重复 409、字段非法或缺引用 400、引用撤场中/已撤场版本 400 |
| POST | `/api/publish` | 发布校验；被拦截 409 并返回 blockers |
| POST | `/api/decommission` | 受理撤场 `{name,version,reason?,operator?}`；同版本重试返回既有记录（幂等），已撤场 409 |
| GET | `/api/decommission` | 全部撤场记录 + 实时待迁清单（中断/重启后从这里恢复过渡现场） |
| GET | `/api/decommission/{name}/{version}` | 单条记录：受理快照（旧版结果）、待迁/已迁清单、迁移日志 |
| POST | `/api/decommission/migrate` | 迁移并发布：为待迁指标登记不再引用目标版本的新版本，单事务；已迁过重试返回 `already_migrated` |
| POST | `/api/decommission/finalize` | 待迁清单清空后一次性切换为已撤场；未清空 409 并返回剩余清单；重试不重复切换 |

```bash
# 注册表与体检
curl -s http://127.0.0.1:18215/api/registry | python3 -m json.tool | head -20
curl -s -X POST http://127.0.0.1:18215/api/check | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['stats'])"
# 撤场：受理 → 看待迁清单 → 迁移 → 切换
curl -s -X POST http://127.0.0.1:18215/api/decommission -H 'Content-Type: application/json' -d '{"name":"gmv","version":3,"reason":"支付口径废弃"}'
curl -s http://127.0.0.1:18215/api/decommission/gmv/3 | python3 -m json.tool
```

## 目录

```
app/
  main.py        # FastAPI 路由（薄）
  expr.py        # 公式解析 + 归一化（纯函数）
  graph.py       # 依赖图 + DFS 三色环检测
  conflict.py    # 同名异口径
  gate.py        # 变更门禁 + 发布门禁
  decommission.py # 撤场过渡（受理/迁移/切换，幂等可恢复）
  registry.py    # 指标注册校验/索引/版本解析
  report.py      # 体检编排
  store.py       # SQLite
  seed.py        # 幂等 seed
  settings.py    # 路径常量
  templates/     # base/index/workbench/result/review
  static/        # app.js（原生）、style.css
sample_data/metrics.json
tests/           # test_expr / test_graph / test_conflict / test_gate / test_api / test_decommission
```

## 测试

```bash
docker compose run --rm app python -m pytest -q
```

## 已知限制

- 归一化只做结构等价（去空白/括号/`*1`/常量化），不做代数化简：`a*2/2`、`a+a` 不会被合并成 `a` 或 `2*a`。
- 依赖解析是静态的：裸引用固定解析到「最新已发布版本」，不回放历史时点（想固定就写 `@版本`）。
- 环检测覆盖指标引用环；`dim:` 维度引用不参与图，维度冲突靠「变更门禁的维度移除判定」兜底。
- 表达式不含窗口/时间过滤语法（`dt=` 之类），明细口径差异由 `dim:` 与描述字段表达。
- 待迁清单按「指标名的最新已发布版本」判定在用公式：旧版本仍引用已撤场版本属于历史快照，不阻断切换，但会在后续体检中体现为未发布引用。
- 单进程 SQLite（WAL），不做并发写入队列；演示规模 24 条指标足够。
