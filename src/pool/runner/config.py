import shutil
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _first_binary(*names: str) -> str:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return names[0]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GH_CHROME_", extra="ignore")

    url: str = "http://127.0.0.1:8000"
    token: str = ""
    display: int = 99
    workdir: Path = Path("/tmp/gh-chrome")
    chrome_binary: str = ""
    ffmpeg_binary: str = "ffmpeg"
    kasmvnc_binary: str = "Xkasmvnc"
    heartbeat_interval: float = 10.0
    debug_port: int = 9222
    proxy: str = ""
    vnc: bool = True
    vnc_port: int = 6667
    upload_allow_private: bool = False
    vnc_frame_rate: int = 30
    vnc_www: Path = Path("/usr/share/kasmvnc/www")

    @property
    def chrome(self) -> str:
        if self.chrome_binary:
            return self.chrome_binary
        return _first_binary(
            "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"
        )

    @property
    def display_name(self) -> str:
        return f":{self.display}"

    @property
    def profile_dir(self) -> Path:
        return self.workdir / "profile"

    @property
    def downloads_dir(self) -> Path:
        return self.workdir / "downloads"

    @property
    def segments_dir(self) -> Path:
        return self.workdir / "segments"

    @property
    def uploads_dir(self) -> Path:
        return self.workdir / "uploads"

    @property
    def logs_dir(self) -> Path:
        return self.workdir / "logs"


settings = Settings()
