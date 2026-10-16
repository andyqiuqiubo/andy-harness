"""自生成原生 EXE 样本（PE32+/x86-64）。

本机没有 MSVC/gcc/Go/Rust 等任何编译器，因此本脚本用标准库 **逐字节构造** 一个
合法、可运行、可被 Ghidra 正常反编译的 Windows 控制台程序，用于第二轮验证。

产物结构：
  .text   RVA 0x1000  —— 两个函数（main / transform），main 以 RIP 相对寻址引用字符串
  .rdata  RVA 0x2000  —— 若干可打印 ASCII 特征串（静态阶段发现 → 动态阶段内存复核）

设计要点：
- 不依赖导入表（NumberOfRvaAndSizes=16 全零），避免 IAT/重定位带来的额外失败点；
- 入口点返回 transform(0x2a) = (0x2a ^ 0x5a) + 0x111 = 385 作为进程退出码，可直接观测；
- 字符串同时作为「REA 静态发现 → Frida 运行时内存扫描」的数据流转载体。

用法：``python make_sample_exe.py [输出路径]``
"""

from __future__ import annotations

import struct
import sys
import time
from pathlib import Path

IMAGE_BASE = 0x140000000
FILE_ALIGN = 0x200
SECT_ALIGN = 0x1000

# ── 代码（手工编码的 x86-64 指令） ────────────────────────────────────
# main:
#   0x1000  48 83 EC 28            sub  rsp, 0x28
#   0x1004  48 8D 05 rel32         lea  rax, [rip+rel32]   -> .rdata 字符串
#   0x100B  B9 2A 00 00 00         mov  ecx, 0x2a
#   0x1010  E8  rel32              call transform (0x1030)
#   0x1015  48 83 C4 28            add  rsp, 0x28
#   0x1019  C3                     ret
MAIN = bytearray()
MAIN += bytes.fromhex("4883EC28")  # 0x1000
MAIN += bytes.fromhex("488D05")  # 0x1004 lea rax, [rip+d]
MAIN += struct.pack("<i", 0x2000 - 0x100B)  # rel32 -> 0x2000
MAIN += bytes.fromhex("B92A000000")  # 0x100B
MAIN += bytes.fromhex("E8")  # 0x1010
MAIN += struct.pack("<i", 0x1030 - 0x1015)
MAIN += bytes.fromhex("4883C428")  # 0x1015
MAIN += bytes.fromhex("C3")  # 0x1019
assert len(MAIN) == 0x1A, len(MAIN)

# transform(int x):  (x ^ 0x5a) + 0x111
#   0x1030  55                     push rbp
#   0x1031  48 89 E5               mov  rbp, rsp
#   0x1034  89 4D 10               mov  [rbp+0x10], ecx
#   0x1037  8B 45 10               mov  eax, [rbp+0x10]
#   0x103A  35 5A 00 00 00         xor  eax, 0x5a
#   0x103F  05 11 01 00 00         add  eax, 0x111
#   0x1044  5D                     pop  rbp
#   0x1045  C3                     ret
TRANSFORM = bytes.fromhex("554889E5" "894D10" "8B4510" "355A000000" "0511010000" "5D" "C3")
assert len(TRANSFORM) == 22, len(TRANSFORM)

STRINGS = [
    b"ANDY-HARNESS::ENGINEERING-REVERSE::SAMPLE-v1\x00",
    b"TRANSFORM-SEED=0x2A\x00",
    b"FRIDA-RUNTIME-PROBE\x00",
    b"EVIDENCE-V2-CHAIN\x00",
    b"SHA256-DIGEST-SAMPLE\x00",
    b"http://127.0.0.1:9/probe\x00",
]


def build() -> bytes:
    text = bytearray(0x200)
    text[0x000:0x01A] = MAIN
    text[0x030:0x030 + len(TRANSFORM)] = TRANSFORM

    rdata = bytearray(0x200)
    off = 0
    for s in STRINGS:
        rdata[off:off + len(s)] = s
        off += len(s)

    # ── DOS 头（64B，无 DOS stub，e_lfanew=0x40） ──
    dos = bytearray(0x40)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 0x40)

    # ── PE 签名 + COFF 头 ──
    pe = bytearray()
    pe += b"PE\x00\x00"
    pe += struct.pack(
        "<HHIIIHH",
        0x8664,  # Machine: AMD64
        2,  # NumberOfSections
        int(time.time()) & 0xFFFFFFFF,  # TimeDateStamp
        0,  # PointerToSymbolTable
        0,  # NumberOfSymbols
        240,  # SizeOfOptionalHeader (PE32+)
        0x0022,  # Characteristics: EXECUTABLE_IMAGE | LARGE_ADDRESS_AWARE
    )

    # ── 可选头（PE32+，240B） ──
    opt = bytearray()
    opt += struct.pack("<HBB", 0x20B, 14, 0)  # Magic, linker ver
    opt += struct.pack("<I", 0x200)  # SizeOfCode
    opt += struct.pack("<I", 0x200)  # SizeOfInitializedData
    opt += struct.pack("<I", 0)  # SizeOfUninitializedData
    opt += struct.pack("<I", 0x1000)  # AddressOfEntryPoint (RVA)
    opt += struct.pack("<I", 0x1000)  # BaseOfCode
    opt += struct.pack("<Q", IMAGE_BASE)
    opt += struct.pack("<II", SECT_ALIGN, FILE_ALIGN)
    opt += struct.pack("<HHHHHH", 6, 0, 0, 0, 6, 0)  # OS/Image/Subsystem versions
    opt += struct.pack("<I", 0)  # Win32VersionValue
    opt += struct.pack("<I", 0x3000)  # SizeOfImage
    opt += struct.pack("<I", FILE_ALIGN)  # SizeOfHeaders
    opt += struct.pack("<I", 0)  # CheckSum
    opt += struct.pack("<HH", 3, 0x0100)  # Subsystem=CONSOLE, NX_COMPAT
    opt += struct.pack("<QQQQ", 0x100000, 0x1000, 0x100000, 0x1000)
    opt += struct.pack("<I", 0)  # LoaderFlags
    opt += struct.pack("<I", 16)  # NumberOfRvaAndSizes
    opt += b"\x00" * (16 * 8)  # DataDirectory 全零（无导入/资源/重定位）
    assert len(opt) == 240, len(opt)

    # ── 节表 ──
    def section(name: bytes, vsize: int, rva: int, rawsize: int, rawoff: int, chars: int) -> bytes:
        return (
            struct.pack("<8s", name)
            + struct.pack("<IIIIIIHHI", vsize, rva, rawsize, rawoff, 0, 0, 0, 0, chars)
        )

    sections = b"".join(
        [
            section(b".text", 0x200, 0x1000, 0x200, 0x200, 0x60000020),
            section(b".rdata", 0x200, 0x2000, 0x200, 0x400, 0x40000040),
        ]
    )

    headers = bytes(dos) + bytes(pe) + bytes(opt) + sections
    pad = FILE_ALIGN - len(headers)
    if pad < 0:
        raise RuntimeError(f"头过长：{len(headers)}")
    out = bytearray(headers + b"\x00" * pad)
    out += text
    out += rdata
    return bytes(out)


def main() -> int:
    target = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path(__file__).resolve().parents[1] / "samples" / "generated" / "andy_reveng_sample.exe"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    data = build()
    target.write_bytes(data)
    print(f"已生成：{target}")
    print(f"  大小：{len(data)} 字节")
    print("  机器：AMD64 (PE32+)，子系统：CONSOLE，入口 RVA：0x1000")
    print(f"  预期退出码：{(0x2A ^ 0x5A) + 0x111}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
