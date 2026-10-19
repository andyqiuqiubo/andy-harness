"""原子文件写入工具测试（SEC-24）。

验证 ``atomic_write_text`` / ``atomic_write_json`` 的原子性契约：
- 覆盖已存在文件时，内容完整替换而非截断；
- 写入失败（如目标目录被占为文件）时不产生残留临时文件；
- JSON 序列化带尾换行、支持 ``default`` 处理特殊类型。
"""

import json
import os
from pathlib import Path

import pytest

from harness.infra.atomic_write import atomic_write_json, atomic_write_text


class TestAtomicWriteText:
    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        """父目录不存在时自动创建。"""
        target = tmp_path / "a" / "b" / "c.txt"
        atomic_write_text(target, "内容")
        assert target.read_text(encoding="utf-8") == "内容"

    def test_overwrite_is_complete(self, tmp_path: Path) -> None:
        """覆盖已存在文件时完整替换（不会残留旧内容尾部）。"""
        target = tmp_path / "f.txt"
        atomic_write_text(target, "x" * 1000)
        atomic_write_text(target, "short")
        assert target.read_text(encoding="utf-8") == "short"

    def test_no_tmp_leftover_on_success(self, tmp_path: Path) -> None:
        """成功后目录里不残留 .tmp 临时文件。"""
        target = tmp_path / "f.json"
        atomic_write_json(target, {"k": "v"})
        leftovers = [p.name for p in tmp_path.iterdir() if ".tmp" in p.name]
        assert leftovers == []


class TestAtomicWriteJson:
    def test_json_roundtrip(self, tmp_path: Path) -> None:
        """写入的 JSON 可被完整解析回原结构。"""
        target = tmp_path / "state.json"
        data = {"mode": "auto", "overrides": {"code_runner": "auto", "x": "y"}}
        atomic_write_json(target, data)
        assert json.loads(target.read_text(encoding="utf-8")) == data

    def test_trailing_newline_and_unicode(self, tmp_path: Path) -> None:
        """默认 ensure_ascii=False 保留中文，且文件以换行结尾。"""
        target = tmp_path / "u.json"
        atomic_write_json(target, {"msg": "已停用：中文"})
        text = target.read_text(encoding="utf-8")
        assert text.endswith("\n")
        assert "中文" in text

    def test_default_handles_non_serializable(self, tmp_path: Path) -> None:
        """通过 default 回调序列化 set 等特殊类型。"""
        target = tmp_path / "s.json"
        atomic_write_json(target, {"disabled": {"a", "b"}}, default=list)
        got = json.loads(target.read_text(encoding="utf-8"))["disabled"]
        assert sorted(got) == ["a", "b"]

    def test_chmod_applied(self, tmp_path: Path) -> None:
        """传入 chmod 时目标权限被收敛（POSIX）。"""
        if os.name == "nt":  # Windows 对 POSIX 位支持有限，跳过
            pytest.skip("POSIX chmod 语义在 Windows 不完整")
        target = tmp_path / "secret.json"
        atomic_write_json(target, {"k": "v"}, chmod=0o600)
        mode = target.stat().st_mode & 0o777
        assert mode == 0o600


class TestAtomicWriteFailure:
    def test_failure_leaves_no_tmp(self, tmp_path: Path) -> None:
        """写入中途失败时清理半成品临时文件，且原文件不受破坏。"""
        # 让 target 变成一个"目录名占用"：原子写先写 tmp 再 replace 到该路径，
        # replace 到目录会抛错 → 触发异常清理分支。
        target = tmp_path / "occupied"
        target.mkdir()  # 占用为目录，os.replace(tmp, dir) 会失败
        original_tmp_count = len([p for p in tmp_path.iterdir() if ".tmp" in p.name])
        with pytest.raises(OSError):
            atomic_write_text(target, "data")
        after_tmp = [p.name for p in tmp_path.iterdir() if ".tmp" in p.name]
        assert len(after_tmp) == original_tmp_count, f"失败后残留临时文件: {after_tmp}"


class TestTransientLockRetry:
    def test_retries_permission_error(self, tmp_path: Path, monkeypatch) -> None:
        """WinError 5（目标被短暂占用）必须重试，而不是直接失败。

        真机事故：录制器收尾写 session.json 时 os.replace 被心跳线程的读句柄
        顶掉，封版中断，会话停在 failed，用户到"解析"才发现"未封版"。
        """
        from harness.infra import atomic_write as aw

        calls = {"n": 0}
        real_replace = aw.os.replace

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError(13, "The process cannot access the file", str(dst))
            return real_replace(src, dst)

        monkeypatch.setattr(aw.os, "replace", flaky)
        target = tmp_path / "session.json"
        atomic_write_json(target, {"status": "ready"})

        assert target.exists()
        assert calls["n"] == 3, f"未按预期重试（实际 {calls['n']} 次）"
        assert not [p for p in tmp_path.iterdir() if ".tmp" in p.name], "临时文件未清理"
