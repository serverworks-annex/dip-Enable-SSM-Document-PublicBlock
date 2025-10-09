import os, json, boto3, datetime, logging

SCHEDULER_REGION = os.getenv("SCHEDULER_REGION", "us-east-1")

# 本処理 Lambda の ARN
TARGET_LAMBDA_ARN = os.getenv("TARGET_LAMBDA_ARN", "arn:aws:lambda:us-east-1:380311593622:function:EnableSSMDocPublicBlock")

# EventBridge Scheduler が Assume するロール（scheduler.amazonaws.com を信頼、lambda:InvokeFunction を許可）
SCHEDULE_ROLE_ARN = os.getenv("SCHEDULE_ROLE_ARN", "arn:aws:iam::380311593622:role/schedule-creator-role")

DELAY_MIN = int(os.getenv("DELAY_MIN", "1")) 

# ===== SNS =====
TOPIC_ARN   = os.getenv("SNS_TOPIC_ARN","arn:aws:sns:ap-northeast-1:380311593622:test-ssm-enable-publicblock") 
SNS_REGION  = os.getenv("SNS_REGION", "ap-northeast-1")

scheduler = boto3.client("scheduler", region_name=SCHEDULER_REGION)
sns_client = boto3.client("sns", region_name=SNS_REGION)

# ===== Logging =====
logger = logging.getLogger()
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)
logger.setLevel(logging.INFO)


def _acct(event):
    d = event.get("detail", {}).get("serviceEventDetails", {}).get("createAccountStatus", {})
    return d.get("accountId") or event.get("account")


def _sns_message(acct, action, name, start_iso, ok=True, error_msg=None):
    """Chatbot/メール向けの本文(JSON文字列)を返す"""
    status = "SUCCESS" if ok else "FAILURE"
    desc_lines = [
        f"スケジュール {name} の {action} が {status} で終了しました。",
        "",
        f"- アカウント: `{acct}`",
        f"- アクション: `{action}`",
        f"- 次回実行(初回): `{start_iso}`" if start_iso else "- 次回実行(初回): なし",
    ]
    if error_msg:
        desc_lines += ["", "### ⚠️ エラー", f"- {error_msg}"]

    payload = {
        "version": "1.0",
        "source": "custom",
        "content": {
            "textType": "client-markdown",
            "title": f"【Scheduler作成/更新】{acct} - {action} ({status})",
            "description": "\n".join(desc_lines).strip()
        }
    }
    return json.dumps(payload, ensure_ascii=False)


def _publish_sns(subject: str, message: str):
    if not TOPIC_ARN:
        logger.warning({"msg": "sns_topic_not_set"})
        return
    resp = sns_client.publish(
        TopicArn=TOPIC_ARN,
        Subject=subject[:100],  # SNS Subject 制限
        Message=message
    )
    logger.info({"msg": "sns_published", "message_id": resp.get("MessageId"), "topic": TOPIC_ARN})


def lambda_handler(event, context):
    acct = _acct(event)
    if not acct:
        msg = f"accountId not found: {json.dumps(event, ensure_ascii=False)}"
        logger.error(msg)
        # 可能なら失敗通知
        try:
            _publish_sns("[Scheduler] accountId not found", _sns_message("-", "validate", "-", "-", ok=False, error_msg=msg))
        finally:
            raise RuntimeError(msg)

    name = f"daily-ssm-docblock-{acct}"
    start_dt = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=DELAY_MIN)).replace(microsecond=0)
    start_iso = start_dt.isoformat()
    payload = json.dumps({"account": acct, "scheduleName": name}, ensure_ascii=False)

    action = None
    ok = True
    error_msg = None

    try:
        scheduler.create_schedule(
            Name=name,
            GroupName="default",
            ScheduleExpression="rate(1 day)",
            StartDate=start_dt,
            FlexibleTimeWindow={"Mode": "OFF"},
            Target={
                "Arn": TARGET_LAMBDA_ARN,
                "RoleArn": SCHEDULE_ROLE_ARN,
                "Input": payload,
            },
        )
        action = "created"
        logger.info({"msg": "schedule_created", "name": name, "account": acct})
    except scheduler.exceptions.ConflictException:
        try:
            scheduler.update_schedule(
                Name=name,
                GroupName="default",
                ScheduleExpression="rate(1 day)",
                FlexibleTimeWindow={"Mode": "OFF"},
                Target={
                    "Arn": TARGET_LAMBDA_ARN,
                    "RoleArn": SCHEDULE_ROLE_ARN,
                    "Input": payload,
                },
            )
            action = "updated"
            logger.info({"msg": "schedule_updated", "name": name, "account": acct})
        except Exception as e:
            action = "update_failed"
            ok = False
            error_msg = str(e)
            logger.error({"msg": "schedule_update_error", "name": name, "account": acct, "error": error_msg})
    except Exception as e:
        action = "create_failed"
        ok = False
        error_msg = str(e)
        logger.error({"msg": "schedule_create_error", "name": name, "account": acct, "error": error_msg})

    # SNS 通知
    status_word = "SUCCESS" if ok else "FAILURE"
    subject = f"[Scheduler] {acct} {action} ({status_word})"
    try:
        _publish_sns(subject, _sns_message(acct, action, name, start_iso, ok=ok, error_msg=error_msg))
    except Exception as e:
        # SNS送信失敗は致命ではないため、結果に格納して返す
        logger.error({"msg": "sns_publish_error", "error": str(e)})

    return {
        "status": status_word,
        "action": action,
        "account": acct,
        "schedule": name,
        "first_run": start_iso,
        "error": error_msg,
    }
