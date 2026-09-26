# 架构与数据流

纯标准库实现（Python 3.11+，SQLite，无第三方依赖），便于在隔离环境审计与运行。

## 分层

```
scripts/audit_cli.py            命令行（init/ingest/rules/clues/decide/seal/…）
scripts/demo_yibin.py           宜宾跨省回流药端到端演示
src/audit/
  schema.sql                    全部表、索引、视图、封存冻结触发器
  db.py                         连接（WAL、外键、busy_timeout）、规范哈希、异常
  ingest.py                     六类来源统一入库 + 派生 + 版本追加
  rules.py                      三条规则的批量交叉比对，只产线索不定性
  cases.py                      立案/加锁研判/决定/封存/交接/证据链/完整性校验
  query.py                      分级查看（案件全量档案 vs 公众单盒摘要）
```

## 数据模型要点

- **原始层（WORM）**：`raw_batches` / `raw_records`。每条报文存原文 JSON 与
  `sha256(canonical_json(payload))`；唯一约束 `(source_type, content_hash)`
  保证同一来源相同内容永远只有一份，重复上传直接判重。
- **派生层**：由原始报文派生为 `scan_events`、`settlements(+versions)`、
  `vouchers(+versions)`、`seizures/items`、`online_sales`、`cooperations`，
  每条派生行都回指 `record_id`。更正时当前版本号 +1 并向 `*_versions` 追加，
  当前值通过 `current_settlement` / `current_voucher` 视图读取。
- **迟到数据**：当新报文的 `observed_at` 早于该来源已收最新业务时间时打
  `is_late=1`，照常入库，不影响已生成的线索（新现象由人工判断是否补证）。
- **风险层**：`risk_clues` 带 `dedup_key`（规则+药盒 / 规则+参保人+日期），
  数据更正出新版本后重跑规则不会重复造线索；`evidence_hash` 固定命中输入快照。
- **协作/案件层**：`cases`、`case_clues`、`decisions`（含研判时刻版本快照
  `data_versions`）、`seals`（清单哈希 + `prev_seal_hash` 哈希链）、
  `handoffs`（pending→received，回执追加不改写）、`access_log`。

## 并发

- 入库与封存使用 `BEGIN IMMEDIATE` + WAL + `busy_timeout=30s`，多连接/多线程
  同时写入自动串行化，不丢批次。
- 研判采用显式行级语义锁（`lock_owner/lock_token/locked_at`，TTL 30 分钟）：
  同一条线索同一时刻只能由一人下结论；决定必须携带取锁时返回的令牌；锁过期可
  接管并留痕。已验证：6 线程同时抢锁，恰 1 人成功、5 人收到冲突。

## 证据链还原与完整性

从 `clue_id` 出发：命中依据（含原始记录号）→ 历次人工决定及**当时采用的数据
版本** (`decisions.data_versions`) → 所属案件的封存清单哈希与哈希链 →
全部交接与接收回执。`verify_integrity()` 重算原始报文与封存清单的 sha256，
可发现任何对原文或清单的篡改。

## 分级可见性

| 能力 | 案件人员/封存专员 | 公众 |
| --- | --- | --- |
| 完整档案（结算、参保人、票账货款、扣押、网售、协作） | ✅ | ❌ |
| 风险线索清单/证据链 | ✅ | ❌ |
| 原始报文调取 | ✅ | ❌ |
| 单盒合法流转摘要（含中性风险信号） | ✅ | ✅ |
| 研判/立案/封存/交接 | ✅ | ❌ |

公众接口对"结算后又出现非正规流通"只返回布尔级信号与中性提示，不泄露任何
案件、平台、扣押地或参保人字段；每次查询写 `access_log`（不记录返回内容）。

## 六类来源的最小字段

见 `src/audit/ingest.py` 中各 `_require(...)`。通用要求 `observed_at`；
扫码需 `medicine_id/event_type/org_code/region_code/event_time`；
结算需 `settlement_id/person/medicine_id/med_inst_code/region_code/
prescribed_at/settled_at`；票账需 `voucher_id/voucher_type`；
扣押需 `seizure_id/warehouse_org/region_code/seized_at/items[]`；
网售需 `online_id/medicine_id/platform/region_code/sold_at`；
协作需 `coop_id/from_org/to_org/channel/payload_summary`。

## 命令行示例

```bash
export AUDIT_DB=/tmp/audit.db
python3 scripts/audit_cli.py init
python3 scripts/audit_cli.py ingest --source seizure --org 联合执法 --file sz.json
python3 scripts/audit_cli.py rules --window 180
python3 scripts/audit_cli.py clues
python3 scripts/audit_cli.py decide CLUE_ID --action confirm_for_transfer \
    --rationale "证据相互印证，建议移送" --case-id CASE_ID
python3 scripts/audit_cli.py seal CASE_ID --note 移送前封存
python3 scripts/audit_cli.py handoff CASE_ID --to-org 公安
python3 scripts/audit_cli.py receive HANDOFF_ID --receipt-org 公安 \
    --receiver 民警 --receipt-no RCPT-1
python3 scripts/audit_cli.py chain CLUE_ID
python3 scripts/audit_cli.py public-verify 8115-M1-0001
python3 scripts/audit_cli.py integrity
```
