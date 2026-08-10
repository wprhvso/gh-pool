import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)

from gh_chrome_server.config import settings

REALM = "gh-chrome"
BASIC_USER = "admin"

_bearer = HTTPBearer(auto_error=True)
_basic = HTTPBasic(auto_error=True, realm=REALM)


async def require_token(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
) -> None:
    if not secrets.compare_digest(credentials.credentials, settings.token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid token")


async def require_basic(
    credentials: Annotated[HTTPBasicCredentials, Depends(_basic)],
) -> None:
    user_ok = secrets.compare_digest(credentials.username, BASIC_USER)
    token_ok = secrets.compare_digest(credentials.password, settings.token)
    if not (user_ok and token_ok):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid credentials",
            headers={"WWW-Authenticate": f'Basic realm="{REALM}"'},
        )


Token = Annotated[None, Depends(require_token)]
Basic = Annotated[None, Depends(require_basic)]
