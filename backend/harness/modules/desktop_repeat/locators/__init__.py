"""L4 定位器：注册表 + 四类实现（§6.1/§8.2 E1）。"""

from . import coord_locator, image_locator, relative_locator, uia_locator  # 注册触发  # noqa: F401
from .registry import (
    ExecContext,
    Locator,
    build_chain,
    register_locator,
    resolve_chain,
)

__all__ = ["ExecContext", "Locator", "register_locator", "build_chain", "resolve_chain"]
