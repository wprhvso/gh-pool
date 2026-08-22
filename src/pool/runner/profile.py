import asyncio
import logging
import shutil

from pool.runner.config import settings
from pool.runner.http import ServerClient

log = logging.getLogger(__name__)

ARCHIVE = "profile.tar.zst"
EXCLUDES = (
    "--exclude=./*/Cache",
    "--exclude=./*/Code Cache",
    "--exclude=./*/GPUCache",
    "--exclude=./*/Service Worker/CacheStorage",
    "--exclude=./ShaderCache",
    "--exclude=./GrShaderCache",
    "--exclude=./component_crx_cache",
    "--exclude=./SingletonLock",
    "--exclude=./SingletonSocket",
    "--exclude=./SingletonCookie",
)


async def restore(server: ServerClient) -> bool:
    archive = settings.workdir / ARCHIVE
    if not await server.get_profile(archive):
        log.info("no profile archive to restore")
        return False
    target = settings.profile_dir
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    await _run("tar", "-I", "zstd -d", "-xf", str(archive), "-C", str(target))
    archive.unlink(missing_ok=True)
    log.info("profile restored")
    return True


async def store(server: ServerClient) -> None:
    archive = settings.workdir / ARCHIVE
    archive.unlink(missing_ok=True)
    await _run(
        "tar",
        "-I",
        "zstd -19 -T4",
        "-cf",
        str(archive),
        "-C",
        str(settings.profile_dir),
        *EXCLUDES,
        ".",
    )
    size = archive.stat().st_size
    await server.put_file("profile", archive)
    log.info("profile stored, %.1f MiB", size / (1 << 20))


async def _run(*command: str) -> None:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"{command[0]} failed: {stderr.decode()[:300]}")
