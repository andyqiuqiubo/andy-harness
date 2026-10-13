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


# ── 应用主库单例（重复 Database() 不得产生第二个库） ──────────────────
def test_database_default_is_singleton(monkeypatch) -> None:
    """``Database()`` 必须是进程单例：REST 层多处直接取库，每建一次就跑一遍全量 DDL。

    更关键的是：生命周期被重复触发时（TestClient 反复进出），若每次都造新实例，
    "不存在才注册"的服务会与其余服务绑定不同库，跨库写入触发外键失败。
    """
    monkeypatch.setenv("HARNESS_DB_PATH", "")
    from harness.infra import database as dbmod

    original = dbmod._DB_SINGLETON
    dbmod._DB_SINGLETON = None  # 隔离：本用例自建一个临时单例
    try:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            monkeypatch.setenv("HARNESS_DB_PATH", str(__import__("pathlib").Path(td) / "s.db"))
            a = Database()
            b = Database()
            assert a is b
            # 第二次构造不得重置连接（否则会打断进行中的查询）
            a.query("SELECT 1")
            assert b._conn is a._conn
            a.close()
    finally:
        dbmod._DB_SINGLETON = original


def test_database_explicit_path_is_independent(tmp_path) -> None:
    """显式传路径必须得到独立实例（测试用 :memory: / 临时文件隔离不受单例影响）。"""
    d1 = Database(str(tmp_path / "a.db"))
    d2 = Database(str(tmp_path / "b.db"))
    assert d1 is not d2
    assert d1.db_path != d2.db_path
    d1.close()
    d2.close()


def test_memory_database_is_independent() -> None:
    """:memory: 每次都是独立实例，避免单例把测试间的内存库串在一起。"""
    m1 = Database(":memory:")
    m2 = Database(":memory:")
    assert m1 is not m2
    m1.close()
    m2.close()
