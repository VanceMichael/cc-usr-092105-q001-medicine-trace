"""稽核后台启动入口。

用法：
    python -m src.server --db data/audit.db --seed --port 8080
生产部署应由进程管理器托管，并在前置网关注入 X-User-Id / X-Role。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .api import create_app
from .seed import seed_base, seed_late
from .services import AuditService
from .store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="医保回流药稽核后台")
    parser.add_argument("--db", default="data/audit.db",
                        help="SQLite 路径（默认 data/audit.db）")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--seed", action="store_true",
                        help="空库时灌入演示场景数据")
    parser.add_argument("--with-late", action="store_true",
                        help="与 --seed 同时灌入跨省迟到数据")
    args = parser.parse_args(argv)

    path = Path(args.db)
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists() or path.stat().st_size == 0
    store = Store(path)
    if args.seed and fresh:
        seed_base(store)
        if args.with_late:
            seed_late(store)
        AuditService(store).run_batch(note="启动初始化批量比对")
        print(f"[启动] 已灌入演示数据：{args.db}", file=sys.stderr)

    server = create_app(store, port=args.port)
    print(f"[启动] 稽核后台监听 http://127.0.0.1:{args.port}", file=sys.stderr)
    print("[启动] 身份头由政务网关注入：X-User-Id / X-Role；"
          "公众接口 /api/public/* 免认证", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
