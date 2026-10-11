"""저장된 repo 벡터 검색과 로컬 답변 생성 테스트."""


class APIError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def error_response(request_id, error):
    return dict(request_id=request_id, status='error',
                error=dict(code=error.code, message=error.message))
