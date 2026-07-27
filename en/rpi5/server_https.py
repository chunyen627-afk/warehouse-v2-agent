#!/usr/bin/python3
import uvicorn, os
os.chdir('/home/p400/warehouse_v2_en')
os.environ['PORT'] = '8002'
import server
uvicorn.run(server.app, host='0.0.0.0', port=8002, ssl_keyfile='key.pem', ssl_certfile='cert.pem')
