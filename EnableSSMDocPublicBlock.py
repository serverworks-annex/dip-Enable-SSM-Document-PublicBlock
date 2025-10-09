import os
import json
import time
import random
import logging
import boto3
from botocore.exceptions import ClientError, EndpointConnectionError

# ===== Logging =====
logger = logging.getLogger()
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)
logger.setLevel(logging.INFO)

# ==== 設定 ====
REGIONS_ENV = os.getenv("REGIONS", "ALL").strip()

ROLE_CANDIDATES = [
    os.getenv("TARGET_ROLE_NAME", "OrganizationAccountAccessRole"),
    "OrganizationAccountAccessRole",
]

SETTING_ID = "/ssm/documents/console/public-sharing-permission"
DESIRED_VALUE = "Disable"  # "Enable" or "Disable"
SKIP_IF_SSM_NOT_READY = os.getenv("SKIP_IF_SSM_NOT_READY", "true").lower() == "true"
SCHEDULER_REGION = os.getenv("SCHEDULER_REGION", "us-east-1")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

TOPIC_ARN = os.getenv("SNS_TOPIC_ARN", "arn:aws:sns:ap-northeast-1:380311593622:test-ssm-enable-publicblock")
SNS_REGION = os.getenv("SNS_REGION", "ap-northeast-1")

# SCP で拒否された場合に error ではなく skipped とするか
SKIP_ON_SCP_DENY = os.getenv("SKIP_ON_SCP_DENY", "true").lower() == "true"

sts = boto3.client("sts")
scheduler = boto3.client("scheduler", region_name=SCHEDULER_REGION)
sns_client = boto3.client("sns", region_name=SNS_REGION)

# ---------- helpers ----------
def enumerate_regions(creds):
    """
    当該アカウントから到達可能な全リージョンを列挙。
    未オプトイン/到達不能なリージョンは除外。
    """
    if REGIONS_ENV and REGIONS_ENV.upper() != "ALL":
        regions = [r.strip() for r in REGIONS_ENV.split(",") if r.strip()]
        logger.info({"msg": "use_regions_from_env", "regions": regions})
        return regions

    ec2 = boto3.client(
        "ec2",
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name="us-east-1",
    )
    try:
        resp = ec2.describe_regions(AllRegions=True)
        regions = [
            r["RegionName"]
            for r in resp.get("Regions", [])
            if r.get("OptInStatus") in (None, "opt-in-not-required", "opted-in")
        ]
        logger.info({"msg": "use_regions_all", "regions": regions})
        return regions
    except ClientError as e:
        logger.error({"msg": "describe_regions_failed", "error": str(e)})
        return []


def assume_role_with_fallback(acct_id: str):
    last = None
    for role in ROLE_CANDIDATES:
        try:
            resp = sts.assume_role(
                RoleArn=f"arn:aws:iam::{acct_id}:role/{role}",
                RoleSessionName="SetSSMDocPublicBlock",
            )
            logger.info({"msg": "assume_role_success", "account": acct_id, "role": role})
            return role, resp["Credentials"]
        except ClientError as e:
            logger.info({"msg": "assume_role_failed", "account": acct_id, "role": role, "error": str(e)})
            last = e
    logger.error({"msg": "assume_role_all_failed", "account": acct_id, "error": str(last)})
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


def ensure_disabled(ssm_client, region: str):
    """Service Setting を DESIRED_VALUE に合わせる。"""
    # すでに一致なら何もしない
    try:
        cur = ssm_client.get_service_setting(SettingId=SETTING_ID)
        cur_val = cur.get("ServiceSetting", {}).get("SettingValue")
        if cur_val == DESIRED_VALUE:
            logger.info({"msg": "already_desired", "region": region, "value": cur_val})
            return "noop"
    except ssm_client.exceptions.ServiceSettingNotFound:
        logger.info({"msg": "setting_not_found", "region": region})

    if DRY_RUN:
        logger.info({"msg": "dry_run", "region": region, "action": "would_update"})
        return "would_update"

    ssm_client.update_service_setting(SettingId=SETTING_ID, SettingValue=DESIRED_VALUE)
    logger.info({"msg": "update_called", "region": region, "value": DESIRED_VALUE})

    # 反映確認
    for i in range(6):
        time.sleep(5)
        new = ssm_client.get_service_setting(SettingId=SETTING_ID)
        new_val = new.get("ServiceSetting", {}).get("SettingValue")
        if new_val == DESIRED_VALUE:
            logger.info({"msg": "update_confirmed", "region": region, "retry": i})
            return "updated"
    logger.warning({"msg": "update_unconfirmed", "region": region})
    return "updated_but_unconfirmed"


# SCP による拒否を検知
def _is_scp_deny(code: str, msg: str) -> bool:
    if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation"):
        text = (msg or "").lower()
        keys = (
            "service control policy",
            "explicit deny",
            "scp",
            "aws:requestedregion",  # リージョン制限でよく出るキー
        )
        return any(k in text for k in keys)
    return False


