import json, logging, os
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
    if effect=="Allow":
        result["context"]={"userId":str(uid),"role":str(role)}
    return result
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
        public_user_id = claims.get("publicUserId")
        if role not in ("doctor","patient"): raise jwt.InvalidTokenError("Invalid role")
        item=table.get_item(Key={"tokenId":f"ACTIVE#{uid}"},ConsistentRead=True).get("Item")
        if not item or item.get("tokenType")!="ACTIVE_ACCESS" or item.get("jti")!=jti or item.get("sessionId")!=sid:
            raise jwt.InvalidTokenError("Not the active login")
        result = policy(uid,"Allow",event.get("methodArn","*"),role)
        if public_user_id is not None:
            result["context"]["publicUserId"] = str(public_user_id)
        return result
    except jwt.InvalidTokenError: raise Exception("Unauthorized")
    except (BotoCoreError,ClientError):
        logger.exception("Authorizer dependency failure"); raise Exception("Unauthorized")
