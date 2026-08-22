from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from gh_pool.config import adopt

adopt()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GH_POOL_", extra="ignore")

    token: str = ""
    worker_token: str = "dev-worker"
    client_token: str = "dev-client"
    database_url: str = "postgresql:///pool"
    data_dir: Path = Path("./data")
    blob_dir: Path | None = None
    event_cap: int = 100 * 1024 * 1024
    lost_after: float = 300.0
    lease_wait: float = 30.0
    worker_stale: float = 120.0
    flush_every: float = 0.2
    shell_cap: int = 1 << 20
    shell_idle: float = 300.0
    shell_poll: float = 10.0
    storage: Path = Path("/var/lib/gh-chrome")
    host: str = "127.0.0.1"
    port: int = 8000
    relay_host: str = "127.0.0.1"
    relay_port: int = 8001
    public_url: str = "http://127.0.0.1:8000"
    pool_server: str = ""
    pool_token: str = ""
    runner_spec: str = ""
    runner_python: str = "3.14"
    runner_timeout: float = 21600.0
    runner_workdir: Path = Path("/tmp/gh-chrome")
    runner_grace: float = 300.0
    heartbeat_timeout: float = 30.0
    ready_timeout: float = 600.0
    watchdog_interval: float = 5.0
    cleanup_interval: float = 3600.0
    cleanup_delay: float = 60.0
    cleanup_max_days: float = 7.0
    cleanup_max_bytes: int = 64 << 30
    segment_seconds: float = 1.0
    max_upload: int = 1 << 30

    @property
    def blobs_dir(self) -> Path:
        return self.blob_dir or self.data_dir / "blobs"

    @property
    def sessions_dir(self) -> Path:
        return self.storage / "sessions"

    @property
    def profiles_dir(self) -> Path:
        return self.storage / "profiles"

    @property
    def files_dir(self) -> Path:
        return self.storage / "files"


settings = Settings()
