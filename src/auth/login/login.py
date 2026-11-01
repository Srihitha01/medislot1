import hashlib, json, logging, os, re, secrets, time, uuid
import bcrypt, boto3, jwt, pymysql
from botocore.exceptions import BotoCoreError, ClientError

logger=logging.getLogger(); logger.setLevel(logging.INFO)
ssm=boto3.client("ssm"); ddb=boto3.resource("dynamodb")
TOKEN_TABLE_NAME=os.environ["AUTH_TOKENS_TABLE"]
tokens=ddb.Table(TOKEN_TABLE_NAME)
ACCESS_MIN=int(os.environ.get("ACCESS_TOKEN_MINUTES","15")); REFRESH_DAYS=int(os.environ.get("REFRESH_TOKEN_DAYS","7"))

def response(status,body):
    return {"statusCode":status,"headers":{"Content-Type":"application/json","Cache-Control":"no-store","Access-Control-Allow-Origin":"*","Access-Control-Allow-Headers":"Content-Type,Authorization,Idempotency-Key,X-Correlation-Id","Access-Control-Allow-Methods":"GET,POST,PUT,PATCH,DELETE,OPTIONS"},"body":json.dumps(body)}
def param(name,decrypt=False):
    return ssm.get_parameter(Name=name,WithDecryption=decrypt)["Parameter"]["Value"]
def secret():
    env=os.environ["ENVIRONMENT"]
    return param(os.environ.get("JWT_SECRET_PARAMETER",f"/medislot/{env}/auth/jwt-secret"),True)
def db():
    env=os.environ["ENVIRONMENT"]
    return pymysql.connect(host=param(os.environ.get("DB_HOST_PARAMETER",f"/medislot/{env}/db/host")),
      port=int(param(os.environ.get("DB_PORT_PARAMETER",f"/medislot/{env}/db/port"))),
      user=os.environ["DB_USER"],password=param(os.environ.get("DB_PASSWORD_PARAMETER",f"/medislot/{env}/db/app-password"),True),
      database=os.environ.get("DB_NAME","medislot"),connect_timeout=5,read_timeout=10,autocommit=True,cursorclass=pymysql.cursors.DictCursor)
def hash_token(value): return hashlib.sha256(value.encode()).hexdigest()
def lambda_handler(event,context):
    try:
        raw=event.get("body") or "{}"; data=json.loads(raw) if isinstance(raw,str) else raw
        if not isinstance(data,dict): return response(400,{"message":"Invalid request body"})
        email=data.get("email"); password=data.get("password")
        if not isinstance(email,str) or not isinstance(password,str): return response(400,{"message":"Email and password are required"})
        email=email.strip().lower()
        if not re.fullmatch(r"^[^@\s]+@[^@\s]+\.[^@\s]+$",email): return response(400,{"message":"Invalid email address"})
        conn=db()
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT user_id,email,password_hash,role,status FROM users WHERE email=%s LIMIT 1""",(email,))
                user=cur.fetchone()
        finally: conn.close()
        if not user or user["status"]!="active" or not bcrypt.checkpw(password.encode(),user["password_hash"].encode()):
            return response(401,{"message":"Invalid email or password"})
        now=int(time.time()); user_id=str(user["user_id"]); role=user["role"]
        access_jti=str(uuid.uuid4()); session_id=str(uuid.uuid4())
        access_exp=now+ACCESS_MIN*60; refresh_exp=now+REFRESH_DAYS*86400
        access=jwt.encode({"sub":user_id,"role":role,"iat":now,"exp":access_exp,"jti":access_jti,"sid":session_id},secret(),algorithm="HS256")
        refresh=secrets.token_urlsafe(48)
        # Active record is keyed by user, so each new login replaces the previous device/session.
        tokens.meta.client.transact_write_items(TransactItems=[
            {"Put":{"TableName":TOKEN_TABLE_NAME,"Item":{
                "tokenId":{"S":f"ACTIVE#{user_id}"},"tokenType":{"S":"ACTIVE_ACCESS"},
                "userId":{"S":user_id},"jti":{"S":access_jti},"sessionId":{"S":session_id},
                "expiresAt":{"N":str(access_exp)}}}},
            {"Put":{"TableName":TOKEN_TABLE_NAME,"Item":{
                "tokenId":{"S":hash_token(refresh)},"tokenType":{"S":"REFRESH"},
                "userId":{"S":user_id},"role":{"S":role},"sessionId":{"S":session_id},
                "expiresAt":{"N":str(refresh_exp)}}}}
        ])
        logger.info(json.dumps({"event":"login_succeeded","userId":user_id,"role":role}))
        return response(200,{"accessToken":access,"tokenType":"Bearer","expiresIn":ACCESS_MIN*60,
            "refreshToken":refresh,"refreshExpiresIn":REFRESH_DAYS*86400})
    except (json.JSONDecodeError,UnicodeDecodeError): return response(400,{"message":"Invalid JSON request"})
    except (BotoCoreError,ClientError,pymysql.MySQLError):
        logger.exception("Login dependency failure"); return response(500,{"message":"Login could not be completed"})
    except Exception:
        logger.exception("Login failure"); return response(500,{"message":"Login could not be completed"})
