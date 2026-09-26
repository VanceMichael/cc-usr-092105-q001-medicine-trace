# 稽核后台设计说明

面向"回流药跨省证据归并"场景的完整后台实现。仅依赖 Python 3.11 标准库，
以 SQLite 为持久化。本文档说明架构、数据模型、关键不变量、接口与部署注意。
代码结构：

| 模块 | 职责 |
|---|---|
| `src/store.py` | 仅追加存储、哈希链、版本化更正、重复上传标记、迟到水位 |
| `src/rules.py` | 三类风险规则（纯逻辑、参数化、带规则版本） |
| `src/services.py` | 批量作业、数据切点、线索版本、并发研判、封存、交接、谱系 |
| `src/access.py` | 公众单盒核验（白名单脱敏） |
| `src/api.py` | stdlib HTTP/JSON 接口与角色鉴权 |
| `src/seed.py` | 宜宾跨省场景虚构演示数据 |
| `src/demo.py` | 端到端演示（`python -m src.demo`） |
| `src/server.py` | 服务入口（`python -m src.server --seed --with-late`） |

## 一、核心原则（不可绕过）

1. **原始来源逐字保存**：六类来源报文以规范 JSON 序列化后原文入 `source_record`，
   系统不提供任何 UPDATE/DELETE 业务接口。
2. **更正只能追加版本**：同业务键再来不同报文，生成 v2、v3……，`supersedes`
   指向旧版，必须附 `correction_reason`；旧版永远可查。
3. **风险提示 ≠ 违法认定**：规则只产出 severity（关注程度）+ 可复核解释，
   线索状态机中没有任何"违法"状态；"confirm_risk"仅表示**风险成立（人工）**，
   定性以司法/行政程序为准，移送后以受案机关结论为准。
4. **人工优先于机器**：人工 `dismiss/confirm_risk/refer` 后，定时/迟到复评
   仍会追加机器版本留痕，但**不回写状态**。
5. **最小可见**：公众只能拿到单盒药品止于"发药给患者"的公开流转摘要；
   扣押、网售、案件、参保人、金额、风险信息一律不进入公众视图。
6. **可还原**：任何一条线索都能还原"当时采用的数据切点 + 输入证据版本 +
   规则版本/参数 + 每条人工决定及其修订号 + 封存哈希 + 交接回执"。

## 二、来源模型

| source_kind | 内容 | 关键业务字段 |
|---|---|---|
| `trace_event` | 药品赋码与扫码事件（一药一码） | box_code、event_type、event_time、region、org |
| `settlement` | 参保人就医结算 | insured_id、settle_time、region、items[box_code] |
| `voucher` | 票、账、货、款（invoice/payment/cold_chain…） | doc_no、box_code、segments |
| `seizure` | 仓库扣押登记 | boxes[{box_code,pkg_damaged,code_altered}] |
| `online_sale` | 网络销售取证 | box_code、platform、post_time、快照号 |
| `cooperation` | 跨省办案协作函件 | from_org/to_org、subject |

每条记录保留：出具单位、原始单号、**业务时间**（来源自报）与
**入库时间**（系统时间）双时钟、上传批次、报文 SHA-256 摘要、去重指纹、
哈希链前后指针。

### 重复上传
按 `(来源类型, 业务键, 报文摘要)` 计算去重指纹。完全重复的报文不产生新版本，
但写入一条 `version=0, duplicate_of=原记录` 的**送达痕迹**，保证"谁在何时
重复传过"本身可审计；批次统计区分 stored/duplicates/rejected。

### 迟到数据
每类来源维护按业务时间推进的高水位线。业务时间早于水位线才送达的记录标记
为 late（批次统计 `late/late_keys`），照常入库并可触发 `trigger='late_data'`
的复评作业。迟到绝不插队改写历史，只形成新的当前版本或补单。

### 防篡改哈希链
`record_hash = SHA256(prev_hash | payload_digest | received_at | kind|key|version)`，
从 GENESIS 起逐条链接。`verify_chain()` 同时重算每条报文摘要与链指针；
直接改动库文件中任一 `payload_json/record_hash` 都会在被改动的 id 处断链。
封存包在此之外提供案件级清单哈希，供跨省交接时对方独立复核。

## 三、风险规则

规则均为纯函数：输入当前版本投影 + 参数 + 范围（可限定 box_codes/
insured_ids），输出 `Finding`。所有 Finding 绑定规则代码与规则版本。

| 规则 | 触发逻辑 | 定级 |
|---|---|---|
| `REAPPEAR-01` 同一药盒跨区域再现（v1.0） | 仅考察**医保结算之后**：实物扫码/网售/扣押出现在结算省之外（避免把厂家→医院的正常跨省流通误报）；参数 `min_settle_to_reappear_hours=24` | 异地再现且包装受损/网售为 high |
| `REPEAT-RX-01` 同日短间隔重复开药（v1.0） | 同一参保人同日相邻两笔结算 ≤ `min_gap_minutes=60` 分钟（默认仅不同机构）；区分是否跨省、是否同类药 | 跨省+同类 high；跨省 medium；其余 low |
| `COLD-01` 冷链材料与真实流转不符（v1.0） | 运单承运窗口内：实物扫码无时段覆盖（uncovered_movement）、票载区域与扫码省不符（region_conflict）、实测温度越限（temp_excursion） | 区域冲突 high，其余 medium |

每条线索解释包含：触发事实、可读时间线、证据清单（每条精确到
`record_id/version/digest`）、以及"系统不作违法认定"的明示。
规则参数随作业持久化进线索版本，事后可复现"当时为什么报、按什么阈值报"。
正常药盒（`BOX8102…` 演示数据）在任何规则下都不会产生线索。

