# 运行手册 metricgraph

## 1 启动

```bash
docker compose up -d --build
curl -s localhost:18215/api/health
```

宿主端口 `18215`，容器内 `8000`。启动时如果库是空的，服务会自动把 `sample_data/` 里的样例跑一遍，页面打开即有数据。

## 2 截图与验收命令

```bash
docker compose config --quiet
docker compose exec -T app python -m pytest -q
```

## 3 主流程

1. 打开 `http://localhost:18215/`，看规格单的字段清单与采信诊断。
2. `/types` 比较两个类型写法，看读者 / 写者两向结论。
3. `/mapping` 看旧名到新名、别名 SQL、冲突项。
4. `/impact` 看受影响节点、环、断链、单边登记。
5. `/batches` 点「跑一次」，再点「陈旧提交」验证 409。
6. `/spec` 导出迁移单与三份账目 CSV。

## 4 重建数据库

```bash
docker compose down -v
docker compose up -d --build
```

命名卷 `metricgraph-data` 挂着 `/data/metricgraph.sqlite`。删卷即清空批次、快照与隔离区。

## 5 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/health` | 服务与库状态、当前批次号 |
| POST | `/api/schemas/import` | 采信样例文件，返回快照摘要与诊断 |
| GET | `/api/schemas/quarantine` | 隔离区 JSON |
| GET | `/api/schemas/quarantine.csv` | 隔离区 CSV |
| GET | `/api/types` | 类型格与别名表 |
| POST | `/api/types/compare` | 两个类型写法比较 |
| POST | `/api/compat` | 一对版本的两向判定 |
| GET | `/api/mapping` | 映射表、别名、冲突 |
| GET | `/api/impact` | 影响面、环、断链 |
| POST | `/api/runs` | 跑批次，可带基线版本号与生效区间 |
| GET | `/api/batches` | 批次历史 |
| GET | `/api/reconcile` | 当前批次的四账对账 |
| GET | `/api/reconciliation/by-day?effective_day=YYYY-MM-DD` | 按生效日选批并出对账，附选批缘由与被替换原批 |
| GET | `/api/reconciliation/select/{effective_day}` | 只预览选批结论，不落复核记录 |
| POST | `/api/reconciliation/signoff` | 签收某天对账（可指定原批次），签收后锁定 |
| GET | `/api/reconciliation/signed/{effective_day}` | 按原批次回查已签收对账 |
| GET | `/api/reconciliation/correction/{原批}/{更正批}` | 两批兼容判定的逐字段差异 |
| GET | `/api/reconciliation/overview` | 复核记录、签收台账、重叠批次对 |
| GET | `/api/reconciliation/{reconcile_id}` | 单条复核记录 |
| GET | `/api/export/spec.md` | 迁移单 Markdown |
| GET | `/api/export/fields.csv` | 字段账 CSV |
| GET | `/api/export/refs.csv` | 引用账 CSV |
| GET | `/api/export/ledger.csv` | 账目 CSV |
| GET | `/api/export/manifest.json` | 运行清单 |

## 6 失败处置

| 现象 | 原因 | 处置 |
|---|---|---|
| 409 `VERSION_STALE` | 提交带的基线序号落后 | 去掉 `base_version_seq` 或填当前序号 |
| 400 `WINDOW_INVERTED` | 生效区间起点不早于终点 | 检查 `effective_from` / `effective_to` |
| 400 `BAD_INPUT` | 文件名、参数或 JSON 结构不对 | 看返回的 `detail` |
| 404 `NOT_FOUND` | 还没跑过批次，或生效日无批次覆盖 | 先在 `/batches` 点「跑一次」；核对生效日落在哪段区间 |
| 409 `SIGNOFF_CONFLICT` | 当天已签收另一批次，不能改签 | 按原批次回查既有签收；确需重签先人工评审 |

## 7 生效日复核口径

1. 选批按半开区间 `[effective_from, effective_to)` 判定覆盖，末端为空视为无穷远。
2. 多个批次区间覆盖同一天（晚到更正与原批重叠）时取 `version_seq` 最大者，原批进入
   `superseded` 清单，缘由码 `WINDOW_LATEST_WINS`。
3. 当天一旦签收，选批永久锁定签收批次，缘由码 `WINDOW_SIGNED_OFF_LOCKED`；后续更正
   批次只进候选清单，不替换已签收对账。
4. 上游文件更新后摘要变化，重跑产生新批次与新兼容判定，未签收日期的复核自动采用新判定；
   判定差异走 `/api/reconciliation/correction/...` 逐字段核对。
5. 幂等：同一输入重跑命中批次键不新增批次；同一天同一批次的复核与签收走 UPSERT，
   不重复登记。

## 8 端口与并发

本服务固定 18215。同时起多个栈时改 `docker-compose.yml` 的端口映射与 `metricgraph-data` 卷名，避免与本机其他项目抢端口、串数据。
