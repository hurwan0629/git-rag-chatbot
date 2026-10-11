"""실행 기본값, 입력 검증, 토큰을 출력하지 않는 환경 로딩."""

import os
import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parents[2]
OUTPUT_DIR = PROJECT_ROOT / 'data' / 'exports'
CACHE_DIR = PROJECT_ROOT / 'data' / 'model-cache'
MODEL_ID = 'intfloat/multilingual-e5-small'
DIMENSION = 384
MAX_INPUT_TOKENS = 512
PREFIX = 'passage: '
CHUNKING = dict(method='line_greedy_char_fallback', max_input_tokens=512,
                overlap_tokens=0, offset_unit='python_char')
EXCLUDED_DIRS = ['.git', '.venv', 'venv', 'node_modules', '__pycache__', 'dist', 'build']
CREDENTIAL_NAMES = ['.env', 'credentials.json', 'id_rsa', 'id_ed25519']
CREDENTIAL_EXTENSIONS = ['.pem', '.key', '.p12', '.pfx']
DEFAULT_EXTENSIONS = [
    '.py', '.md', '.txt', '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.vue', '.svelte',
    '.html', '.css', '.scss', '.sass', '.less', '.json', '.yaml', '.yml', '.toml', '.xml',
    '.ini', '.cfg', '.conf', '.properties', '.sql', '.sh', '.bash', '.ps1',
    '.java', '.kt', '.kts', '.gradle', '.go', '.rs', '.c', '.h', '.cpp', '.hpp', '.cs',
    '.rb', '.php', '.swift', '.dart', '.lua', '.r', '.scala', '.tf',
]
SUPPORTED_FILENAMES = ['readme', 'license', 'dockerfile', 'makefile',
                       '.gitignore', '.dockerignore', '.editorconfig']


def parse_repo_url(value):
    url = urlsplit(value)
    match = re.fullmatch(r'/([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+)/?', url.path)
    if url.scheme != 'https' or url.netloc != 'github.com' or not match or url.query or url.fragment:
        raise ValueError('GitHub repo 루트 URL이 필요합니다: https://github.com/owner/repo')
    owner, name = match.groups()
    name = name.removesuffix('.git')
    if name in ('', '.', '..'):
        raise ValueError('올바른 repo 이름이 필요합니다.')
    return f'{owner}/{name}'.lower()


def validate_path(value, allow_empty=False):
    if allow_empty and value == '':
        return value
    if (not isinstance(value, str) or not value or '\\' in value or '\x00' in value
            or PurePosixPath(value).is_absolute() or re.match(r'^[A-Za-z]:', value)
            or any(part in ('', '.', '..') for part in value.split('/'))):
        raise ValueError('repo 상대 경로가 필요합니다. 절대 경로, 빈 구성 요소, ..는 허용하지 않습니다.')
    return value


def load_env(env_file=None):
    from dotenv import load_dotenv
    if env_file is not None:
        path = Path(env_file).resolve()
        if not path.is_file():
            raise ValueError('명시한 .env 파일이 없습니다.')
        load_dotenv(path, override=False)
    else:
        for folder in (PROJECT_ROOT, *PROJECT_ROOT.parents):
            path = folder / '.env'
            if path.is_file():
                load_dotenv(path, override=False)
                break
    return os.environ.get('GITHUG_TOKEN', '').strip() or os.environ.get('GITHUB_TOKEN', '').strip() or None


def filter_settings(args):
    return dict(max_file_bytes=args.max_file_bytes, extensions=args.extensions,
                extensionless_names=SUPPORTED_FILENAMES, excluded_directories=EXCLUDED_DIRS,
                credential_names=CREDENTIAL_NAMES, credential_extensions=CREDENTIAL_EXTENSIONS,
                env_pattern='.env.*')


def safe_error(error, token=None):
    message = str(error)
    for secret in (token, os.environ.get('GITHUG_TOKEN'), os.environ.get('GITHUB_TOKEN'),
                   os.environ.get('HF_TOKEN')):
        if secret:
            message = message.replace(secret, '[REDACTED]')
    return message[:1500]
