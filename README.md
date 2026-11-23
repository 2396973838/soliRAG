# soliRAG

公司里各部门各自有一套 Dify，知识库散在不同机器上，而且谁也不能把自己的文档挪到别人那儿去。这个项目在中间加一层，让部门的 Dify 应用能问到所有经授权可见的库，文档和索引始终留在原地。

设计上只有一条原则：**读走中心，写留部门**。检索必须过中心，因为要鉴权和聚合；增删改由部门直接在自己 Dify 里做，中心不参与，也没有写接口。

检索能力来自各部门已有的 Dify，这一层只负责认证、路由、融合和审计。

## 结构

```
               部门 Dify 应用（配成"外部知识库"）
                        │  Bearer 部门 token
                        ▼
        ┌────────  center :8080  ────────┐
        │  认证 → 授权 → 路由 → 融合 → 审计  │
        └──┬───────────┬───────────┬─────┘
           │ X-Federation-Key（唯一的跨机方向）
       node-hr      node-fin     node-a
        :8001        :8001       :8001
           │            │           │
      Dify 库 1      Dify 库 2    Dify 库 3
```

两个服务、两套镜像，分别部署：

- `center`：中心。存部门、token、库登记、授权、审计。不存文档，也不存任何 Dify 凭证。
- `node`：部门这一侧，310 行。只做三件事：认中心的密钥、把库名翻译成自家 Dify 的地址、把结果原样交回。

这么拆的实际好处是：中心不知道任何 Dify 地址和 key，公司层面泄露不了部门凭证；Dify 不用对外开端口，只暴露一个 node；加部门时中心只改一行配置，不用重发镜像。

## 跑起来

需要 Python 3.12 和四个包（fastapi / uvicorn / httpx / pydantic），测试另加 pytest。

```
make test      # 65 个用例，不联网
make accept    # 24 条断言，进程内跑通 中心 → 节点 → 假 Dify
```

Windows 上用 `powershell -ExecutionPolicy Bypass -File .\make.ps1 test`。

想开真容器（要先装 Docker）：

```
docker compose -f docker-compose.dev.yml up -d --build
curl localhost:8080/healthz
```

这时有四个容器：center、fake-dify、node-hr、node-fin。fake-dify 是内存里写死的假知识库，用来跑通链路。

再叠一个指向真实 Dify 的节点：

```
python scripts/dify_setup.py --dataset hr-policy --base-url http://host.docker.internal
docker compose -f docker-compose.dev.yml -f docker-compose.dify.yml up -d
python scripts/smoke_real_dify.py      # 11 条
python scripts/dify_external_kb.py     # 8 条，Dify 反过来调联邦
```

`dify_setup.py` 会登录本机 Dify 控制台、建知识库 API key、生成 `dev/node-dify.toml`。账号密码通过 `--email/--password` 或环境变量 `DIFY_EMAIL` / `DIFY_PASSWORD` 传入，脚本里没有默认值。

## 部署

### 1. 先出镜像

仓库里只有源码。`deploy/` 下的 compose 直接引用 `solirag-center:0.1.0` 和 `solirag-node:0.1.0`，所以要在能上外网的机器上先构建：

```
docker build -t solirag-center:0.1.0 ./center
docker build -t solirag-node:0.1.0   ./node
```

分发看公司有没有内部 registry：

```
# 有
docker tag solirag-node:0.1.0 registry.corp/solirag-node:0.1.0
docker push registry.corp/solirag-node:0.1.0

# 没有
docker save solirag-node:0.1.0 -o solirag-node-0.1.0.tar
docker load -i solirag-node-0.1.0.tar     # 部门机器上执行
```

生产机不需要源码，容器跑的是镜像里的代码。完全隔离的内网要提前准备 `python:3.12-slim` 和 PyPI 镜像源，否则构建会失败。

### 2. 中心

```
cd deploy/center
cp .env.example .env
```

`.env` 里三项必须改：`SOLIRAG_ADMIN_TOKEN`、`SOLIRAG_NODE_KEY`、`SOLIRAG_NODES`。最后一项写节点的内网地址，不是 Dify 的地址。

```
docker compose up -d
curl localhost:8080/healthz
```

建部门、发 token：

```
curl -X POST localhost:8080/admin/departments -H "Authorization: Bearer $ADMIN" -d '{"name":"hr"}'
curl -X POST localhost:8080/admin/tokens -H "Authorization: Bearer $ADMIN" \
     -d '{"dept":"hr","scopes":["read","grant"]}'
```

token 明文只出现这一次，记下来交给部门。丢了只能重发。

