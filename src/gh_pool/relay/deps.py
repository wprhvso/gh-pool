from typing import Annotated

from fastapi import Depends

from gh_pool.core.deps import from_state
from gh_pool.relay.tunnel import Tunnels

Tn = Annotated[Tunnels, Depends(from_state("tunnels"))]
