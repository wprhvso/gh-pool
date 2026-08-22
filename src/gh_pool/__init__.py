from gh_pool.config import adopt

adopt()

from gh_pool.rpc import emit  # noqa: E402

__all__ = ["emit"]
