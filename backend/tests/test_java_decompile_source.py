"""Java 反编译：源码级反编译器（CFR / FernFlower / Jadx）集成与逻辑测试。

用项目内隔离 JDK 现场编译一个「桩反编译器」jar，模拟真实反编译器的命令行约定，
验证 JavaDecompileStage 在配置 ``ANDY_JAVA_DECOMPILER_JAR`` 后会优先输出真正的
``.java`` 源码；同时覆盖反编译器类型识别、命令行构造、FernFlower 的 jar 解压等纯逻辑。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import zipfile
from pathlib import Path

from harness.modules.reverse_engineering.models import STAGE_JAVA_DECOMPILE
from harness.modules.reverse_engineering.pipeline import ReverseEngineeringPipeline
from harness.modules.reverse_engineering.stages import JavaDecompileStage

SRC_ROOT = Path(__file__).resolve().parents[2]  # .../src
TOOLS_DIR = Path(os.environ.get("ANDY_REVENG_TOOLS_DIR") or (SRC_ROOT.parent / ".tools"))
JDK_DIR = TOOLS_DIR / "jdk-21"
JAVAC = JDK_DIR / "bin" / "javac.exe"
JAR = JDK_DIR / "bin" / "jar.exe"
SAMPLE_DIR = SRC_ROOT / "rev_eng" / "samples" / "java"


def _sample_class() -> Path:
    """确保 Hello.class 存在（不存在则用项目 JDK 现场编译）。"""
    src = SAMPLE_DIR / "Hello.java"
    cls = SAMPLE_DIR / "Hello.class"
    if not cls.is_file() and src.is_file():
        subprocess.run([str(JAVAC), str(src)], cwd=str(SAMPLE_DIR), check=True, capture_output=True)
    return cls


# 桩反编译器：模拟 CFR 的命令行约定（<file> --outputdir <dir> [--silent true]）
STUB_CFR_SRC = """\
import java.io.*;
import java.nio.file.*;
public class Stub {
  public static void main(String[] args) throws Exception {
    String outDir = ".";
    String input = args[0];
    for (int i = 0; i < args.length; i++) {
      if ("--outputdir".equals(args[i]) && i + 1 < args.length) outDir = args[++i];
    }
    Path dir = Paths.get(outDir);
    Files.createDirectories(dir);
    String fn = Paths.get(input).getFileName().toString();
    int dot = fn.lastIndexOf('.');
    String name = dot > 0 ? fn.substring(0, dot) : fn;
    Path out = dir.resolve(name + ".java");
    Files.writeString(out, "public class " + name + " {\\n    public void hello() {}\\n}\\n");
    System.out.println("decompiled " + name);
  }
}
"""

# 桩反编译器：模拟 FernFlower 的命令行约定（<file> <dest-dir>），结果打包成 <name>.jar
STUB_FERN_SRC = """\
import java.io.*;
import java.nio.file.*;
import java.util.zip.*;
public class Stub {
  public static void main(String[] args) throws Exception {
    String input = args[0];
    String outDir = args[1];
    Path dir = Paths.get(outDir);
    Files.createDirectories(dir);
    String fn = Paths.get(input).getFileName().toString();
    int dot = fn.lastIndexOf('.');
    String name = dot > 0 ? fn.substring(0, dot) : fn;
    Path jar = dir.resolve(name + ".jar");
    try (ZipOutputStream zos = new ZipOutputStream(Files.newOutputStream(jar))) {
      ZipEntry e = new ZipEntry(name + ".java");
      zos.putNextEntry(e);
      zos.write(("public class " + name + " {\\n}\\n").getBytes("UTF-8"));
      zos.closeEntry();
    }
    System.out.println("fernflower done");
  }
}
"""


def _build_stub(tmp_path: Path, jar_name: str, src: str) -> Path:
    """用项目 JDK 现场编译一个桩反编译器 jar。"""
    jdir = tmp_path / "stubbuild"
    jdir.mkdir(parents=True, exist_ok=True)
    (jdir / "Stub.java").write_text(src, encoding="utf-8")
    subprocess.run([str(JAVAC), "Stub.java"], cwd=str(jdir), check=True, capture_output=True)
    jar = tmp_path / jar_name
    subprocess.run(
        [str(JAR), "cfe", str(jar), "Stub", "-C", str(jdir), "Stub.class"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    return jar


# ── 纯逻辑：反编译器类型识别 ─────────────────────────────────────────────
def test_detect_decompiler_by_filename():
    cases = {
        "cfr-0.152.jar": "cfr",
        "CFR.jar": "cfr",
        "fernflower.jar": "fernflower",
        "FernThing.jar": "fernflower",
        "jadx-cli-1.4.7.jar": "jadx",
        "my-decompiler.jar": "generic",
    }
    for name, expected in cases.items():
        got = JavaDecompileStage._detect_decompiler(Path(name))
        assert got == expected, f"{name}: 期望 {expected}，实际 {got}"


# ── 纯逻辑：按类型构造命令行 ─────────────────────────────────────────────
def test_build_command_per_decompiler():
    java = JDK_DIR / "bin" / "java.exe"
    subject = Path("X.class")
    out = Path("out")
    specs = [("cfr.jar", "cfr"), ("fernflower.jar", "fernflower"), ("jadx.jar", "jadx"), ("other.jar", "generic")]
    for jar_name, kind in specs:
        cmd = JavaDecompileStage()._build_command(kind, Path(jar_name), subject, out)
        assert cmd[0] == str(java), cmd
        assert "-jar" in cmd
        if kind == "cfr":
            assert "--outputdir" in cmd and cmd[cmd.index("--outputdir") + 1] == str(out)
        elif kind == "jadx":
            assert "-d" in cmd and cmd[cmd.index("-d") + 1] == str(out)
        elif kind in ("fernflower", "generic"):
            # 第二、三位置参为 <input> <output-dir>
            assert cmd[-2] == str(subject) and cmd[-1] == str(out)
        else:  # pragma: no cover
            raise AssertionError(kind)


# ── 纯逻辑：FernFlower jar 解压 ──────────────────────────────────────────
def test_extract_jars(tmp_path: Path):
    d = tmp_path / "out"
    d.mkdir()
    jar = d / "A.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("A.java", "public class A {}")
    JavaDecompileStage._extract_jars(d)
    assert (d / "A.java").is_file()


# ── 端到端：配置 CFR 桩 → 输出真实 .java 源码 ───────────────────────────
def test_cfr_produces_real_java_source(tmp_path: Path):
    sample = _sample_class()
    jar = _build_stub(tmp_path, "cfr-stub.jar", STUB_CFR_SRC)
    assert JavaDecompileStage._detect_decompiler(jar) == "cfr"
    os.environ["ANDY_JAVA_DECOMPILER_JAR"] = str(jar)
    try:
        pipe = ReverseEngineeringPipeline(runs_dir=tmp_path / "runs")
        rec = asyncio.run(pipe.run(sample, {"stages": [STAGE_JAVA_DECOMPILE]}))
    finally:
        os.environ.pop("ANDY_JAVA_DECOMPILER_JAR", None)

    st = rec.stages[STAGE_JAVA_DECOMPILE]
    assert st.status.value == "ok", st.summary
    sources = [a for a in st.artifacts if a.kind == "java_source"]
    assert sources, "应产出 java_source 产物"
    assert not any(a.kind == "java_decompile" for a in st.artifacts), "源码级反编译成功不应再落 javap 文本"
    content = (Path(rec.run_dir) / "artifacts" / sources[0].name).read_text(encoding="utf-8")
    assert "public class Hello" in content, content
    assert st.facts["decompiler"] == "cfr"
    assert st.facts["java_source_count"] == 1


# ── 端到端：配置 FernFlower 桩（jar-in-jar）→ 解压后输出 .java 源码 ───────
def test_fernflower_produces_real_java_source(tmp_path: Path):
    sample = _sample_class()
    jar = _build_stub(tmp_path, "fernflower-stub.jar", STUB_FERN_SRC)
    assert JavaDecompileStage._detect_decompiler(jar) == "fernflower"
    os.environ["ANDY_JAVA_DECOMPILER_JAR"] = str(jar)
    try:
        pipe = ReverseEngineeringPipeline(runs_dir=tmp_path / "runs")
        rec = asyncio.run(pipe.run(sample, {"stages": [STAGE_JAVA_DECOMPILE]}))
    finally:
        os.environ.pop("ANDY_JAVA_DECOMPILER_JAR", None)

    st = rec.stages[STAGE_JAVA_DECOMPILE]
    assert st.status.value == "ok", st.summary
    sources = [a for a in st.artifacts if a.kind == "java_source"]
    assert sources, "FernFlower 解压后应产出 java_source"
    content = (Path(rec.run_dir) / "artifacts" / sources[0].name).read_text(encoding="utf-8")
    assert "public class Hello" in content, content


# ── 端到端：未配置反编译器 → 回退 javap 结构文本（java_decompile） ────────
def test_no_decompiler_falls_back_to_javap(tmp_path: Path):
    sample = _sample_class()
    os.environ.pop("ANDY_JAVA_DECOMPILER_JAR", None)
    pipe = ReverseEngineeringPipeline(runs_dir=tmp_path / "runs")
    rec = asyncio.run(pipe.run(sample, {"stages": [STAGE_JAVA_DECOMPILE]}))
    st = rec.stages[STAGE_JAVA_DECOMPILE]
    assert st.status.value == "ok", st.summary
    texts = [a for a in st.artifacts if a.kind == "java_decompile"]
    assert texts, "未配置反编译器应落 javap 结构文本"
    assert st.facts["decompiler"] == "javap"
    assert st.facts.get("degraded") is False