### 3. 部门

```
cd deploy/node
cp ../../node/node.example.toml node.toml
```

要填四项：本部门 Dify 的知识库地址、知识库 UUID、知识库 API key、`federation_key`（和中心的 `SOLIRAG_NODE_KEY` 一致）。

还有一项容易踩：`search_method`。库是 economy 索引就必须写 `keyword_search`，写成 `semantic_search` 会直接 400。

```
docker compose up -d
```

8001 只允许中心访问，其余一律拒绝。Dify 自己不需要对外。

### 4. 部门在中心登记自己的库

```
curl -X PUT http://center:8080/v1/datasets/hr \
  -H "Authorization: Bearer $HR_TOKEN" \
  -d '{"node":"node-hr","visibility":"shared","grants":[{"dept":"fin"}]}'
```

这一步由部门自己做，不需要找中心管理员。中心由此知道"hr 这个库在 node-hr 上"，依然不知道 Dify 地址和 key。

### 5. Dify 里配外部知识库

知识库 → 外部知识库 API → 添加：

- Endpoint 填 `http://center:8080/v1/dify`，到 `/v1/dify` 为止，Dify 会自己补 `/retrieval`
- API Key 填部门 token

然后新建知识库，选"连接外部知识库"，`knowledge_id` 填 `global`（全部可见库）或 `hr`（只查一个）。

Dify 的召回阈值建议设成 0。对外已经归一化到首条 1.0，但阈值留着容易把结果全砍掉。

### 6. 确认能查到

```
curl localhost:8001/v1/node/stats -H "X-Federation-Key: $NODE_KEY"
curl -s http://center:8080/v1/dify/retrieval \
     -H "Authorization: Bearer $HR_TOKEN" \
     -d '{"knowledge_id":"global","query":"劳动合同"}'
```

第二条返回里的 `federation.searched_count` 应该等于这个部门可见的库数量。

## 权限

授权由数据的主人决定，平台方没有接口能干预。

- `owner_dept` 由 token 决定，登记后不能过户
- 只有 owner 能改自己的 grants；拿到别人库的部门不能再转授
- 撤销和过期都是即时生效，中间没有缓存
- 访问者之间互相不可见：看不到别人被授权了什么，也看不到别人在读什么
- 未授权的库和不存在的库返回一样的结果，防止枚举

owner 能看到自己的库被谁读过、命中几条，但看不到对方的 query。审计里只存 query 的 sha256。

## 检索行为

`knowledge_id` 有三种情况：

| 传什么 | 查哪些库 |
|---|---|
| `global` 或空 | 本部门可见的全部（自己的 + 被授权的） |
| `hr` | 只查这一个，前提是有权限 |
| 别的 | 200 加空数组，同时写一条 denied 审计 |

融合用 RRF，k=60。不用分数，因为各库的索引类型、嵌入模型、打分方式都不一样，有的库干脆不返回分数（Dify 的 economy 索引就全是 0）。RRF 只看名次，对这类差异免疫。

单库超时 1500ms，整体预算 2500ms。部分库不通返回 200 加 `degraded`，全部不通返回 504。不会用 200 加空数组表示故障，那样运维和用户都看不出站点已经挂了。

结果里会带 `metadata.library`，用于追溯这条内容来自哪个库。`federation` 里只给数量不给库名，库名明细只进审计。

## 测试

`make test` 跑 65 个单元测试，不联网也不起容器，覆盖认证、授权、融合、可见性判定和审计链。

另外几个脚本要到真环境才有意义：

```
python scripts/accept.py            # 进程内跑通 center → node → 假 Dify
python scripts/smoke_real.py        # 打真实监听的端口
python scripts/smoke_real_dify.py   # 中心 → 真 Dify
python scripts/dify_external_kb.py  # 真 Dify → 中心
python scripts/dify_app_check.py    # Dify 应用问答
```

整套东西在一台 Windows 机器上跑通过（Docker Desktop、Dify 1.18、Qdrant），没有生产流量，也没压测。

## 踩过的坑

联调时卡住的几个地方，都不是自己代码的问题，是 Dify 的行为：

- Dify 出站默认走 ssrf_proxy，会拦内网地址。中心在内网时必须放开私有网段，否则登记外部知识库时报"连不上"，但用 curl 手动试又是通的。
- economy 索引的库没有向量，`search_method` 必须写 `keyword_search`，写 `semantic_search` 直接 400。
- Dify 发过来的 `top_k` 可能是字符串，按整数强校验会 422。
- 新建的外部知识库 `retrieval_model` 是 NULL，不在应用内检索前补一次，会崩在 `'NoneType' object has no attribute 'get'`。
- 登录接口的 password 字段是 base64 编码，不是明文；token 在 httpOnly cookie 里，后续请求还要带 CSRF。

