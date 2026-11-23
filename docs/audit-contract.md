# 契约 D：审计与可见性

> 一句话：**"谁读了什么"只有 owner 和审计能看，访问者之间互相不可见。**

---

## 1. 表结构

```sql
audit (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           TEXT,      -- ISO8601 UTC
  request_id   TEXT,
  reader_dept  TEXT,      -- 谁在查
  dataset      TEXT,      -- 这一次打的是哪个库
  owner_dept   TEXT,      -- 冗余列，owner 视图靠它走索引
  action       TEXT,      -- retrieve | probe | grant | revoke | list
  decision     TEXT,      -- allowed | denied | degraded | error
  reason       TEXT,
  matched_docs INTEGER,
  searched_count INTEGER,
  query_hash   TEXT,      -- sha256(query)，不存明文
  latency_ms   INTEGER,
  prev_hash    TEXT,      -- 哈希链
  hash         TEXT
);
CREATE INDEX ix_audit_owner  ON audit(owner_dept, id DESC);
CREATE INDEX ix_audit_reader ON audit(reader_dept, id DESC);
```

**为什么 `global` 一次查询要写 N 行**：
每个被尝试的库各写一行 `(reader, dataset, owner)`。这样 HR 的 owner 视图只需要
`WHERE owner_dept='hr'`，天然只看到涉及自己的那一片，不需要在查询层做跨库裁剪。

---

## 2. 三个视图

| 端点 | 谁能调 | 返回 |
|---|---|---|
| `GET /v1/me/audit?limit=` | 任意部门 token | 自己作为 reader 的记录（含 dataset 名） |
| `GET /v1/datasets/{ds}/audit?limit=&decision=` | **仅 owner** | 谁在查我的库 |
| `GET /admin/audit?reader=&dataset=&limit=` | 仅 admin | 全量 |

`GET /v1/datasets/{ds}/audit` 被非 owner 调用 → `403 NOT_OWNER`，并且**这次探测本身也记审计**
（`action=probe, decision=denied`，只进 admin 视图）。

---

## 3. query 明文策略

| 存什么 | 给谁看 | 理由 |
|---|---|---|
| `query_hash` | 所有视图 | 能对同一查询做频次统计，泄漏为零 |
| `query` 明文 | **不落库** | visitor 的查询带着它自己部门的业务意图 |
| `query_preview` | 默认关闭 | `SOLIRAG_AUDIT_QUERY_PREVIEW=1` 才存前 32 字，且只进 admin |

owner 视图足以回答真问题："FIN 上周扫了我 300 次"看得到；"FIN 在问什么"看不到。

---

## 4. 防篡改

| 约束 | 实现 |
|---|---|
| append-only | 没有任何 `PUT` / `DELETE` 审计的端点 |
| 只在中间件/统一入口写 | 禁止业务 handler 手写审计行 |
| 哈希链 | `hash = sha256(prev_hash ‖ 规范化字段)`，`prev_hash` 取上一行 `hash` |
| 可校验 | `GET /admin/audit/verify` 逐行校验链完整性，返回第一个断点 |

有了哈希链，"只有 owner 和审计能看"才不依赖于"你相信中心没偷偷删记录"。

---

## 5. 必须成立的性质（A1–A8，都有对应测试）

| # | 断言 | 期望 |
|---|---|---|
| A1 | FIN 读 `hr` 后，HR 查 `/v1/datasets/hr/audit` | 出现 `reader_dept=fin` 且 `matched_docs>0` |
| A2 | FIN 调 `GET /v1/datasets/hr/grants` | `403 NOT_OWNER`，且该探测进 admin 审计 |
| A3 | RD 读 `hr` 后，FIN 的 `GET /v1/me/audit` | **查不到任何 RD 的记录** |
| A4 | FIN 调 `GET /v1/datasets` | 只返回 `fin` 与 `hr`，不出现 `rd` 的库 |
| A5 | FIN 硬查 `rd` 的库名 | `200 + []`；RD 的 owner 审计出现 `denied`；FIN 侧无信息增量 |
| A6 | HR 撤销 FIN 授权后 FIN 再查 | `200 + []`，HR 审计出现撤权后的 `denied` |
| A7 | 对 `/v1/audit` 的 `PUT`/`DELETE`/`POST` | `404`/`405`，路由不存在 |
| A8 | 任意响应体 | 不含 `query` 明文（除调用方本次自己的输入） |

---

## 6. 已知代价（写进文档而不是藏起来）

| 代价 | 说明 |
|---|---|
| owner 能看见"谁在读" | 可能被当作竞争情报；用"不给 query 明文"把伤害限制在"频次可见、内容不可见" |
| 审计写放大 | 全局查询 N 库 = N 行 |
| 中心仍是信任根 | 哈希链 + `verify` 端点让 owner 能审计审计者 |
