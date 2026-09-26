-- 稽核后台存储结构。
--
-- 设计原则（对应 docs/domain.md）：
--
-- 1. 原始报文不可变：任何来源的原始 JSON 先落 raw_records，内容哈希去重；
--    业务表只保存由原始报文派生的数据，并回指原始记录。
-- 2. 更正只能追加版本：业务事实表带版本号，更正产生新版本，旧版本保留在
--    *_versions 中；当前视图通过 current_* 视图读取。
-- 3. 风险不等于违法：risk_clues 只保存由规则引擎算出的"提示"，其结论字段
--    （status 的处置）只能由具备权限的人工账号填写。
-- 4. 全程留痕：研判、决定、封存、交接、查询均写审计日志；封存后生成只读快照。

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ---------------------------------------------------------------------------
-- 0. 账号与角色（分级查看的基础）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    user_id     TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('case_worker','sealing_officer','public')),
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ---------------------------------------------------------------------------
-- 1. 原始来源（六类来源统一入口，WORM）
-- ---------------------------------------------------------------------------
-- source_type:
--   coding       药品赋码与扫码事件
--   settlement   参保人就医结算
--   voucher      票账货款材料
--   seizure      仓库扣押登记
--   online       网络销售
--   cooperation  办案协作
CREATE TABLE IF NOT EXISTS raw_batches (
    batch_id        TEXT PRIMARY KEY,
    source_type     TEXT NOT NULL CHECK (source_type IN
                        ('coding','settlement','voucher','seizure','online','cooperation')),
    source_org      TEXT NOT NULL,          -- 报送单位，原始来源标识
    submitted_by    TEXT,
    submitted_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    record_count    INTEGER NOT NULL DEFAULT 0,
    deduped_count   INTEGER NOT NULL DEFAULT 0,  -- 与历史内容重复而跳过的条数
    note            TEXT
);

CREATE TABLE IF NOT EXISTS raw_records (
    record_id       TEXT PRIMARY KEY,           -- 系统对单条原始报文的编号
    batch_id        TEXT NOT NULL REFERENCES raw_batches(batch_id),
    source_type     TEXT NOT NULL,
    source_org      TEXT NOT NULL,
    source_record_key TEXT,                    -- 来源方自身的记录号（可为空）
    content_hash    TEXT NOT NULL,             -- sha256(规范化JSON)
    payload         TEXT NOT NULL,             -- 原始 JSON 报文，原样保存
    observed_at     TEXT NOT NULL,             -- 报文所述业务发生时间（迟到数据判定依据）
    received_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    is_late         INTEGER NOT NULL DEFAULT 0,
    UNIQUE (source_type, content_hash)         -- 同一来源、相同内容只允许一份
);

CREATE INDEX IF NOT EXISTS idx_raw_observed ON raw_records(observed_at);

