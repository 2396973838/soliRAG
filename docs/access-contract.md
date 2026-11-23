# 契约 A：登记与访问（center 侧）

> 实现与本文冲突时以本文为准。改契约必须同步改对应测试。

---

## 1. 三个主体

| 主体 | 凭证 | 能 | 不能 |
|---|---|---|---|
| 库的主人（owner） | 部门 token（`scopes` 含 `grant`） | 登记自己的库、决定谁能读、随时收回、查谁在查我的库 | 碰别人的库、看别人的审计 |
| 访问者（visitor） | 部门 token（`scopes` 含 `read`） | 提问并拿到全局最相关结果 | 知道库的存在、位置、还有谁被授权 |
| 中心管理员（admin） | `SOLIRAG_ADMIN_TOKEN` | 建部门、签 token、看全量审计 | **替部门授权**、看正文 |

**核心立场**：授权权属于数据的主人，不属于平台。admin 没有任何接口能改 `grants`。

---

## 2. 认证

```
Authorization: Bearer <raw token>
raw token 形如 soli_<dept>_<32 hex>
库中只存 sha256(raw)；明文只在签发响应里出现一次
```

判定顺序：

1. 取 `Bearer`，缺失或前缀不对 → `401 UNAUTHORIZED`
2. `sha256(raw)` 查 `tokens` 表，无记录 → `401 UNAUTHORIZED`
3. `revoked_at` 非空 → `401 TOKEN_REVOKED`
4. `expires_at` 已过 → `401 TOKEN_EXPIRED`
5. 所属部门 `status != active` → `401 DEPT_DISABLED`
6. 通过 → `Principal{dept, scopes}`

`scopes` 只表达**能力类别**，不表达"能读哪个库"：

| scope | 含义 |
|---|---|
| `read` | 可读自己被授权的库 |
| `grant` | 可将自己的库授权给别人 |

逐库能不能读，完全由 `grants` 表决定。

---

## 3. 登记（owner 自助）

```http
PUT /v1/datasets/{ds}
Authorization: Bearer soli_hr_xxx
Content-Type: application/json

{
  "node": "node-hr",
  "visibility": "shared",
  "grants": [{"dept": "fin", "permission": "read", "expires_at": null}]
}
```

规则：

1. `owner_dept` 由 token 决定，**请求体里不允许出现**；出现即忽略。
2. 首次登记由调用者成为 owner；**已存在时 owner 不可变更**（同内容重复 PUT 幂等；换人 → `403 NOT_OWNER`）。
3. `node` 必须在中心配置 `SOLIRAG_NODES` 里存在，否则 `400 UNKNOWN_NODE`。
4. 中心**不接收也不需要** Dify 地址与密钥；那些只存在节点本地配置里。
5. `visibility` 取 `shared`（可被授权）或 `private`（仅 owner 可读，忽略 grants）。

---

## 4. grants（授权）

| 端点 | 权限 | 说明 |
|---|---|---|
| `PUT /v1/datasets/{ds}/grants` | owner + `grant` | 全量替换 |
| `POST /v1/datasets/{ds}/grants` | owner + `grant` | 追加/更新一条 |
| `DELETE /v1/datasets/{ds}/grants/{dept}` | owner + `grant` | **立即生效** |
| `GET /v1/datasets/{ds}/grants` | **仅 owner** | 看自己的白名单 |

四条硬规则：

1. **不可转授**：只有 `owner_dept` 能写自己的 `grants`。FIN 拿到 HR 的库之后，不能再授权给 RD。
2. **撤销立即生效**：中心不缓存 grants，每次检索实时查表。
3. **过期即失效**：`expires_at` 到期后等同于没有授权。
4. **权限位只有 `read`**：系统不存在跨节点写，`write` 不做、`admin` 不对外暴露。

---

## 5. 403 矩阵（每一格都要有测试）

| 调用者 \ 动作 | 读自己的库 | 读被授权的库 | 改自己的 grants | 改别人的 grants | 看别人的 grants | 看别人的审计 |
|---|---|---|---|---|---|---|
| owner | ✅ | — | ✅ | ❌ 403 | ❌ 403（那不是自己的库） | ❌ 403 |
| 被授权方 | — | ✅ | ❌ 403 | ❌ 403 | **❌ 403** | ❌ 403 |
| 无授权方 | — | `200 + []` + 审计 | ❌ 403 | ❌ 403 | ❌ 403 | ❌ 403 |
| admin | ✅（经 admin 接口） | ✅ | ❌ 403 | ❌ 403 | ❌ 403 | ✅ `/admin/audit` |

所有 403 都要写审计（`action=probe, decision=denied`），只进 admin 视图。

---

## 6. 可见性规则（"访问者互相不可见"落地）

| 信息 | visitor 能否看到 | 出口 |
|---|---|---|
| 库在哪台机器 / 哪个 Dify / 哪个 IP | ❌ | 从不返回 |
| 是否存在一个叫 `X` 的库 | ❌ | 未授权检索 → `200 + []`；`GET /v1/datasets/{未被授权的库}` → `404` |
| 还有谁被授权读这个库 | ❌ | `GET .../grants` 仅 owner |
| 还有谁在读这个库 | ❌ | `GET .../audit` 仅 owner |
| 自己能读哪些库 | ✅ | `GET /v1/datasets`（只列可见的） |
| 本次结果来自哪个库 | ✅ | `records[].metadata.library`（溯源必需） |
| 本次有没有库挂了 | ✅ | `federation.missing_count` / `degraded`（只给数量，不给库名） |

**透明 ≠ 无痕**：拓扑与存在性不可见；自己的边界、结果来源、结果是否残缺必须可见。

---

## 7. 平台方保留的两个开关（默认关闭）

| 开关 | 默认 | 说明 |
|---|---|---|
| `SOLIRAG_STRICT_403` | `0` | 置 1 时未授权检索返回 403（默认 `200 + []` 防枚举） |
| `SOLIRAG_EMERGENCY_REVOKE` | `0` | 置 1 时 admin 可强制下线某部门（`status=disabled`），但仍**不能**替部门授权 |
