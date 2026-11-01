import hashlib,json,logging,os
import boto3,jwt
from botocore.exceptions import BotoCoreError,ClientError
logger=logging.getLogger(); logger.setLevel(logging.INFO)
ssm=boto3.client("ssm"); table=boto3.resource("dynamodb").Table(os.environ["AUTH_TOKENS_TABLE"])
def response(s,b): return {"statusCode":s,"headers":{"Content-Type":"application/json","Cache-Control":"no-store","Access-Control-Allow-Origin":"*","Access-Control-Allow-Headers":"Content-Type,Authorization,Idempotency-Key,X-Correlation-Id","Access-Control-Allow-Methods":"GET,POST,PUT,PATCH,DELETE,OPTIONS"},"body":json.dumps(b)}
def secret():
 env=os.environ["ENVIRONMENT"]; name=os.environ.get("JWT_SECRET_PARAMETER",f"/medislot/{env}/auth/jwt-secret")
 return ssm.get_parameter(Name=name,WithDecryption=True)["Parameter"]["Value"]
def h(t): return hashlib.sha256(t.encode()).hexdigest()
def lambda_handler(event,context):
 try:
  headers={str(k).lower():v for k,v in (event.get("headers") or {}).items()}
  auth=headers.get("authorization","")
  if not isinstance(auth,str) or not auth.startswith("Bearer "): return response(401,{"message":"Bearer access token is required"})
  claims=jwt.decode(auth[7:].strip(),secret(),algorithms=["HS256"],options={"require":["sub","role","exp","jti","sid"]})
  uid=str(claims["sub"]); sid=str(claims["sid"]); jti=str(claims["jti"])
  active=table.get_item(Key={"tokenId":f"ACTIVE#{uid}"},ConsistentRead=True).get("Item")
  if not active or active.get("sessionId")!=sid or active.get("jti")!=jti: return response(401,{"message":"Login session is no longer active"})
  table.delete_item(Key={"tokenId":f"ACTIVE#{uid}"},ConditionExpression="sessionId=:s AND jti=:j",ExpressionAttributeValues={":s":sid,":j":jti})
  raw=event.get("body") or "{}"; data=json.loads(raw) if isinstance(raw,str) else raw
  refresh=data.get("refreshToken") if isinstance(data,dict) else None
  if isinstance(refresh,str) and refresh:
   table.delete_item(Key={"tokenId":h(refresh)},ConditionExpression="sessionId=:s",ExpressionAttributeValues={":s":sid})
  return response(200,{"message":"Logout successful"})
 except jwt.InvalidTokenError: return response(401,{"message":"Invalid or expired access token"})
 except ClientError as exc:
  if exc.response.get("Error",{}).get("Code")=="ConditionalCheckFailedException": return response(401,{"message":"Login session is no longer active"})
  logger.exception("Logout dependency failure"); return response(500,{"message":"Logout could not be completed"})
 except (BotoCoreError,): logger.exception("Logout dependency failure"); return response(500,{"message":"Logout could not be completed"})
 except (json.JSONDecodeError,UnicodeDecodeError): return response(400,{"message":"Invalid JSON request"})
 except Exception: logger.exception("Logout failure"); return response(500,{"message":"Logout could not be completed"})