## 目录

```
soliRAG/
├── center/                        中心服务（控制面）
│   ├── Dockerfile                 基于 python:3.12-slim，装四个依赖
│   ├── pyproject.toml
│   ├── src/solirag_center/
│   │   ├── main.py                所有端点、依赖注入、统一错误、request id
│   │   ├── registry.py            部门/token/数据集登记、grants、可见性判定
│   │   ├── retrieve.py            检索编排：展开 → 扇出 → RRF → 去重 → 归一化
│   │   ├── audit.py               审计写入、三方视图、哈希链校验
│   │   ├── nodes.py               调节点的 HTTP 客户端（超时、错误映射）
│   │   ├── auth.py                部门 token 与 admin token 的认证
│   │   ├── models.py              pydantic 模型，含对 Dify 脏数据的宽松转换
│   │   ├── db.py                  SQLite 连接与建表（5 张表）
│   │   ├── config.py              环境变量
│   │   └── util.py                时间、哈希、文本规范化
│   └── tests/                     49 个用例，conftest 里搭了 hr/fin/rd 三方世界
│
├── node/                          部门节点，部署在部门自己的机器上
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── node.example.toml          部门唯一要改的文件
│   ├── src/solirag_node/
│   │   ├── main.py                三个端点 + 三道校验
│   │   ├── dify.py                唯一知道 Dify API 形状的文件
│   │   └── config.py              读 TOML
│   └── tests/test_node.py         16 个用例
│
├── deploy/                        生产部署，只有 compose
│   ├── center/docker-compose.yml  中心服务器上唯一的服务
│   ├── center/.env.example        九个配置项
│   └── node/docker-compose.yml    部门机器上拷一份
│
├── dev/                           没有 Dify 也能把链路跑通
│   ├── fake_dify.py               假 Dify，内存里两篇语料
│   ├── mock_llm.py                OpenAI 兼容的占位模型
│   ├── Dockerfile                 假 Dify 的镜像
│   ├── Dockerfile.mock-llm        占位模型的镜像
│   └── node-*.toml                四份节点配置，容器版和本机版各两套
│
├── docs/                          设计约定
│   ├── access-contract.md         谁能登记、谁能授权、403 矩阵
│   ├── wire-contract.md           中心与节点之间的请求响应、错误码
│   ├── retrieval-contract.md      knowledge_id 语义、融合、降级
│   └── audit-contract.md          审计结构、三方视图、哈希链
│
├── scripts/                       集成与启动
│   ├── accept.py                  进程内跑通 center → node → 假 Dify
│   ├── smoke_real.py              打真实监听的端口
│   ├── smoke_real_dify.py         中心 → 真 Dify
│   ├── dify_external_kb.py        真 Dify → 中心
│   ├── dify_app_check.py          在 Dify 里建应用并问答
│   ├── dify_setup.py              登录 Dify 建知识库 key，生成节点配置
│   └── run_local.py               本机起四个进程
│
├── docker-compose.dev.yml         本地四个容器
├── docker-compose.dify.yml        叠加真 Dify 节点和占位模型
├── Makefile / make.ps1
├── README.md
└── LICENSE
```

几个约定：

- 两个服务之间**没有共享代码**，只通过 `docs/wire-contract.md` 那套 JSON 通信。中心升级不影响节点，节点也不用跟着发版。
- `center` 内部是单向依赖：`main` 调其余模块；`retrieve` 只依赖 `registry`（取目标库）、`nodes`（发请求）、`audit`（记一行），自己不碰 SQL。
- 契约在 `docs/`。代码和契约冲突时以契约为准，改契约要同时改对应测试。

改代码时一般动这几个地方：

| 想改什么 | 看哪个文件 |
|---|---|
| 融合算法、超时预算 | `center/src/solirag_center/retrieve.py` |
| 授权规则、可见性 | `center/src/solirag_center/registry.py` |
| 审计字段或可见范围 | `audit.py` + `db.py` 的建表语句 |
| 换检索后端（比如直连向量库） | `node/src/solirag_node/dify.py`，对外签名不用动 |
| 加一个接口 | `center/src/solirag_center/main.py` + `models.py` |



## 依赖

Python 3.12，四个包：fastapi、uvicorn、httpx、pydantic。测试另需 pytest。
中心用标准库 sqlite3，节点配置用标准库 tomllib。

## License

MIT，见 [LICENSE](LICENSE)。
