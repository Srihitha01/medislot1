import json, logging, os, re
import boto3, jwt
from botocore.exceptions import BotoCoreError, ClientError

logger=logging.getLogger(); logger.setLevel(logging.INFO)
ssm=boto3.client("ssm"); table=boto3.resource("dynamodb").Table(os.environ["AUTH_TOKENS_TABLE"])
def get_secret():
    env=os.environ["ENVIRONMENT"]
    name=os.environ.get("JWT_SECRET_PARAMETER",f"/medislot/{env}/auth/jwt-secret")
    return ssm.get_parameter(Name=name,WithDecryption=True)["Parameter"]["Value"]
def policy(uid,effect,resource,role=None):
    result={"principalId":str(uid),"policyDocument":{"Version":"2012-10-17","Statement":[{"Action":"execute-api:Invoke","Effect":effect,"Resource":resource}]}}
    if effect=="Allow": result["context"]={"userId":str(uid),"role":str(role)}
    return result
# User ids are whole numbers (1, 2, 3 ...). A token for anything else, such as an
# old UUID-style id, is not accepted. MySQL would read a leading number in text
# like "5f3e-..." as 5, so this check must stay strict.
USER_ID_RE=re.compile(r"[1-9][0-9]{0,19}")
def extract(event):
    value=event.get("authorizationToken")
    if not value:
        headers=event.get("headers") or {}
        value=next((v for k,v in headers.items() if k.lower()=="authorization"),None)
    return value[7:].strip() if isinstance(value,str) and value.startswith("Bearer ") else None
def lambda_handler(event,context):
    token=extract(event)
    if not token: raise Exception("Unauthorized")
    try:
        claims=jwt.decode(token,get_secret(),algorithms=["HS256"],options={"require":["sub","role","iat","exp","jti","sid"]})
        uid=str(claims["sub"]); role=claims["role"]; jti=claims["jti"]; sid=claims["sid"]
        if not USER_ID_RE.fullmatch(uid): raise jwt.InvalidTokenError("Invalid subject")
        if role not in ("doctor","patient"): raise jwt.InvalidTokenError("Invalid role")
        item=table.get_item(Key={"tokenId":f"ACTIVE#{uid}"},ConsistentRead=True).get("Item")
        if not item or item.get("tokenType")!="ACTIVE_ACCESS" or item.get("jti")!=jti or item.get("sessionId")!=sid:
            raise jwt.InvalidTokenError("Not the active login")
        return policy(uid,"Allow",event.get("methodArn","*"),role)
    except jwt.InvalidTokenError: raise Exception("Unauthorized")
    except (BotoCoreError,ClientError):
        logger.exception("Authorizer dependency failure"); raise Exception("Unauthorized")
