"""
大小姐管家模式插件（事件溯源架构重写版）

实现主对话模型与执行代理的角色分离。
纯 harness 层（harness/ 包）不依赖 astrbot 运行时，可独立测试；
main 的导入放在 try 里，测试环境缺 astrbot/quart 时不阻塞子模块导入。
"""

__version__ = "2.0.5"

try:  # pragma: no cover - AstrBot 运行时才可用
    from .main import MaidAgent  # noqa: F401

    __all__ = ["MaidAgent", "__version__"]
except ImportError:
    __all__ = ["__version__"]