## 四、作业、切点与线索版本

- `batch_job`：每次批量交叉比对登记作业（manual/scheduled/late_data），
  开始前先冻结**数据切点** `data_cut`（max_record_id + 当前投影全量摘要）。
- `risk_lead`：规则×主体唯一。首次出现即建线索；后续作业命中同一主体时
  追加 `lead_version`（绑定新切点、参数、证据快照、解释），**永不覆盖**。
- 人工状态与机器版本分离：状态机
  `open → reviewing → confirmed_risk / dismissed / referred`，
  全部由人工动作驱动；机器复评不改状态。
- `revision` 修订号：任何人工决定或机器新版本都使其 +1。研判提交时带
  `expected_version`（乐观锁/CAS），过期提交收到 409，必须重读后再决定。
  多人并发时只有一人写入成功，保证决定序号连续、无丢失更新。

## 五、案件、封存与跨省交接

- 案件 `case_file` + 成员 `case_member`（supervisor/investigator/viewer）。
  investigator 只能操作其参与的案件；viewer 只读；非成员一律拒绝。
- `link_lead` 在线索上追加一条 `claim` 决定（纳入案件），同样只追加。
- **封存**：汇总案件全部线索全部版本引用到的来源记录，生成排序清单
  `manifest` 与封存哈希 `SHA256(case_no|manifest)`；封存是只读快照，
  可重复执行且结果幂等。`verify_seal` 逐条比对记录当前摘要。
- **交接**：交接前必须已封存；`transfer` 登记接收单位/地区。接收方执行
  `receive_transfer` 时系统**如实**复算哈希：通过则出具
  `package_intact=1` 回执，不通过则 0 并列出差异记录、提示不得签收采信。
  回执（接收人、单位、时间、复算哈希）随案永久保存。

## 六、谱系还原（`GET /api/leads/{no}/lineage`）

从一条线索输出：

```
线索（规则、状态）
└─ 机器版本 v1..vn：作业号、规则版本、参数、解释、severity
    └─ data_cut：切点号 / max_record_id / 切点摘要
    └─ evidence：[{来源, 业务键, record_id, 版本, 摘要, 说明}]
└─ 人工决定 seq 1..n：动作、人、意见、基于修订号、时间
└─ 关联案件：封存包（哈希/条数/时间）→ 交接单 → 签收回执（完整性、复算哈希）
```

## 七、HTTP 接口

身份假定由政务统一网关注入请求头 `X-User-Id`、`X-Role`。

| 方法 路径 | 角色 | 说明 |
|---|---|---|
| GET `/api/health` | 任意 | 健康检查 |
| GET `/api/public/verify/{box_code}` | 匿名 | **公众单盒核验**，白名单脱敏输出 |
| POST `/api/ingest/{source_kind}` | investigator+ | 批量入库（自动版本/去重/迟到处理） |
| POST `/api/batch-jobs` | investigator+ | 触发批量交叉比对 |
| GET `/api/leads`、`/api/leads/{no}` | 内部 | 列表/详情（含全部机器版本与决定） |
| GET `/api/leads/{no}/lineage` | 内部 | 全链路谱系还原 |
| POST `/api/leads/{no}/decisions` | investigator+ | 人工动作（claim/annotate/start_review/confirm_risk/dismiss/refer），带乐观锁 |
| POST `/api/cases` | supervisor | 建案 |
| POST `/api/cases/{no}/members` | supervisor | 成员管理 |
| POST `/api/cases/{no}/links` | 案件成员 | 线索纳入案件 |
| POST `/api/cases/{no}/seal` | 案件成员 | 封存 |
| GET `/api/cases/{no}/seal` | 内部 | 封存核验 |
| POST `/api/cases/{no}/transfer` | supervisor | 发起跨省交接（须先封存） |
| POST `/api/transfers/{no}/receive` | investigator+ | 接收方核验并出回执 |

错误约定：403 权限不足、404 不存在、409 修订号冲突、400 请求不合法。

### 公众核验输出白名单
盒码脱敏（前6后4）、药品通用名/规格/厂家、**止于发药**的公开追溯环节、
配发机构与日期（无参保人、无金额）、结论性摘要、通用维权提示。
查无此码与正常记录返回结构一致，避免接口被用于枚举盒码。
即使药品已涉案，公众视图中也不出现扣押/网售/案件/风险等任何侦查信息。

## 八、运行

```bash
# 端到端故事（不落盘）
python3 -m src.demo

# 启动服务并在空库灌入演示数据
python3 -m src.server --db data/audit.db --seed --with-late --port 8080

# 全量测试（32 个用例）
python3 -m unittest discover -s tests -v
```

## 九、生产化注意（本实现刻意留白处）

- 身份/角色：当前以请求头模拟，生产须接统一身份认证与网关，禁止外部直连伪造头。
- 并发：SQLite + 进程内写锁适合单实例；多实例部署应换 PostgreSQL，
  乐观锁改用行版本（SELECT ... FOR UPDATE / version 列比较），语义不变。
- WORM：仅追加是应用层约束，合规部署应对数据库账号仅授 INSERT/SELECT，
  并将封存包导出至只读介质或对接区块链/时间戳服务；哈希链提供离线自证。
- 数据安全：报文含个人信息，须启用磁盘加密、传输加密、字段级脱敏与留痕查询审计；
  本仓库演示数据全部虚构。
- 规则治理：规则版本独立于代码发布管理，阈值调整须留痕；新规则上线前应回放
  历史切点评估误报率。
