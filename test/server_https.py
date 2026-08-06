#!/usr/bin/python3
import uvicorn, os
os.chdir('/home/p400/warehouse_v2')
os.environ['PORT'] = '8001'
import server
uvicorn.run(server.app, host='0.0.0.0', port=8001, ssl_keyfile='key.pem', ssl_certfile='cert.pem')
