from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

Codes = dict[type[Exception], int]


def _reply(code: int) -> Callable[[Request, Exception], Response]:
    def handler(_: Request, exc: Exception) -> Response:
        return JSONResponse({"detail": str(exc)}, status_code=code)

    return handler


def install(app: FastAPI, codes: Codes) -> None:
    for error, code in codes.items():
        app.add_exception_handler(error, _reply(code))
