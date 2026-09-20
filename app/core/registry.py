"""
实现注册表 —— 让四层能真正并行开发的关键。

每个能力定义成一个 Protocol，实现注册到表里，靠 .env 切换。
组员接自己的实现只需新建文件加一个装饰器，不改任何现有代码。

    # app/core/providers/zhipu_vlm.py
    from app.core.registry import register

    @register("vlm", "zhipu")
    class ZhipuVLM:
        async def describe(self, image: bytes, prompt: str) -> dict: ...

切 .env:  VLM_PROVIDER=zhipu
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any, Callable

_IMPLS: dict[str, dict[str, type]] = {}

# 会被自动扫描的包 —— 里面的 @register 装饰器在 import 时执行
_SCAN_PACKAGES = (
    "app.core.providers",
    "app.core.detectors",
    "app.core.layers",
    "app.core.sources",
)


def register(kind: str, name: str) -> Callable[[type], type]:
    """把实现登记到 `kind` 类别下的 `name` 名下。"""

    def deco(cls: type) -> type:
        bucket = _IMPLS.setdefault(kind, {})
        if name in bucket:
            raise ValueError(f"实现名冲突: {kind}/{name} 已被 {bucket[name].__name__} 占用")
        bucket[name] = cls
        return cls

    return deco


def get(kind: str, name: str, **kwargs: Any):
    """取一个实现实例。"""
    try:
        cls = _IMPLS[kind][name]
    except KeyError:
        available = ", ".join(sorted(_IMPLS.get(kind, {}))) or "(无)"
        raise KeyError(f"找不到实现 {kind}/{name}；已注册的有: {available}") from None
    return cls(**kwargs)


def names(kind: str) -> list[str]:
    return sorted(_IMPLS.get(kind, {}))


def load_all() -> None:
    """import 所有实现模块，触发 @register 装饰器。进程启动时调一次。"""
    for pkg_name in _SCAN_PACKAGES:
        pkg = importlib.import_module(pkg_name)
        for mod in pkgutil.iter_modules(pkg.__path__):
            importlib.import_module(f"{pkg_name}.{mod.name}")