def handle_one_account(acct_id: str):
    logger.info({"msg": "start_account", "account": acct_id})
    role_used, creds = assume_role_with_fallback(acct_id)
    iam = iam_cli(creds)

    if SKIP_IF_SSM_NOT_READY and not is_ssm_ready(iam):
        logger.info({"msg": "slr_missing_skip", "account": acct_id})
        return {"assumed_role": role_used, "slr": "missing", "results": "skipped:not_ready"}

    regions = enumerate_regions(creds)
    results = {}
    for region in regions:
        try:
            cli = ssm_cli(creds, region)
            results[region] = ensure_disabled(cli, region)

        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            msg = e.response.get("Error", {}).get("Message", "")

            #  SCP による拒否は error にしない
            if SKIP_ON_SCP_DENY and _is_scp_deny(code, msg):
                logger.info({
                    "msg": "skipped_by_scp",
                    "account": acct_id, "region": region,
                    "code": code, "message": msg
                })
                results[region] = "skipped:SCP"
                continue

            logger.error({"msg": "client_error", "account": acct_id, "region": region, "code": code, "message": msg})

            if code in ("ThrottlingException", "TooManyRequestsException"):
                time.sleep(1 + random.random())
                try:
                    results[region] = ensure_disabled(cli, region)
                    continue
                except Exception as e2:
                    logger.error({"msg": "retry_failed", "account": acct_id, "region": region, "error": str(e2)})
                    results[region] = f"error:{type(e2).__name__}"
                    continue

            results[region] = f"error:{code}:{msg}"

        except EndpointConnectionError:
            logger.info({"msg": "endpoint_connection_error", "account": acct_id, "region": region})
            results[region] = "skipped:EndpointConnectionError"

        except Exception as e:
            logger.error({"msg": "unexpected_error", "account": acct_id, "region": region, "error": str(e)})
            results[region] = f"error:{type(e).__name__}"

    logger.info({"msg": "end_account", "account": acct_id, "results": results})
    return {"assumed_role": role_used, "slr": "ok", "results": results}


# skipped:* も成功相当として扱う
def is_success(account_result: dict) -> bool:
    if account_result.get("slr") == "missing":
        return False
    results = account_result.get("results", {})

    def _ok(v: str) -> bool:
        s = str(v)
        return s == "noop" or s.startswith("updated") or s.startswith("skipped")

    return all(_ok(v) for v in results.values())


def create_sns_message(account_id: str, success_logs, failure_logs, status: str) -> str:
    """SNS（Chatbot想定）に載せる本文(JSON文字列)を返す"""
    description = f"SSM『ドキュメントのパブリック共有ブロック』設定が {status} で終了しました。\n\n"

    if success_logs:
        description += "### ✅ 成功・スキップ\n" + "\n".join(f"- {m}" for m in success_logs) + "\n\n"
    if failure_logs:
        description += "### ⚠️ 失敗\n" + "\n".join(f"- {m}" for m in failure_logs)

    payload = {
        "version": "1.0",
        "source": "custom",
        "content": {
            "textType": "client-markdown",
            "title": f"【SSMパブリックブロック】{account_id} の実行結果 ({status})",
            "description": description.strip()
        }
    }
    return json.dumps(payload, ensure_ascii=False)


def result_notification(subject: str, message: str):
    """SNS トピックへ Publish（Chatbot/メール配信）"""
    if not TOPIC_ARN:
        logger.warning({"msg": "sns_topic_not_set"})
        return
    resp = sns_client.publish(
        TopicArn=TOPIC_ARN,
        Subject=subject[:100],   # SNS Subject は最大 100 文字
        Message=message
    )
    logger.info({"msg": "sns_published", "message_id": resp.get("MessageId"), "topic": TOPIC_ARN})


# ---------- Lambda entry ----------
def lambda_handler(event, context):
    # account を抽出（CreateAccountResult にも対応）
    acct = (
        event.get("account")
        or event.get("detail", {}).get("recipientAccountId")
        or event.get("detail", {}).get("userIdentity", {}).get("accountId")
        or event.get("detail", {}).get("serviceEventDetails", {}).get("createAccountStatus", {}).get("accountId")
    )
    if not acct:
        msg = {"msg": "account_not_found_in_event", "event": event}
        logger.error(msg)
        raise RuntimeError(json.dumps(msg, ensure_ascii=False))

    result = handle_one_account(acct)

    # 成功したら自分のスケジュールを削除
    schedule_name = event.get("scheduleName")
    if schedule_name and is_success(result):
        try:
            scheduler.delete_schedule(Name=schedule_name, GroupName="default")
            result["schedule_deleted"] = schedule_name
            logger.info({"msg": "schedule_deleted", "schedule": schedule_name})
        except Exception as e:
            result["schedule_delete_error"] = str(e)
            logger.error({"msg": "schedule_delete_error", "schedule": schedule_name, "error": str(e)})

    # ==== 通知 ====
    res = result.get("results", {}) if isinstance(result.get("results"), dict) else {}
    success_logs = [f"{r}: {v}" for r, v in res.items()
                    if str(v) == "noop" or str(v).startswith("updated") or str(v).startswith("skipped")]
    failure_logs = [f"{r}: {v}" for r, v in res.items() if str(v).startswith("error")]

    status = "SUCCESS" if (is_success(result) and not failure_logs) else "FAILURE"

    subject = f"[SSM PublicBlock] {acct} result ({status})"
    message = create_sns_message(acct, success_logs, failure_logs, status)
    try:
        result_notification(subject, message)
        result["sns"] = {"subject": subject}
    except Exception as e:
        result["sns_error"] = str(e)
        logger.error({"msg": "sns_publish_error", "error": str(e)})

    # 返却
    result["status"] = status
    return {
        "status": status,
        "account": acct,
        "detail": result
    }
