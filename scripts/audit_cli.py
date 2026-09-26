#!/usr/bin/env python3
"""稽核后台命令行。

示例：
  export AUDIT_DB=/tmp/audit.db
  python3 scripts/audit_cli.py init
  python3 scripts/audit_cli.py ingest --source coding --org 追溯平台 --file recs.json
  python3 scripts/audit_cli.py rules
  python3 scripts/audit_cli.py clues
  python3 scripts/audit_cli.py chain CLUE_ID
  python3 scripts/audit_cli.py public-verify 8115-M1-0001

所有写操作只追加、不覆盖；任何命令都可通过 --as 指定操作账号（默认稽核员甲）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.audit import cases, query, rules
from src.audit.db import AuditError, connect, init_db
from src.audit.ingest import SOURCE_TYPES, ingest_batch

DEFAULT_USER = "u_yibin_01"


def _db_path(args) -> Path:
    return Path(args.db or os.environ.get("AUDIT_DB", "audit.db"))


def _conn(args):
    conn = connect(_db_path(args))
    init_db(conn)  # 幂等
    return conn


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def cmd_init(args):
    conn = _conn(args)
    _print({"ok": True, "db": str(_db_path(args)),
            "users": [dict(r) for r in conn.execute(
                "SELECT user_id, display_name, role FROM users ORDER BY user_id")]})


def cmd_ingest(args):
    if args.file == "-":
        data = json.load(sys.stdin)
    else:
        data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = [data]
    conn = _conn(args)
    stats = ingest_batch(conn, args.source, args.org, data,
                         submitted_by=args.as_user, note=args.note)
    _print(stats)


def cmd_rules(args):
    conn = _conn(args)
    _print(rules.run_all(conn, repeat_window_min=args.window))


def cmd_clues(args):
    conn = _conn(args)
    _print(query.list_clues(conn, args.as_user, status=args.status,
                            clue_type=args.type))


def cmd_public_verify(args):
    conn = _conn(args)
    _print(query.public_verify(conn, args.code))


def cmd_dossier(args):
    conn = _conn(args)
    _print(query.medicine_dossier(conn, args.as_user, args.code))


def cmd_raw(args):
    conn = _conn(args)
    _print(query.original_payload(conn, args.as_user, args.record_id))


def cmd_case(args):
    conn = _conn(args)
    clue_ids = args.clues.split(",") if args.clues else None
    _print({"case_id": cases.create_case(conn, args.as_user, args.title, clue_ids)})


def cmd_decide(args):
    conn = _conn(args)
    token = cases.acquire_lock(conn, args.clue_id, args.as_user)
    did = cases.add_decision(conn, args.clue_id, args.as_user, args.action,
                             args.rationale, token, case_id=args.case_id)
    _print({"decision_id": did, "lock_token": token})


def cmd_seal(args):
    conn = _conn(args)
    _print(cases.seal_case(conn, args.case_id, args.as_user, args.note))


def cmd_handoff(args):
    conn = _conn(args)
    _print(cases.handoff(conn, args.case_id, args.as_user, args.to_org,
                         from_org=args.from_org))


def cmd_receive(args):
    conn = _conn(args)
    cases.receive_handoff(conn, args.handoff_id, args.receipt_org,
                          args.receiver, args.receipt_no, args.remark)
    _print({"ok": True, "handoff_id": args.handoff_id, "status": "received"})


def cmd_chain(args):
    conn = _conn(args)
    _print(cases.evidence_chain(conn, args.clue_id))


def cmd_integrity(args):
    conn = _conn(args)
    _print(cases.verify_integrity(conn, args.case_id))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="回流药跨省证据归并稽核后台")
    parser.add_argument("--db", help="SQLite 路径（默认取 $AUDIT_DB 或 ./audit.db）")
    parser.add_argument("--as", dest="as_user", default=DEFAULT_USER,
                        help="操作账号（默认 u_yibin_01）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)

    p = sub.add_parser("ingest", help="按原始来源入库一个 JSON 数组/对象文件")
    p.add_argument("--source", required=True, choices=SOURCE_TYPES)
    p.add_argument("--org", required=True, help="报送单位（原始来源标识）")
    p.add_argument("--file", required=True, help="JSON 文件，- 表示标准输入")
    p.add_argument("--note")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("rules", help="批量交叉比对，生成风险线索")
    p.add_argument("--window", type=int, default=180, help="重复开药阈值（分钟）")
    p.set_defaults(func=cmd_rules)

    p = sub.add_parser("clues")
    p.add_argument("--status")
    p.add_argument("--type", dest="type")
    p.set_defaults(func=cmd_clues)

    p = sub.add_parser("public-verify", help="公众核验单盒合法流转摘要")
    p.add_argument("code")
    p.set_defaults(func=cmd_public_verify)

    p = sub.add_parser("dossier", help="案件人员查看单盒完整档案")
    p.add_argument("code")
    p.set_defaults(func=cmd_dossier)

    p = sub.add_parser("raw", help="调取某条派生数据的原始报文")
    p.add_argument("record_id")
    p.set_defaults(func=cmd_raw)

    p = sub.add_parser("case", help="立案")
    p.add_argument("--title", required=True)
    p.add_argument("--clues", help="逗号分隔的 clue_id")
    p.set_defaults(func=cmd_case)

    p = sub.add_parser("decide", help="持锁并写入一次人工研判决定")
    p.add_argument("clue_id")
    p.add_argument("--action", required=True,
                   choices=["note", "escalate", "request_coop",
                            "dismiss", "confirm_for_transfer"])
    p.add_argument("--rationale", required=True)
    p.add_argument("--case-id", dest="case_id")
    p.set_defaults(func=cmd_decide)

    p = sub.add_parser("seal")
    p.add_argument("case_id")
    p.add_argument("--note")
    p.set_defaults(func=cmd_seal)

    p = sub.add_parser("handoff")
    p.add_argument("case_id")
    p.add_argument("--to-org", dest="to_org", required=True)
    p.add_argument("--from-org", dest="from_org", default="宜宾市医保经办稽核部门")
    p.set_defaults(func=cmd_handoff)

    p = sub.add_parser("receive", help="接收方追加回执")
    p.add_argument("handoff_id")
    p.add_argument("--receipt-org", dest="receipt_org", required=True)
    p.add_argument("--receiver", required=True)
    p.add_argument("--receipt-no", dest="receipt_no", required=True)
    p.add_argument("--remark")
    p.set_defaults(func=cmd_receive)

    p = sub.add_parser("chain", help="从线索还原证据链")
    p.add_argument("clue_id")
    p.set_defaults(func=cmd_chain)

    p = sub.add_parser("integrity")
    p.add_argument("case_id", nargs="?")
    p.set_defaults(func=cmd_integrity)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except AuditError as e:
        print(f"操作被拒绝：{e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
