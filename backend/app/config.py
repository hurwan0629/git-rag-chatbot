"""One root .env for Docker, the API, and CLI commands."""
import os
from pathlib import Path
from sqlalchemy import URL
from .services.config import load_env

ROOT = Path(__file__).resolve().parents[2]


class Settings:
    def __init__(self, *, device=None, search_only=None, database_required=True):
        self.github_token = load_env(ROOT / '.env') if (ROOT / '.env').is_file() else load_env()
        self.device = device or os.getenv('MODEL_DEVICE', 'auto')
        if self.device not in ('auto', 'cpu', 'cuda'):
            raise ValueError('MODEL_DEVICE must be auto, cpu, or cuda')
        self.search_only = (os.getenv('SEARCH_ONLY', 'false').lower() == 'true'
                            if search_only is None else search_only)
        password = os.getenv('POSTGRES_PASSWORD', '')
        if database_required and not password:
            raise ValueError('Fill POSTGRES_PASSWORD in the root .env file')
        self.database_url = URL.create('postgresql+psycopg',
            username=os.getenv('POSTGRES_USER', 'git_rag'), password=password,
            host=os.getenv('POSTGRES_HOST', '127.0.0.1'),
            port=int(os.getenv('POSTGRES_PORT', '5432')),
            database=os.getenv('POSTGRES_DB', 'git_rag'))
        cache = Path(os.getenv('MODEL_CACHE_DIR', str(ROOT / 'data/model-cache')))
        self.cache = (cache if cache.is_absolute() else ROOT / cache).resolve()
        self.request_log = ROOT / 'data/logs/requests.jsonl'


def redact(message):
    result = str(message)
    for key in ('GITHUG_TOKEN', 'GITHUB_TOKEN', 'HF_TOKEN', 'POSTGRES_PASSWORD'):
        secret = os.getenv(key)
        if secret:
            result = result.replace(secret, '[REDACTED]')
    return result[:1500]
