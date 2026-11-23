# 契约 B：中心 ↔ 节点 线协议（唯一接缝）

> `center/` 与 `node/` 是两份独立可部署的源码，只通过本文定义的 HTTP 通信。
> 任何一侧改字段，必须同时改本文与对应测试。

---

## 1. 拓扑与方向

```
  部门 Dify ──(中心作为外部知识库)──▶ center :8080
                                        │  X-Federation-Key
                                        ▼
                                     node :8001 ──▶ 本部门 Dify（凭证只在 node）
```

- **中心 → 节点**：唯一跨机调用方向。
- **部门 Dify → 中心**：走 Dify 原生「外部知识库 API」，见 `retrieval-contract.md`。
- **没有人能直连节点写数据**：节点只暴露检索，没有任何写接口。

---

## 2. 请求

```http
POST /v1/node/retrieve
X-Federation-Key: <SOLIRAG_NODE_KEY>
X-Reader-Dept: fin
X-Request-Id: req-7f3a…
Content-Type: application/json

{
  "dataset": "hr",
  "query": "劳动合同到期不续签怎么补偿",
  "top_k": 10,
  "reader_dept": "fin",
  "request_id": "req-7f3a…"
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `dataset` | ✅ | 库名，必须是本节点 `node.toml` 里登记过的 |
| `query` | ✅ | 非空，长度 ≤ 4096 |
| `top_k` | ❌ | 默认 10，上限 50 |
| `reader_dept` | ✅ | 调用方部门（冗余放 body，便于节点记日志） |
| `request_id` | ✅ | 透传，用于两侧日志对齐 |

---

## 3. 响应

```json
{
  "node": "node-hr",
  "dataset": "hr",
  "took_ms": 42,
  "hits": [
    {
      "content": "……正文……",
      "score": 0.87,
      "document_id": "d-1a2b",
      "segment_id": "s-9f3a",
      "title": "劳动合同法实施条例 §12"
    }
  ]
}
```

- `hits` 按 `score` 降序，**名次即语义**（中心只用名次做 RRF，不用分数跨库比较）。
- `title` 允许为空字符串或缺失，中心回退为 `document_id`。
- 无命中 → `hits: []` 且 HTTP 200。

---

## 4. 错误码（节点 → 中心）

| HTTP | `error.code` | 含义 | 中心如何处理 |
|---|---|---|---|
| 401 | `FEDERATION_KEY_INVALID` | 共享密钥不对/缺失 | 视为节点配置错误，记 `missing`，返回 `502 NODE_AUTH_FAILED` |
| 404 | `NOT_MY_DATASET` | 这个库不归我 | 视为中心配置漂移，记 `missing`，返回 `502 DATASET_NODE_MISMATCH` |
| 403 | `READER_NOT_ALLOWED` | 节点本地白名单拒绝 | 记 `missing`，按无权处理 |
| 502 | `UPSTREAM_ERROR` | Dify 调用失败 | 记 `missing` |
| 504 | `UPSTREAM_TIMEOUT` | Dify 超时 | 记 `missing` |

统一错误体：

```json
{"error": {"code": "NOT_MY_DATASET", "message": "...", "request_id": "req-…"}}
```

---

## 5. 其他端点

| 方法 | 路径 | 凭证 | 说明 |
|---|---|---|---|
| GET | `/healthz` | 无 | `{"status":"ok","node":"node-hr"}` |
| GET | `/v1/node/stats` | `X-Federation-Key` | 本节点库清单与 Dify 连通性（不含密钥） |

---

## 6. 二期预留：节点侧硬校验

v1：授权由中心裁决，节点只校验"库是不是我的"。
v2：中心在请求里附加 `X-Grant-Signature: hmac-sha256(node_key, dataset|reader_dept|exp)`，
节点本地校验签名与有效期后才转发。

> **v1 必须先把这个 header 的位置写进代码注释**，否则 v2 要么改协议要么留后门。
> 节点配置里同时保留 `allowed_readers` 字段：为空表示"只信中心"，非空即启用本地硬拦（无需改协议）。
