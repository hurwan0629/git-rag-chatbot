import json
import threading
from datetime import datetime, timezone
from fastapi.encoders import jsonable_encoder


class RequestLogger:
    def __init__(self,path):
        self.path=path
        self.lock=threading.Lock()

    def write(self,record):
        record['at']=datetime.now(timezone.utc).isoformat()
        # Redact individual string values before encoding to keep JSON escaping intact.
        def clean(value):
            if isinstance(value,str):
                import os
                for key in ('GITHUG_TOKEN','GITHUB_TOKEN','HF_TOKEN','POSTGRES_PASSWORD'):
                    if os.getenv(key):value=value.replace(os.environ[key],'[REDACTED]')
                return value
            if isinstance(value,list):return [clean(v) for v in value]
            if isinstance(value,dict):return {k:clean(v) for k,v in value.items()}
            return value
        content=json.dumps(clean(jsonable_encoder(record)),ensure_ascii=False,allow_nan=False)
        with self.lock:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            with self.path.open('a',encoding='utf-8') as stream:
                stream.write(content+'\n')
