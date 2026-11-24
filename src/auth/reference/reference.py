import json, os
import boto3, pymysql
from botocore.exceptions import BotoCoreError, ClientError
logger=logging.getLogger()
logger.setLevel(logging.INFO)
ssm=boto3.client("ssm")
def param(name,decrypt=False): return ssm.get_parameter(Name=name,WithDecryption=decrypt)["Parameter"]["Value"]
def response(status,body): return {"statusCode":status,"headers":{"Content-Type":"application/json","Cache-Control":"no-store","Access-Control-Allow-Origin":"*","Access-Control-Allow-Headers":"Content-Type,Authorization,Idempotency-Key,X-Correlation-Id","Access-Control-Allow-Methods":"GET,POST,PUT,PATCH,DELETE,OPTIONS"},"body":json.dumps(body)}
def _lambda_handler_impl(event,context):
 conn=None
 try:
  path=(event.get("path") or event.get("rawPath") or "")
  env=os.environ["ENVIRONMENT"]
  conn=pymysql.connect(host=param(f"/medislot/{env}/db/host"),port=int(param(f"/medislot/{env}/db/port")),
    user=os.environ["DB_USER"],password=param(f"/medislot/{env}/db/app-password",True),
    database="medislot",connect_timeout=5,read_timeout=10,autocommit=True,cursorclass=pymysql.cursors.DictCursor)
  with conn.cursor() as cur:
   if path.endswith("/reference/specialisations"):
    cur.execute("SELECT specialisation_id,name FROM specialisations ORDER BY name")
   elif path.endswith("/reference/hospitals"):
    cur.execute("SELECT hospital_id,name,city,address FROM hospitals WHERE status='active' ORDER BY city,name")
   else: return response(404,{"message":"Reference endpoint not found"})
   return response(200,{"items":cur.fetchall()})
 except (BotoCoreError,ClientError,pymysql.MySQLError):
  return response(500,{"message":"Reference data could not be loaded"})
 finally:
  if conn: conn.close()

def lambda_handler(event, context):
 import time
 started=time.monotonic()
 result=_lambda_handler_impl(event,context)
 status=int((result or {}).get("statusCode",500))
 level="info" if 200<=status<300 or status in (404,409) else ("warning" if status<500 else "error")
 headers={str(k).lower():v for k,v in (event.get("headers") or {}).items()}
 rc=event.get("requestContext") or {}
 record={"event":"http_response","service":"reference-data","method":event.get("httpMethod"),"path":event.get("path") or event.get("rawPath"),"statusCode":status,"statusDescription":"Request processed successfully" if status<300 else ("Requested resource or route was not found" if status==404 else ("Request validation was rejected" if status<500 else "Reference-data dependency failure")),"outcome":"success" if status<300 else ("client_rejected" if status<500 else "server_failure"),"requestId":getattr(context,"aws_request_id",None),"correlationId":headers.get("x-correlation-id") or rc.get("requestId"),"durationMs":round((time.monotonic()-started)*1000,2)}
 getattr(logger,level)(json.dumps(record,separators=(",",":")))
 return result

