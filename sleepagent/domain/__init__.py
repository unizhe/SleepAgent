# 当前领域包只重导出稳定合同，避免加载数据库或兼容运行时。
from sleepagent.domain.contracts import *  # noqa: F403
from sleepagent.domain.contracts import __all__
