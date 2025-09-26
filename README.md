# 概要
新規 AWS Organizations メンバーアカウントが作成されたタイミングで、
該当アカウントの SSM Service Setting
ssm/documents/console/public-sharing-permission を Disable（パブリック共有ブロック） に自動設定する仕組みです。

# 構成図
<img width="1179" height="643" alt="image" src="https://github.com/user-attachments/assets/abd9b5a9-4c78-4222-a8f5-7cb24898fc80" />

# 処理フロー
1.新規アカウント作成 → ルール起動

・管理アカウントの EventBridge ルールがCreateAccountResultを検知

・ルールが スケジュール作成用 Lambda を起動

2.スケジュール作成用 Lambda

・EventBridge Scheduler に daily-ssm-docblock-<accountId> を作成（rate(1 day)

・ターゲットは 本処理 Lambda

3.本処理 Lambda（毎日実行）

・子アカウントを AssumeRole

・子アカウントに AWSServiceRoleForAmazonSSM（SSM の SLR）があるか IAM:GetRole で確認

・まだ無ければ：skipped:not_ready で終了（スケジュールは残る → 翌日再実行）

・対象リージョンで/ssm/documents/console/public-sharing-permission = Disable に更新（既に Disable なら noop、更新したら updated）

4.完了判定 & スケジュール削除

・全リージョンが updated または noop になったら成功

・受け取った scheduleName で Scheduler のスケジュールを削除

# スケジュール作成用Lambda：create-schedule-lambda
```
import os, json, boto3, datetime
SCHEDULER_REGION = "us-east-1"

# 本処理 Lambda の ARN
TARGET_LAMBDA_ARN = "arn:aws:lambda:us-east-1:AccountID:function:EnableSSMDocPublicBlock"

# ここは IAM ロールの ARN（scheduler.amazonaws.com を信頼、lambda:InvokeFunction を許可）
SCHEDULE_ROLE_ARN = "arn:aws:iam::AccountID:role/schedule-creator-role"

DELAY_MIN = 1  # 初回だけ少し遅らせたい場合

scheduler = boto3.client("scheduler", region_name=SCHEDULER_REGION)

def _acct(event):
    d = event.get("detail", {}).get("serviceEventDetails", {}).get("createAccountStatus", {})
    return d.get("accountId") or event.get("account")

def lambda_handler(event, context):
    acct = _acct(event)
    if not acct:
        raise RuntimeError(f"accountId not found: {json.dumps(event)}")

    name = f"daily-ssm-docblock-{acct}"
    start = (datetime.datetime.now(datetime.timezone.utc)
             + datetime.timedelta(minutes=DELAY_MIN)).replace(microsecond=0)
    payload = json.dumps({"account": acct, "scheduleName": name})

    try:
        scheduler.create_schedule(
            Name=name,
            GroupName="default",
            ScheduleExpression="rate(1 day)",
            StartDate=start,
            FlexibleTimeWindow={"Mode": "OFF"},
            Target={
                "Arn": TARGET_LAMBDA_ARN,
                "RoleArn": SCHEDULE_ROLE_ARN,
                "Input": payload
            }
        )
        action = "created"
    except scheduler.exceptions.ConflictException:
        scheduler.update_schedule(
            Name=name,
            GroupName="default",
            ScheduleExpression="rate(1 day)",
            FlexibleTimeWindow={"Mode": "OFF"},
            Target={
                "Arn": TARGET_LAMBDA_ARN,
                "RoleArn": SCHEDULE_ROLE_ARN,
                "Input": payload
            }
        )
        action = "updated"

    return {"status": action, "account": acct, "schedule": name, "first_run": start.isoformat()}

```
## IAMロール：lambda-schedule-creator-role 
### 信頼ポリシー
```
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {
                "Service": "lambda.amazonaws.com"
            },
            "Action": "sts:AssumeRole"
        }
    ]
}
```
### インナーポリシー：Lambda-Create-Schedule-Policy

```
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "ManageDailyDocblockSchedulesInDefaultGroup",
            "Effect": "Allow",
            "Action": [
                "scheduler:CreateSchedule",
                "scheduler:UpdateSchedule"
            ],
            "Resource": "arn:aws:scheduler:us-east-1:AccountID:schedule/default/daily-ssm-docblock-*"
        },
        {
            "Sid": "PassSchedulerInvokeRole",
            "Effect": "Allow",
            "Action": "iam:PassRole",
            "Resource": "arn:aws:iam::AccountID:role/schedule-creator-role"
        }
    ]
}
```

