import re
from uuid import UUID
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, field_validator
from .services.config import parse_repo_url, validate_path


class RequestModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class RepoRequest(RequestModel):
    repo_url: StrictStr

    @field_validator('repo_url')
    @classmethod
    def url(cls, value):
        return 'https://github.com/' + parse_repo_url(value.strip())


class IngestionRequest(RequestModel):
    ref: StrictStr | None = None
    scope_path: StrictStr = ''

    @field_validator('ref')
    @classmethod
    def ref_value(cls, value):
        if value is not None and (not value.strip() or len(value) > 255 or '\x00' in value):
            raise ValueError('ref must have 1–255 characters')
        return value

    @field_validator('scope_path')
    @classmethod
    def scope(cls, value):
        if len(value) > 4096:
            raise ValueError('scope_path is too long')
        return validate_path(value, allow_empty=True)


class SearchRequest(RequestModel):
    question: StrictStr
    repo_ids: list[UUID]
    top_k: StrictInt = 5

    @field_validator('question')
    @classmethod
    def question_value(cls, value):
        value = value.strip()
        if not value or '\x00' in value:
            raise ValueError('A nonempty question is required')
        return value

    @field_validator('repo_ids', mode='before')
    @classmethod
    def uuid_strings(cls, value):
        if not isinstance(value, list) or not value or any(not isinstance(v, str) for v in value):
            raise ValueError('repo_ids must be a nonempty list of UUID strings')
        return value

    @field_validator('repo_ids')
    @classmethod
    def unique(cls, value):
        if len(set(value)) != len(value):
            raise ValueError('Duplicate repo_ids')
        return value

    @field_validator('top_k')
    @classmethod
    def top(cls, value):
        if not 1 <= value <= 10:
            raise ValueError('top_k must be 1–10')
        return value


RUN_PATTERN = r'^\d{8}T\d{6}Z_[a-f0-9]{32}$'
HASH_PATTERN = r'^[a-f0-9]{64}$'
