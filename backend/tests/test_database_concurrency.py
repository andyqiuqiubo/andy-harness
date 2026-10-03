"""数据库并发与 WAL 健壮性测试（对应 GAP G3）。"""

import threading

from harness.infra.database import Database


def test_wal_and_busy_timeout(tmp_path) -> None:
    """连接应开启 WAL 日志模式与 busy_timeout。"""
    db = Database(str(tmp_path / "wal.db"))
    mode = db.query_one("PRAGMA journal_mode")
    assert mode["journal_mode"] == "wal"
    bt = db.query_one("PRAGMA busy_timeout")
    assert bt["timeout"] == 5000
    db.close()


def test_concurrent_writes_no_corruption(tmp_path) -> None:
    """多线程并发写入不损坏数据库、不抛错，数据一致。"""
    db = Database(str(tmp_path / "concurrent.db"))
    db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v INTEGER NOT NULL DEFAULT 0)")

    errors: list[BaseException] = []

    def worker(key: str) -> None:
        try:
            for _ in range(50):
                db.execute(
                    "INSERT INTO kv (k, v) VALUES (?, 1) ON CONFLICT(k) DO UPDATE SET v = v + 1",
                    (key,),
                )
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(f"k{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发写入出现异常: {errors[:3]}"
    count = db.query_one("SELECT COUNT(*) AS c FROM kv")
    assert count["c"] == 8
    db.close()
