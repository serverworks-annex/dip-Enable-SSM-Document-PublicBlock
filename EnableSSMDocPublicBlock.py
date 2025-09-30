# file: main_lambda.py
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
    # account を抽出
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