# 本処理用Lambda：EnableSSMDocPublicBlock
```
import os
import json
import time
import random
import boto3
from botocore.exceptions import ClientError, EndpointConnectionError

# ==== 設定 ====
REGIONS = [r.strip() for r in os.getenv(
    "REGIONS",
    "ap-northeast-1,ap-northeast-2,ap-northeast-3,us-east-1,us-east-2,us-west-1,us-west-2"
).split(",")]

ROLE_CANDIDATES = [
    os.getenv("TARGET_ROLE_NAME", "OrganizationAccountAccessRole"),
    "OrganizationAccountAccessRole"
]

SETTING_ID = "/ssm/documents/console/public-sharing-permission"
DESIRED_VALUE = "Disable"  # "Enable" or "Disable"
SKIP_IF_SSM_NOT_READY = os.getenv("SKIP_IF_SSM_NOT_READY", "true").lower() == "true"
SCHEDULER_REGION = os.getenv("SCHEDULER_REGION", "us-east-1")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

sts = boto3.client("sts")
scheduler = boto3.client("scheduler", region_name=SCHEDULER_REGION)

def assume_role_with_fallback(acct_id: str):
    last = None
    for role in ROLE_CANDIDATES:
        try:
            resp = sts.assume_role(
                RoleArn=f"arn:aws:iam::{acct_id}:role/{role}",
                RoleSessionName="SetSSMDocPublicBlock"
            )
            return role, resp["Credentials"]
        except ClientError as e:
            last = e
    raise last

def iam_cli(creds):
    return boto3.client(
        "iam",
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )

def ssm_cli(creds, region):
    return boto3.client(
        "ssm",
        region_name=region,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )

def is_ssm_ready(iam_client) -> bool:
    try:
        iam_client.get_role(RoleName="AWSServiceRoleForAmazonSSM")
        return True
    except iam_client.exceptions.NoSuchEntityException:
        return False
    except ClientError:
        return False

def ensure_disabled(ssm_client):
    # すでに Disable なら何もしない
    try:
        cur = ssm_client.get_service_setting(SettingId=SETTING_ID)
        if cur.get("ServiceSetting", {}).get("SettingValue") == DESIRED_VALUE:
            return "noop"
    except ssm_client.exceptions.ServiceSettingNotFound:
        pass

    if DRY_RUN:
        return "would_update"

    ssm_client.update_service_setting(SettingId=SETTING_ID, SettingValue=DESIRED_VALUE)
    # 反映確認（最大 ~30秒）
    for _ in range(6):
        time.sleep(5)
        new = ssm_client.get_service_setting(SettingId=SETTING_ID)
        if new.get("ServiceSetting", {}).get("SettingValue") == DESIRED_VALUE:
            return "updated"
    return "updated_but_unconfirmed"

def handle_one_account(acct_id: str):
    role_used, creds = assume_role_with_fallback(acct_id)
    iam = iam_cli(creds)

    if SKIP_IF_SSM_NOT_READY and not is_ssm_ready(iam):
        return {"assumed_role": role_used, "slr": "missing", "results": "skipped:not_ready"}

    results = {}
    for region in REGIONS:
        try:
            cli = ssm_cli(creds, region)
            results[region] = ensure_disabled(cli)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("ThrottlingException", "TooManyRequestsException"):
                time.sleep(1 + random.random())
                try:
                    results[region] = ensure_disabled(cli)
                    continue
                except Exception as e2:
                    results[region] = f"error:{type(e2).__name__}"
                    continue
            msg = e.response.get("Error", {}).get("Message", "")
            results[region] = f"error:{code}:{msg}"
        except EndpointConnectionError:
            results[region] = "skipped:EndpointConnectionError"
        except Exception as e:
            results[region] = f"error:{type(e).__name__}"

    return {"assumed_role": role_used, "slr": "ok", "results": results}

def is_success(account_result: dict) -> bool:
    if account_result.get("slr") == "missing":
        return False
    results = account_result.get("results", {})
    return all((v == "noop") or str(v).startswith("updated") for v in results.values())

def lambda_handler(event, context):
    # account を抽出（CreateAccountResult にも対応）
    acct = (
        event.get("account")
        or event.get("detail", {}).get("recipientAccountId")
        or event.get("detail", {}).get("userIdentity", {}).get("accountId")
        or event.get("detail", {}).get("serviceEventDetails", {}).get("createAccountStatus", {}).get("accountId")
    )
    if not acct:
        raise RuntimeError(f"account not found in event: {json.dumps(event)}")

    result = handle_one_account(acct)

    # 成功したら自分のスケジュールを削除
    schedule_name = event.get("scheduleName")
    if schedule_name and is_success(result):
        try:
            scheduler.delete_schedule(Name=schedule_name, GroupName="default")
            result["schedule_deleted"] = schedule_name
        except Exception as e:
            result["schedule_delete_error"] = str(e)

    print(json.dumps({"target_account": acct, **result}, ensure_ascii=False))
    return {"status": "ok", "target_account": acct, **result}

```
## IAMロール：Lambda-EnableSSMDocPublicBlock-Role 
### 信頼ポリシー
```
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {
                "Service": "lambda.amazonaws.com"
            },
            "Action": "sts:AssumeRole"
        }
    ]
}
```
### インナーポリシー：Lambda-EnableSSMDocPublicBlock-Policy

```
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "AssumeIntoOrgChildAccounts",
            "Effect": "Allow",
            "Action": "sts:AssumeRole",
            "Resource": "arn:aws:iam::*:role/OrganizationAccountAccessRole",
            "Condition": {
                "StringEquals": {
                    "aws:ResourceOrgID": "o-xxxxxxxxxx"
                }
            }
        },
        {
            "Sid": "Logs",
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents"
            ],
            "Resource": "*"
        },
        {
            "Sid": "OptionalOrgsRead",
            "Effect": "Allow",
            "Action": [
                "organizations:DescribeAccount",
                "organizations:ListAccounts",
                "organizations:ListParents",
                "organizations:ListChildren",
                "organizations:ListAccountsForParent"
            ],
            "Resource": "*"
        },
        {
            "Sid": "DeleteOwnDailySchedules",
            "Effect": "Allow",
            "Action": "scheduler:DeleteSchedule",
            "Resource": "arn:aws:scheduler:us-east-1:AccountIF:schedule/default/daily-ssm-docblock-*"
        }
    ]
}
```

メモリ：256MB

エフェメラルストレージ：512MB

タイムアウト：1分


# EventBridge：CreateSchedule
```
{
  "detail-type": ["AWS Service Event via CloudTrail"],
  "source": ["aws.organizations"],
  "detail": {
    "eventSource": ["organizations.amazonaws.com"],
    "serviceEventDetails": {
      "createAccountStatus": {
        "state": ["SUCCEEDED"]
      }
    },
    "eventName": ["CreateAccountResult"]
  }
}
```
## Target：スケジュール作成用Lambda（create-schedule-lambda ）




