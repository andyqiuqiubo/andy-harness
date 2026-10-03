"""生成桌面壳图标（一次性脚本）：品牌 PNG / ICO。

Tauri 打包需要的 .icns 用 `tauri icon` 由 CLI 生成（见 desktop/README.md）。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve()
# 深靛蓝品牌色（与前端主色一致）
BG = (79, 70, 229)
FG = (255, 255, 255)


def _font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in (
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_icon(size: int) -> Image.Image:
    """圆角方块 + 白色 AH 字样。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    radius = int(size * 0.22)
    draw.rounded_rectangle(
        [(0, 0), (size - 1, size - 1)], radius=radius, fill=BG
    )
    font = _font(int(size * 0.42))
    text = "AH"
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    x = (size - w) / 2 - bbox[0]
    y = (size - h) / 2 - bbox[1]
    draw.text((x, y), text, font=font, fill=FG)
    return img


def main() -> None:
    out = HERE.parent
    # 1024 主源图
    make_icon(1024).save(out / "icon.png")
    make_icon(32).save(out / "32x32.png")
    make_icon(128).save(out / "128x128.png")
    make_icon(256).save(out / "128x128@2x.png")
    # 多尺寸 ICO
    ico = make_icon(256)
    ico.save(
        out / "icon.ico",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print("icons generated in", out)


if __name__ == "__main__":
    main()
