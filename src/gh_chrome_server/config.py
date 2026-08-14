from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GH_CHROME_", extra="ignore")

    token: str = ""
    database_url: str = "postgresql:///gh_chrome_client"
    storage: Path = Path("/var/lib/gh-chrome")
    host: str = "127.0.0.1"
    port: int = 8000
    public_url: str = "http://127.0.0.1:8000"
    github_repo: str = ""
    github_workflow: str = "chrome.yml"
    github_ref: str = "main"
    github_pat: str = ""
    heartbeat_timeout: float = 30.0
    ready_timeout: float = 600.0
    watchdog_interval: float = 5.0
    segment_seconds: float = 1.0
    max_upload: int = 1 << 30

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
