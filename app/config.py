from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    upstream_simple_url: str = "https://pypi.org/simple"
    upstream_files_url: str = "https://files.pythonhosted.org"
    redis_url: str = "redis://redis:6379/0"
    cache_ttl_seconds: int = 300
    cache_404_ttl: int = 60
    http_timeout: float = 10.0
    enable_background_metadata: bool = False  # HEAD .metadata for 658/714 (off by default, lazy)

settings = Settings()
