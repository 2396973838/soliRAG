# 契约 C：联邦检索（center 侧）

---

## 1. 对 Dify 暴露的那一侧

```http
POST /v1/dify/retrieval
Authorization: Bearer soli_fin_xxx
Content-Type: application/json

{"knowledge_id": "global", "query": "年假怎么算",
 "retrieval_setting": {"top_k": 5, "score_threshold": 0}}
```

Dify 会在配置的 Endpoint 后**固定追加** `/retrieval`，因此 Endpoint 必须填到 `/v1/dify` 为止。

### `knowledge_id` 语义（不按库分类，全局优先）

| 取值 | 含义 |
|---|---|
| `global` / `*` / 空 / 缺省 | **本部门可见的全部库**：自己的 ∪ 被授权且未过期的 |
| 具体库名（如 `hr`） | 单库（调试与专用应用用） |
| 不存在或无权访问的名字 | `200 + {"records":[]}` + 审计 `denied`，**不静默降级成 global** |

同一个 `global`，不同 token，展开出不同视野——这是"对访问者透明"的落点。

---

## 2. 流水线（顺序不可调换）

```
① 认证   Bearer → dept + scopes
② 展开   knowledge_id → targets（数据集列表，实时查 grants）
③ 扇出   并行 POST 各节点 /v1/node/retrieve（每库独立 deadline）
④ 融合   RRF: score(d) = Σ 1/(K + rank_i(d))，K=60
⑤ 去重   key = sha256(normalize(content))，只算哈希不存正文
⑥ 归一   本次结果首条 = 1.0（单调变换，不改排序）
⑦ 过滤   按调用方 retrieval_setting.score_threshold
⑧ 审计   每个被查询的库各写一行
```

- ②不通过时**不许扇出**，避免用响应时间推断库是否存在。
- 每库 `top_k` 取 `min(50, max(2*k, 10))`，给 RRF 留出候选池。
- 单库 deadline 默认 1500ms，整体预算 2500ms。

---

## 3. 响应

```json
{
  "records": [
    {
      "content": "……",
      "score": 0.93,
      "title": "劳动合同法实施条例 §12",
      "metadata": {
        "library": "hr",
        "dept": "hr",
        "document_id": "d-1a2b",
        "segment_id": "s-9f3a",
        "libraries": ["hr", "fin"]
      }
    }
  ],
  "federation": {
    "mode": "global",
    "searched_count": 2,
    "missing_count": 0,
    "degraded": false,
    "fusion": "rrf",
    "took_ms": 118
  }
}
```

- `metadata.libraries` 是去重合并后的来源集合（同一段内容被多个库命中时出现多个）。
- `federation` 只给**数量**不给库名：`searched` / `missing` 明细只进审计。
- Dify 只读 `records`，未知顶层字段被忽略。

---

## 4. 错误语义（绝对禁止 `200 + []` 表示故障）

| 情形 | HTTP | body |
|---|---|---|
| 正常命中 | 200 | `records` 非空，`degraded=false` |
| 库都正常但 0 命中 | 200 | `{"records":[]}`，`degraded=false` |
| 未授权 | 200 | `{"records":[]}` + 审计 `denied`（`SOLIRAG_STRICT_403=1` 时改 403） |
| 部分库不可达 | 200 | `degraded=true`，`missing_count>0`，其余库结果照常返回 |
| 全部库不可达 | 504 | `{"error":{"code":"ALL_SITES_FAILED",...}}` |
| 节点密钥错 / 库归属漂移 | 502 | `NODE_AUTH_FAILED` / `DATASET_NODE_MISMATCH` |
| 参数非法 | 400 | `INVALID_ARGUMENT` |

---

## 5. 参数校验

| 输入 | 判定 |
|---|---|
| `query` 缺失 / 空 / 全空白 | 400 `INVALID_ARGUMENT` |
| `query` 长度 > 4096 | 400 `QUERY_TOO_LONG` |
| `retrieval_setting` 缺失 | 默认 `top_k=5, score_threshold=0` |
| `top_k` ≤ 0 | 400 `INVALID_ARGUMENT` |
| `top_k` > 50 | 截断为 50（不报错） |
| `knowledge_id` 缺失 | 视为 `global` |

---

## 6. 两条设计约束

1. **不缓存检索结果**。缓存会破坏"删文即时"与"撤权即时"。
   唯一的例外是节点侧对 `document_id → title` 的元数据缓存——它不影响可见性。
2. **跨库分数不可比，只能比名次**。不同 Dify 用不同嵌入与 rerank，
   因此融合一律 RRF，对外分数只在本次响应内归一化。
