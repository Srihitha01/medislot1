import hashlib,json,logging,os,time,uuid
import boto3,jwt
from botocore.exceptions import BotoCoreError,ClientError
logger=logging.getLogger(); logger.setLevel(logging.INFO)
ssm=boto3.client("ssm"); table=boto3.resource("dynamodb").Table(os.environ["AUTH_TOKENS_TABLE"])
ACCESS_MIN=int(os.environ.get("ACCESS_TOKEN_MINUTES","15"))
def response(s,b): return {"statusCode":s,"headers":{"Content-Type":"application/json","Cache-Control":"no-store","Access-Control-Allow-Origin":"*","Access-Control-Allow-Headers":"Content-Type,Authorization,Idempotency-Key,X-Correlation-Id","Access-Control-Allow-Methods":"GET,POST,PUT,PATCH,DELETE,OPTIONS"},"body":json.dumps(b)}
def secret():
 env=os.environ["ENVIRONMENT"]; name=os.environ.get("JWT_SECRET_PARAMETER",f"/medislot/{env}/auth/jwt-secret")
 return ssm.get_parameter(Name=name,WithDecryption=True)["Parameter"]["Value"]
def h(t): return hashlib.sha256(t.encode()).hexdigest()
def lambda_handler(event,context):
 try:
  raw=event.get("body") or "{}"; data=json.loads(raw) if isinstance(raw,str) else raw
  token=data.get("refreshToken") if isinstance(data,dict) else None
  if not isinstance(token,str) or not token: return response(400,{"message":"refreshToken is required"})
  item=table.get_item(Key={"tokenId":h(token)},ConsistentRead=True).get("Item")
  now=int(time.time())
  if not item or item.get("tokenType")!="REFRESH" or int(item.get("expiresAt",0))<=now: return response(401,{"message":"Invalid or expired refresh token"})
  uid=str(item["userId"]); sid=str(item["sessionId"])
  active=table.get_item(Key={"tokenId":f"ACTIVE#{uid}"},ConsistentRead=True).get("Item")
  if not active or active.get("sessionId")!=sid: return response(401,{"message":"This login session is no longer active"})
  jti=str(uuid.uuid4()); exp=now+ACCESS_MIN*60
  access=jwt.encode({"sub":uid,"role":item["role"],"iat":now,"exp":exp,"jti":jti,"sid":sid},secret(),algorithm="HS256")
  # Conditional update prevents a stale refresh request from overwriting a newer login.
  table.update_item(Key={"tokenId":f"ACTIVE#{uid}"},
    UpdateExpression="SET jti=:j, expiresAt=:e",
    ConditionExpression="sessionId=:s AND tokenType=:t",
    ExpressionAttributeValues={":j":jti,":e":exp,":s":sid,":t":"ACTIVE_ACCESS"})
  return response(200,{"accessToken":access,"tokenType":"Bearer","expiresIn":ACCESS_MIN*60,
    "refreshToken":token,"refreshExpiresIn":int(item["expiresAt"])-now})
 except ClientError as exc:
  if exc.response.get("Error",{}).get("Code")=="ConditionalCheckFailedException": return response(401,{"message":"Login session is no longer active"})
  logger.exception("Refresh dependency failure"); return response(500,{"message":"Token refresh could not be completed"})
 except (BotoCoreError,): logger.exception("Refresh dependency failure"); return response(500,{"message":"Token refresh could not be completed"})
 except (json.JSONDecodeError,UnicodeDecodeError): return response(400,{"message":"Invalid JSON request"})
 except Exception: logger.exception("Refresh failure"); return response(500,{"message":"Token refresh could not be completed"})
