"""Start the built frontend and API on a single localhost port."""
import argparse
import socket
import uvicorn
from app.config import Settings
from app.main import create_app


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8000)
    parser.add_argument('--device',choices=['auto','cuda','cpu'])
    parser.add_argument('--search-only',action='store_true',default=None)
    args=parser.parse_args()
    if not 1<=args.port<=65535:parser.error('port must be 1–65535')
    listener=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    try:
        if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
            listener.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        else:
            listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        # Detect occupied ports before loading several GB of model weights.
        listener.bind(('127.0.0.1',args.port))
        config=uvicorn.Config(create_app(settings=Settings(device=args.device,search_only=args.search_only)),
                              host='127.0.0.1',port=args.port,workers=1)
        uvicorn.Server(config).run(sockets=[listener])
    except KeyboardInterrupt:
        pass
    finally:
        listener.close()


if __name__=='__main__':main()
