import json, os
import boto3, pymysql
from botocore.exceptions import BotoCoreError, ClientError
ssm=boto3.client("ssm")
def param(name,decrypt=False): return ssm.get_parameter(Name=name,WithDecryption=decrypt)["Parameter"]["Value"]
def response(status,body): return {"statusCode":status,"headers":{"Content-Type":"application/json","Cache-Control":"no-store","Access-Control-Allow-Origin":"*","Access-Control-Allow-Headers":"Content-Type,Authorization,Idempotency-Key,X-Correlation-Id","Access-Control-Allow-Methods":"GET,POST,PUT,PATCH,DELETE,OPTIONS"},"body":json.dumps(body)}
def lambda_handler(event,context):
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
