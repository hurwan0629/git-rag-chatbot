import json
import threading
import uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from .config import ROOT, Settings
from .db import Database
from .errors import APIError
from .api.routes import router
from .jobs import Jobs
from .ml.encoder import Encoder
from .ml.generator import Generator
from .request_logging import RequestLogger


def error_response(request_id,status,code,message):
    return JSONResponse(dict(request_id=request_id,status='error',error=dict(code=code,message=message)),status_code=status)


class RequestGuard:
    def __init__(self,app):self.app=app

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':
            return await self.app(scope,receive,send)
        rid=str(uuid.uuid4());scope.setdefault('state',{})['request_id']=rid
        if scope['method']=='POST' and scope['path'].startswith('/api/'):
            headers=dict(scope['headers'])
            content_type=headers.get(b'content-type',b'').split(b';')[0].strip().lower()
            if content_type!=b'application/json':
                return await error_response(rid,415,'UNSUPPORTED_MEDIA_TYPE','application/json 본문이 필요합니다.')(scope,receive,send)
            messages=[];size=0
            while True:
                message=await receive()
                if message['type']=='http.disconnect':return
                size+=len(message.get('body',b''))
                if size>16384:
                    return await error_response(rid,413,'BODY_TOO_LARGE','POST 본문은 16 KiB 이하여야 합니다.')(scope,receive,send)
                messages.append(message)
                if not message.get('more_body'):break
            try:
                raw=b''.join(m.get('body',b'') for m in messages).decode('utf-8',errors='strict')
                def constant(_):raise ValueError('Non-finite JSON')
                def pairs(items):
                    result={}
                    for k,v in items:
                        if k in result:raise ValueError('Duplicate JSON key')
                        result[k]=v
                    return result
                value=json.loads(raw,parse_constant=constant,object_pairs_hook=pairs)
                json.dumps(value,ensure_ascii=False,allow_nan=False).encode('utf-8',errors='strict')
                if not isinstance(value,dict):raise ValueError('JSON object required')
            except (ValueError,UnicodeError):
                return await error_response(rid,400,'INVALID_JSON','유효한 UTF-8 JSON 객체가 필요합니다.')(scope,receive,send)
            pending=iter(messages)
            original_receive=receive
            async def replay():
                try:return next(pending)
                except StopIteration:return await original_receive()
            receive=replay
        await self.app(scope,receive,send)


def create_app(*,runtime=None,settings=None):
    @asynccontextmanager
    async def lifespan(app):
        if runtime is not None:
            for key,value in runtime.items():setattr(app.state,key,value)
            yield
            return
        cfg=settings or Settings();app.state.settings=cfg
        db=Database(cfg.database_url);app.state.db=db
        jobs=None
        try:
            print('Loading E5 encoder...',flush=True)
            encoder=Encoder(cfg);app.state.encoder=encoder
            print('Loading Qwen generator...' if not cfg.search_only else 'Search-only mode',flush=True)
            app.state.generator=None if cfg.search_only else Generator(cfg)
            app.state.inference_lock=threading.Lock()
            app.state.request_logger=RequestLogger(cfg.request_log)
            jobs=Jobs(db,cfg,encoder,app.state.inference_lock);app.state.jobs=jobs;jobs.start()
            print(f'Service ready on {encoder.device}',flush=True)
            yield
        finally:
            if jobs:jobs.stop()
            db.close()
    app=FastAPI(title='Git RAG Local MVP',version='0.1.0',lifespan=lifespan)
    app.add_middleware(RequestGuard)

    @app.exception_handler(APIError)
    async def api_error(request:Request,error:APIError):
        return error_response(request.state.request_id,error.status,error.code,str(error))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request:Request,error:RequestValidationError):
        return error_response(request.state.request_id,422,'VALIDATION_ERROR','입력 필드의 타입·형식·범위를 확인하세요.')

    @app.exception_handler(HTTPException)
    async def http_error(request:Request,error:HTTPException):
        return error_response(request.state.request_id,error.status_code,'NOT_FOUND' if error.status_code==404 else 'INVALID_REQUEST','요청 경로 또는 형식을 확인하세요.')

    @app.exception_handler(Exception)
    async def internal_error(request:Request,error:Exception):
        return error_response(request.state.request_id,500,'INTERNAL_ERROR','서버 처리 중 오류가 발생했습니다.')

    app.include_router(router)
    dist=ROOT/'frontend/dist'
    if dist.is_dir():app.mount('/',StaticFiles(directory=dist,html=True),name='frontend')
    return app
