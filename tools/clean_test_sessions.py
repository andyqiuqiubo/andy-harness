"""清理测试污染数据：仅删除测试产生的会话（附件测试 / 空 other）。

安全约束：
- 只匹配 title='附件测试'，或 title='other' 且消息数为 0；
- 删除前逐条打印 id / title / 创建时间 / 消息数（审计留痕）；
- 真实会话（有实际对话内容）不在匹配范围内，不会被删除。
"""

import json
import sqlite3
import sys
import urllib.request

DB = "data/harness.db"
BASE = "http://127.0.0.1:8020"


def main() -> int:
    con = sqlite3.connect(DB)
    rows = con.execute(
        """
        SELECT s.id, s.title, s.created_at,
               (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS n
        FROM sessions s
        WHERE s.title = '附件测试'
           OR (s.title = 'other'
               AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.id))
        ORDER BY s.created_at
        """
    ).fetchall()
    # 二次确认 other 必须 0 消息
    targets = []
    for sid, title, created, n in rows:
        if title == "other" and n > 0:
            continue
        targets.append((sid, title, created, n))
    con.close()

    print(f"匹配到 {len(targets)} 个测试会话，逐条审计：")
    for sid, title, created, n in targets:
        print(f"  DELETE {sid} | title={title!r} | created={created[:19]} | messages={n}")
    if len(sys.argv) > 1 and sys.argv[1] == "--dry-run":
        print("dry-run：不执行删除")
        return 0

    ok = fail = 0
    for sid, _t, _c, _n in targets:
        req = urllib.request.Request(f"{BASE}/api/sessions/{sid}", method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status in (200, 204):
                    ok += 1
                else:
                    fail += 1
                    print(f"  ✗ {sid} HTTP {r.status}")
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"  ✗ {sid} {e}")

    print(f"\n删除完成：成功 {ok}，失败 {fail}")

    con = sqlite3.connect(DB)
    print("删除后剩余：sessions =", con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
          "| messages =", con.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
          "| 附件测试 =", con.execute(
              "SELECT COUNT(*) FROM sessions WHERE title='附件测试'").fetchone()[0])
    con.close()
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
