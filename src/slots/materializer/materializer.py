import json, logging, os, time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo
import boto3, pymysql
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

log=logging.getLogger(); log.setLevel(logging.INFO)
ssm=boto3.client("ssm"); ddb=boto3.resource("dynamodb")
def param(name,decrypt=False): return ssm.get_parameter(Name=name,WithDecryption=decrypt)["Parameter"]["Value"]
def connection():
 env=os.environ["ENVIRONMENT"]
 return pymysql.connect(host=param(os.environ.get("DB_HOST_PARAMETER",f"/medislot/{env}/db/host")),
  port=int(param(os.environ.get("DB_PORT_PARAMETER",f"/medislot/{env}/db/port"))),
  user=os.environ["DB_USER"],password=param(os.environ.get("DB_PASSWORD_PARAMETER",f"/medislot/{env}/db/app-password"),True),
  database=os.environ.get("DB_NAME","medislot"),connect_timeout=8,read_timeout=20,write_timeout=20,
  autocommit=True,cursorclass=pymysql.cursors.DictCursor)
def utc_iso(value):
 return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")
def mysql_time(value):
 if isinstance(value,dt_time): return value
 if isinstance(value,timedelta):
  seconds=int(value.total_seconds()) % 86400
  return dt_time(seconds//3600,(seconds%3600)//60,seconds%60)
 return dt_time.fromisoformat(str(value))
def localize_candidates(naive, zone):
 # Return both folds for repeated autumn wall times. Nonexistent spring
 # wall times return no candidates after the UTC round-trip check.
 candidates=[]
 for fold in (0,1):
  aware=naive.replace(tzinfo=zone,fold=fold)
  back=aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
  if back==naive and all(x.utcoffset()!=aware.utcoffset() for x in candidates):
   candidates.append(aware)
 return candidates
def fetch_data(doctor_id=None):
 conn=connection()
 try:
  with conn.cursor() as cur:
   where="WHERE d.user_id=%s" if doctor_id else ""
   args=(doctor_id,) if doctor_id else ()
   cur.execute(f"""SELECT d.user_id,d.timezone,ct.consultation_type_id,ct.duration_min,ct.capacity,ct.is_active
       FROM doctors d JOIN consultation_types ct ON ct.doctor_user_id=d.user_id {where}""",args)
   types=cur.fetchall()
   result=[]
   for item in types:
    did=item["user_id"]; cid=item["consultation_type_id"]
    cur.execute("SELECT day_of_week,start_time,end_time FROM availability_patterns WHERE doctor_user_id=%s AND consultation_type_id=%s",(did,cid))
    patterns=cur.fetchall()
    cur.execute("SELECT blackout_date FROM blackout_dates WHERE doctor_user_id=%s",(did,))
    blackouts={str(x["blackout_date"]) for x in cur.fetchall()}
    cur.execute("""SELECT exception_date,exception_type,start_time,end_time FROM availability_exceptions
      WHERE doctor_user_id=%s AND consultation_type_id=%s""",(did,cid))
    exceptions=cur.fetchall()
    result.append({**item,"patterns":patterns,"blackouts":blackouts,"exceptions":exceptions})
   return result
 finally: conn.close()
def lambda_handler(event,context):
 event=event or {}; doctor_id=event.get("doctorId")
 date_from=date.fromisoformat(event["dateFrom"]) if event.get("dateFrom") else date.today()
 date_to=date.fromisoformat(event["dateTo"]) if event.get("dateTo") else date_from+timedelta(days=int(os.environ.get("SLOT_WINDOW_DAYS","30"))-1)
 if date_to<date_from: raise ValueError("dateTo must be on or after dateFrom")
 if (date_to-date_from).days>60: raise ValueError("Materialisation window cannot exceed 60 days")
 table=ddb.Table(os.environ["SLOTS_TABLE"])
 schedules=fetch_data(doctor_id)
 created=0; expected=set(); examined=0
 for schedule in schedules:
  if not schedule["is_active"]: continue
  did=str(schedule["user_id"]); cid=int(schedule["consultation_type_id"]); zone=ZoneInfo(schedule["timezone"])
  patterns=schedule["patterns"]; exceptions={}
  for ex in schedule["exceptions"]: exceptions.setdefault(str(ex["exception_date"]),[]).append(ex)
  day=date_from
  while day<=date_to:
   daykey=day.isoformat()
   if daykey not in schedule["blackouts"]:
    windows=[]
    day_ex=exceptions.get(daykey,[])
    if day_ex:
     if not any(x["exception_type"]=="OVERRIDE" for x in day_ex):
      windows=[(p["start_time"],p["end_time"]) for p in patterns if int(p["day_of_week"])==day.isoweekday()]
     for ex in day_ex:
      if ex["exception_type"]=="CLOSED": windows=[]
      elif ex["exception_type"] in ("ADD","OVERRIDE"):
       if ex["exception_type"]=="OVERRIDE": windows=[]
       windows.append((ex["start_time"],ex["end_time"]))
    else:
     windows=[(p["start_time"],p["end_time"]) for p in patterns if int(p["day_of_week"])==day.isoweekday()]
    for start_value,end_value in windows:
     start_t=mysql_time(start_value)
     end_t=mysql_time(end_value)
     local_start=datetime.combine(day,start_t); local_end=datetime.combine(day,end_t)
     end_candidates=localize_candidates(local_end,zone)
     if not end_candidates:
      # A schedule boundary that falls in a nonexistent spring-forward time
      # is skipped for this date rather than guessed.
      continue
     end_limit=max(x.astimezone(timezone.utc) for x in end_candidates)
     cursor=local_start
     duration=int(schedule["duration_min"])
     while cursor<local_end:
      for aware_start in localize_candidates(cursor,zone):
       end_instant=aware_start.astimezone(timezone.utc)+timedelta(minutes=duration)
       if end_instant<=end_limit:
        start_utc=utc_iso(aware_start); end_utc=end_instant.isoformat(timespec="seconds").replace("+00:00","Z")
        slot_id=f"{did}#{cid}#{start_utc}"
        expected.add(slot_id); examined+=1
        expiry=int((datetime.combine(day+timedelta(days=90),dt_time.min,tzinfo=timezone.utc)).timestamp())
        item={"slotId":slot_id,"doctorId":did,"consultationTypeId":str(cid),"startUtc":start_utc,
         "endUtc":end_utc,"localDate":daykey,"timezone":schedule["timezone"],"utcOffset":aware_start.strftime("%z"),
         "localStartTime":cursor.strftime("%H:%M:%S"),"durationMinutes":duration,
         "capacity":int(schedule["capacity"]),"bookedCount":0,"status":"OPEN",
         "source":"MATERIALIZED","expiresAt":expiry,"createdAt":int(time.time())}
        try:
         table.put_item(Item=item,ConditionExpression="attribute_not_exists(slotId)")
         created+=1
        except ClientError as exc:
         if exc.response.get("Error",{}).get("Code")!="ConditionalCheckFailedException": raise
      cursor+=timedelta(minutes=duration)
   day+=timedelta(days=1)
 # Reconcile generated future slots for every affected doctor. Never touch booked slots.
 doctor_ids={str(x["user_id"]) for x in schedules}
 for did in doctor_ids:
  now_utc=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")
  result=table.query(IndexName="gsi-doctor-time",KeyConditionExpression=Key("doctorId").eq(did) & Key("startUtc").gte(now_utc))
  old=result.get("Items",[])
  while result.get("LastEvaluatedKey"):
   result=table.query(IndexName="gsi-doctor-time",KeyConditionExpression=Key("doctorId").eq(did) & Key("startUtc").gte(now_utc),ExclusiveStartKey=result["LastEvaluatedKey"])
   old.extend(result.get("Items",[]))
  valid_for_doctor={sid for sid in expected if sid.startswith(did+"#")}
  for slot in old:
   if slot.get("source")=="MATERIALIZED" and slot["slotId"] not in valid_for_doctor and int(slot.get("bookedCount",0))==0:
    try:
     table.update_item(Key={"slotId":slot["slotId"]},UpdateExpression="SET #st=:cancel,updatedAt=:now",
       ConditionExpression="bookedCount=:zero",ExpressionAttributeNames={"#st":"status"},
       ExpressionAttributeValues={":cancel":"CANCELLED",":now":int(time.time()),":zero":0})
    except ClientError as exc:
     if exc.response.get("Error",{}).get("Code")!="ConditionalCheckFailedException": raise
 log.info(json.dumps({"event":"slot_materialisation_complete","doctorId":str(doctor_id) if doctor_id else None,
  "created":created,"examined":examined,"windowStart":date_from.isoformat(),"windowEnd":date_to.isoformat()}))
 return {"status":"ok","created":created,"examined":examined,"windowStart":date_from.isoformat(),"windowEnd":date_to.isoformat()}
