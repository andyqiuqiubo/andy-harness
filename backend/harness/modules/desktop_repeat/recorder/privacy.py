"""隐私前置屏蔽（§3.8）：在采集层拦截，数据从未产生——不是"记了再删"。"""

from __future__ import annotations

import re

from ..models import UiaSnapshot, WindowRef

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_text(s: str) -> str:
    return _CONTROL_RE.sub("", str(s or ""))


class PrivacyGate:
    def __init__(self, mask_windows: list[str], mask_keys: list[str],
                 auto_pause_mask: list[str], capture_clipboard: bool) -> None:
        self._win_res = _compile(mask_windows)
        self._key_specs = [tuple(p.strip() for p in str(k).lower().split("+") if p.strip())
                           for k in mask_keys if str(k).strip()]
        self._auto_res = _compile(auto_pause_mask)
        self.capture_clipboard = bool(capture_clipboard)

    def mask_window(self, win: WindowRef | None) -> bool:
        if win is None:
            return False
        hay = (f"{win.title}|{win.exe}")
        return any(rx.search(hay) for rx in self._win_res)

    def auto_pause(self, win: WindowRef | None) -> bool:
        if win is None or not self._auto_res:
            return False
        hay = f"{win.title}|{win.exe}"
        return any(rx.search(hay) for rx in self._auto_res)

    def masked_keys(self, modifiers: list[str], key: str) -> bool:
        """本组合键是否命中 mask_keys（命中 → 该键与后续文本不入库）。"""
        parts = {str(m).lower() for m in modifiers} | {str(key).lower()}
        for spec in self._key_specs:
            if set(spec) <= parts:
                return True
        return False

    def mask_event(self, win: WindowRef | None, uia: UiaSnapshot | None) -> bool:
        """事件级：屏蔽窗口内事件只留 flags.masked；密码控件只留长度占位。"""
        return self.mask_window(win)

    def uia_allowed(self, win: WindowRef | None, uia: UiaSnapshot | None) -> bool:
        if self.mask_window(win):
            return False
        if uia is not None and uia.is_password:
            return False
        return True


def _compile(patterns: list[str]) -> list[re.Pattern[str]]:
    out: list[re.Pattern[str]] = []
    for p in patterns or []:
        s = str(p)
        if not s.strip():
            continue
        try:
            out.append(re.compile(s, re.IGNORECASE))
        except re.error:
            # 非法正则退化为字面包含匹配
            out.append(re.compile(re.escape(s), re.IGNORECASE))
    return out