-- ---------------------------------------------------------------------------
-- 2. 业务事实（追加版本式； *_history 保留每一次版本）
-- ---------------------------------------------------------------------------
-- 药品主档：由赋码来源建立，一药一码
CREATE TABLE IF NOT EXISTS medicines (
    medicine_id     TEXT PRIMARY KEY,          -- 追溯码（涂改后以平台记录为准）
    code_status     TEXT NOT NULL DEFAULT 'normal'
                    CHECK (code_status IN ('normal','damaged','altered')),
    product_name    TEXT,
    spec            TEXT,
    manufacturer    TEXT,
    batch_no        TEXT,
    produced_at     TEXT,
    is_cold_chain   INTEGER NOT NULL DEFAULT 0,
    current_version INTEGER NOT NULL DEFAULT 1,
    first_record_id TEXT NOT NULL REFERENCES raw_records(record_id),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS medicine_versions (
    medicine_id     TEXT NOT NULL REFERENCES medicines(medicine_id),
    version         INTEGER NOT NULL,
    record_id       TEXT NOT NULL REFERENCES raw_records(record_id),
    code_status     TEXT NOT NULL,
    product_name    TEXT,
    spec            TEXT,
    manufacturer    TEXT,
    batch_no        TEXT,
    produced_at     TEXT,
    is_cold_chain   INTEGER NOT NULL DEFAULT 0,
    changed_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    change_reason   TEXT,
    PRIMARY KEY (medicine_id, version)
);

-- 扫码/流通事件（仅追加；迟到数据照常落库并打标）
CREATE TABLE IF NOT EXISTS scan_events (
    event_id        TEXT PRIMARY KEY,
    medicine_id     TEXT NOT NULL REFERENCES medicines(medicine_id),
    seq             INTEGER NOT NULL,          -- 来源提供的环节序号
    event_type      TEXT NOT NULL CHECK (event_type IN
                        ('produce','warehouse','distribute','hospital_in',
                         'dispense','settlement_scan','return','resale','online_order',
                         'seizure','other')),
    org_code        TEXT NOT NULL,
    org_name        TEXT,
    region_code     TEXT NOT NULL,             -- 省级或地区编码
    event_time      TEXT NOT NULL,
    record_id       TEXT NOT NULL REFERENCES raw_records(record_id),
    UNIQUE (medicine_id, event_type, org_code, event_time)
);
CREATE INDEX IF NOT EXISTS idx_scan_med_time ON scan_events(medicine_id, event_time);
CREATE INDEX IF NOT EXISTS idx_scan_region ON scan_events(region_code);

-- 参保人与就医结算（更正追加版本）
CREATE TABLE IF NOT EXISTS persons (
    person_id       TEXT PRIMARY KEY,          -- 系统内部脱敏ID，不存明文证件号
    id_hash         TEXT UNIQUE,               -- 证件号 sha256，仅用于比对
    current_version INTEGER NOT NULL DEFAULT 1,
    first_record_id TEXT NOT NULL REFERENCES raw_records(record_id)
);
CREATE TABLE IF NOT EXISTS person_versions (
    person_id       TEXT NOT NULL REFERENCES persons(person_id),
    version         INTEGER NOT NULL,
    record_id       TEXT NOT NULL REFERENCES raw_records(record_id),
    surname         TEXT,                      -- 仅存姓氏，供展示
    masked_id_no    TEXT,                      -- 脱敏证件号
    region_code     TEXT,
    changed_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (person_id, version)
);

CREATE TABLE IF NOT EXISTS settlements (
    settlement_id   TEXT PRIMARY KEY,
    person_id       TEXT NOT NULL REFERENCES persons(person_id),
    medicine_id     TEXT NOT NULL REFERENCES medicines(medicine_id),
    current_version INTEGER NOT NULL DEFAULT 1,
    first_record_id TEXT NOT NULL REFERENCES raw_records(record_id)
);
CREATE TABLE IF NOT EXISTS settlement_versions (
    settlement_id   TEXT NOT NULL REFERENCES settlements(settlement_id),
    version         INTEGER NOT NULL,
    record_id       TEXT NOT NULL REFERENCES raw_records(record_id),
    med_inst_code   TEXT NOT NULL,             -- 结算医疗机构（最初结算机构的答案在此）
    med_inst_name   TEXT,
    region_code     TEXT NOT NULL,
    diagnosis       TEXT,
    prescribed_at   TEXT NOT NULL,             -- 开药时间
    settled_at      TEXT NOT NULL,             -- 结算时间
    quantity        INTEGER NOT NULL,
    amount          REAL NOT NULL,             -- 医保支付金额（资金线）
    fund_type       TEXT,                      -- 统筹/个账等
    is_cross_region INTEGER NOT NULL DEFAULT 0,-- 跨省就医直接结算标记
    status          TEXT NOT NULL DEFAULT 'valid'
                    CHECK (status IN ('valid','corrected','voided')),
    changed_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    change_reason   TEXT,
    PRIMARY KEY (settlement_id, version)
);
CREATE INDEX IF NOT EXISTS idx_settle_person ON settlements(person_id);
CREATE INDEX IF NOT EXISTS idx_settle_med ON settlements(medicine_id);

-- 票账货款材料（发票/账册/随货同行/冷链温控），可追加更正版本
CREATE TABLE IF NOT EXISTS vouchers (
    voucher_id      TEXT PRIMARY KEY,
    medicine_id     TEXT REFERENCES medicines(medicine_id),  -- 可能按批次关联，可空
    settlement_id   TEXT REFERENCES settlements(settlement_id),
    current_version INTEGER NOT NULL DEFAULT 1,
    first_record_id TEXT NOT NULL REFERENCES raw_records(record_id)
);
CREATE TABLE IF NOT EXISTS voucher_versions (
    voucher_id      TEXT NOT NULL REFERENCES vouchers(voucher_id),
    version         INTEGER NOT NULL,
    record_id       TEXT NOT NULL REFERENCES raw_records(record_id),
    voucher_type    TEXT NOT NULL CHECK (voucher_type IN
                        ('invoice','ledger','shipping','cold_chain','payment','other')),
    doc_no          TEXT,
    party_org       TEXT,
    amount          REAL,
    region_code     TEXT,
    issued_at       TEXT,
    -- 冷链材料专用字段：材料声称的储运温度区间与时段
    temp_min        REAL,
    temp_max        REAL,
    temp_recorded   REAL,                      -- 实际/第三方探头温度
    cold_window_start TEXT,
    cold_window_end   TEXT,
    status          TEXT NOT NULL DEFAULT 'valid'
                    CHECK (status IN ('valid','corrected','voided')),
    changed_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    change_reason   TEXT,
    PRIMARY KEY (voucher_id, version)
);

-- 仓库扣押登记
CREATE TABLE IF NOT EXISTS seizures (
    seizure_id      TEXT PRIMARY KEY,
    case_id         TEXT REFERENCES cases(case_id),
    warehouse_org   TEXT NOT NULL,
    region_code     TEXT NOT NULL,
    seized_at       TEXT NOT NULL,
    officer         TEXT,
    record_id       TEXT NOT NULL REFERENCES raw_records(record_id),
    note            TEXT
);
CREATE TABLE IF NOT EXISTS seizure_items (
    seizure_id      TEXT NOT NULL REFERENCES seizures(seizure_id),
    medicine_id     TEXT NOT NULL REFERENCES medicines(medicine_id),
    pkg_condition   TEXT NOT NULL DEFAULT 'intact'
                    CHECK (pkg_condition IN ('intact','damaged','altered_code')),
    observed_code   TEXT,                      -- 现场辨认/还原出的码
    qty             INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (seizure_id, medicine_id)
);

-- 网络销售
CREATE TABLE IF NOT EXISTS online_sales (
    online_id       TEXT PRIMARY KEY,
    medicine_id     TEXT NOT NULL REFERENCES medicines(medicine_id),
    platform        TEXT NOT NULL,
    shop_name       TEXT,
    region_code     TEXT NOT NULL,            -- 发货/收货地
    listing_time    TEXT,
    sold_at         TEXT NOT NULL,
    buyer_region    TEXT,
    price           REAL,
    record_id       TEXT NOT NULL REFERENCES raw_records(record_id)
);
CREATE INDEX IF NOT EXISTS idx_online_med ON online_sales(medicine_id);

-- 办案协作（异地协查、监控线索、物流单据等）
CREATE TABLE IF NOT EXISTS cooperations (
    coop_id         TEXT PRIMARY KEY,
    case_id         TEXT REFERENCES cases(case_id),
    from_org        TEXT NOT NULL,
    to_org          TEXT NOT NULL,
    channel         TEXT CHECK (channel IN
                        ('cross_province','logistics','surveillance','fund','other')),
    request_at      TEXT,
    response_at     TEXT,
    payload_summary TEXT NOT NULL,
    record_id       TEXT NOT NULL REFERENCES raw_records(record_id)
);

-- ---------------------------------------------------------------------------
-- 3. 风险线索（只提示，不定性）
-- ---------------------------------------------------------------------------
-- clue_type:
--   cross_region_reappear 同一药盒在不同地区再次出现
--   repeat_prescription   同一天短间隔重复开药
--   cold_chain_mismatch   冷链材料与真实流转不符
CREATE TABLE IF NOT EXISTS risk_clues (
    clue_id         TEXT PRIMARY KEY,
    clue_type       TEXT NOT NULL CHECK (clue_type IN
                        ('cross_region_reappear','repeat_prescription','cold_chain_mismatch')),
    medicine_id     TEXT REFERENCES medicines(medicine_id),
    person_id       TEXT REFERENCES persons(person_id),
    severity        TEXT NOT NULL CHECK (severity IN ('low','medium','high')),
    title           TEXT NOT NULL,
    -- 可解释依据：规则名、命中数据及其版本、阈值、推理说明
    explanation     TEXT NOT NULL,            -- JSON: {rule, inputs:[{...}], threshold, rationale}
    evidence_hash   TEXT NOT NULL,            -- 命中输入快照哈希，保证线索可复核
    -- 业务身份去重键：同一规则、同一业务主体（药盒/参保人+日期）只生成一条线索，
    -- 数据被更正产生新版本后重跑规则不会重复造线索；新依据版本在研判快照中留痕
    dedup_key       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','in_review','dismissed','confirmed_transfer')),
    created_by_rule TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    -- 并乐观锁：多人同时研判同一条线索
    lock_owner      TEXT REFERENCES users(user_id),
    lock_token      TEXT,
    locked_at       TEXT,
    version         INTEGER NOT NULL DEFAULT 1,
    UNIQUE (clue_type, dedup_key)
);
CREATE INDEX IF NOT EXISTS idx_clue_status ON risk_clues(status);
CREATE INDEX IF NOT EXISTS idx_clue_med ON risk_clues(medicine_id);

-- ---------------------------------------------------------------------------
-- 4. 案件、人工决定、封存、交接
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cases (
    case_id         TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','sealed','transferred','closed')),
    created_by      TEXT REFERENCES users(user_id),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    sealed_at       TEXT,
    transferred_at  TEXT
);
CREATE TABLE IF NOT EXISTS case_clues (
    case_id         TEXT NOT NULL REFERENCES cases(case_id),
    clue_id         TEXT NOT NULL REFERENCES risk_clues(clue_id),
    added_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (case_id, clue_id)
);

-- 研判意见与人工决定（追加，不允许修改/删除）
CREATE TABLE IF NOT EXISTS decisions (
    decision_id     TEXT PRIMARY KEY,
    clue_id         TEXT NOT NULL REFERENCES risk_clues(clue_id),
    case_id         TEXT REFERENCES cases(case_id),
    reviewer        TEXT NOT NULL REFERENCES users(user_id),
    action          TEXT NOT NULL CHECK (action IN
                        ('note','escalate','request_coop','dismiss','confirm_for_transfer')),
    rationale       TEXT NOT NULL,
    -- 研判时采用的数据版本，保证"从线索还原当时依据"
    data_versions   TEXT NOT NULL,            -- JSON: {settlement:{id:version}, medicine:{id:version}, ...}
    decided_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_decision_clue ON decisions(clue_id, decided_at);

-- 证据封存：对案件在某时点的全部证据做只读快照
CREATE TABLE IF NOT EXISTS seals (
    seal_id         TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL REFERENCES cases(case_id),
    sealed_by       TEXT NOT NULL REFERENCES users(user_id),
    sealed_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    manifest        TEXT NOT NULL,            -- JSON: 每类对象的 {id: version, record_id, hash}
    manifest_hash   TEXT NOT NULL,            -- sha256(规范化 manifest)
    prev_seal_hash  TEXT,                     -- 同一案件多次封存形成哈希链
    note            TEXT
);

-- 交接回执：移送/跨部门交接
CREATE TABLE IF NOT EXISTS handoffs (
    handoff_id      TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL REFERENCES cases(case_id),
    seal_id         TEXT NOT NULL REFERENCES seals(seal_id),
    from_org        TEXT NOT NULL,
    to_org          TEXT NOT NULL,
    handler         TEXT NOT NULL REFERENCES users(user_id),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','received','rejected')),
    -- 接收方回执
    receipt_org     TEXT,
    receiver        TEXT,
    receipt_no      TEXT,
    received_at     TEXT,
    remark          TEXT
);

-- ---------------------------------------------------------------------------
-- 5. 访问审计（公众查询也要留痕，但审计行不含敏感查询结果）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS access_log (
    log_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor           TEXT,
    role            TEXT,
    action          TEXT NOT NULL,
    target_type     TEXT,
    target_id       TEXT,
    result          TEXT NOT NULL DEFAULT 'ok',
    at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    detail          TEXT
);
CREATE INDEX IF NOT EXISTS idx_access_at ON access_log(at);

-- 当前版本便捷视图
CREATE VIEW IF NOT EXISTS current_settlement AS
SELECT s.settlement_id, s.person_id, s.medicine_id, s.current_version,
       v.med_inst_code, v.med_inst_name, v.region_code, v.diagnosis,
       v.prescribed_at, v.settled_at, v.quantity, v.amount, v.fund_type,
       v.is_cross_region, v.status, v.record_id, v.version, v.changed_at
FROM settlements s
JOIN settlement_versions v
  ON v.settlement_id = s.settlement_id AND v.version = s.current_version;

CREATE VIEW IF NOT EXISTS current_voucher AS
SELECT v.voucher_id, v.medicine_id, v.settlement_id, v.current_version,
       h.voucher_type, h.doc_no, h.party_org, h.amount, h.region_code,
       h.issued_at, h.temp_min, h.temp_max, h.temp_recorded,
       h.cold_window_start, h.cold_window_end, h.status, h.record_id, h.version
FROM vouchers v
JOIN voucher_versions h
  ON h.voucher_id = v.voucher_id AND h.version = v.current_version;

CREATE TRIGGER IF NOT EXISTS trg_seal_freeze_cases
    BEFORE UPDATE OF status ON cases
    WHEN OLD.status = 'sealed' AND NEW.status = 'open'
BEGIN
    SELECT RAISE(ABORT, '案件已封存，不能解除封存状态（如需继续请立案）');
END;
