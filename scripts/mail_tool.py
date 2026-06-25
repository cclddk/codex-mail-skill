#!/usr/bin/env python3
import argparse
import datetime as dt
import email
from email.header import decode_header, make_header
from email.message import EmailMessage
from email import policy
import imaplib
import json
import math
import os
from pathlib import Path
import re
import smtplib
import ssl
import sys


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = SKILL_DIR / "configs" / "accounts.json"
DEFAULT_ENV = SKILL_DIR / "configs" / ".env"
DEFAULT_DRAFT_DIR = SKILL_DIR / "configs" / "drafts"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


AD_WORDS = [
    "unsubscribe", "promotion", "discount", "sale", "newsletter", "marketing",
    "advertisement", "offer", "coupon", "优惠", "促销", "广告", "退订", "订阅",
    "会员", "特惠", "活动", "推广"
]
REPLY_WORDS = [
    "please reply", "respond", "feedback", "review", "approve", "confirm",
    "需要回复", "请回复", "请确认", "审批", "反馈", "确认", "意见"
]
URGENT_WORDS = [
    "urgent", "asap", "deadline", "overdue", "important", "immediately",
    "紧急", "尽快", "截止", "逾期", "重要", "马上"
]
NOTIFY_WORDS = [
    "notification", "no-reply", "noreply", "receipt", "statement", "alert",
    "系统", "通知", "提醒", "验证码", "账单", "回执"
]


class MailToolError(Exception):
    pass


def print_json(data, status=0):
    print(json.dumps(data, ensure_ascii=False, indent=2))
    raise SystemExit(status)


def load_dotenv(path=DEFAULT_ENV):
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_config(path=DEFAULT_CONFIG):
    load_dotenv()
    with Path(path).open("r", encoding="utf-8") as f:
        data = json.load(f)
    accounts = data.get("accounts", [])
    if not isinstance(accounts, list):
        raise MailToolError("accounts.json must contain an accounts list")
    return accounts


def select_accounts(accounts, account_id):
    enabled = [a for a in accounts if a.get("enabled", True)]
    if account_id == "all":
        return enabled
    selected = [a for a in enabled if a.get("id") == account_id]
    if not selected:
        raise MailToolError(f"unknown or disabled account: {account_id}")
    return selected


def secret_for(account):
    env_name = account.get("password_env")
    if not env_name:
        return None
    return os.environ.get(env_name)


def require_secret(account):
    secret = secret_for(account)
    if not secret:
        raise MailToolError(
            f"missing secret for {account.get('id')}: set {account.get('password_env')} in configs/.env or the environment"
        )
    return secret


def decode_mime(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def html_to_text(html):
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<br\s*/?>", "\n", text)
    text = re.sub(r"(?s)</p\s*>", "\n", text)
    text = re.sub(r"(?s)<.*?>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def message_body(msg):
    if msg.is_multipart():
        html_part = None
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            if ctype == "text/plain":
                return part.get_content().strip()
            if ctype == "text/html" and html_part is None:
                html_part = part.get_content()
        if html_part:
            return html_to_text(html_part)
        return ""
    if msg.get_content_type() == "text/html":
        return html_to_text(msg.get_content())
    try:
        return msg.get_content().strip()
    except Exception:
        return ""


def snippet(text, max_len=320):
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "..."


def categorize_message(subject, sender, body):
    hay = f"{subject} {sender} {body}".lower()
    if any(word.lower() in hay for word in URGENT_WORDS):
        return "urgent"
    if any(word.lower() in hay for word in REPLY_WORDS):
        return "needs_reply"
    if any(word.lower() in hay for word in AD_WORDS):
        return "advertising"
    if any(word.lower() in hay for word in NOTIFY_WORDS):
        return "notification"
    return "fyi"


def brief_sentence(rec):
    subject = rec.get("subject") or "(no subject)"
    sender = rec.get("from") or "(unknown sender)"
    text = rec.get("snippet") or ""
    if text:
        text = snippet(text, 120)
        return f"{sender} sent '{subject}': {text}"
    return f"{sender} sent '{subject}'."


def connect_imap(account):
    secret = require_secret(account)
    host = account["imap_host"]
    port = int(account.get("imap_port", 993))
    conn = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context())
    conn.login(account.get("username") or account["email"], secret)
    return conn


def select_folder(conn, folder):
    typ, data = conn.select(folder, readonly=False)
    if typ != "OK":
        raise MailToolError(f"cannot select folder {folder}: {data}")


def uid_search(conn, since_date=None):
    criteria = ["ALL"]
    if since_date:
        criteria = ["SINCE", since_date.strftime("%d-%b-%Y")]
    typ, data = conn.uid("SEARCH", None, *criteria)
    if typ != "OK":
        raise MailToolError(f"imap search failed: {data}")
    return data[0].split()


def since_from_days(days):
    if days is None or days <= 0:
        return None
    return dt.datetime.now() - dt.timedelta(days=days)


def effective_limit(args, account_count):
    total_limit = getattr(args, "total_limit", None)
    if total_limit:
        return max(1, math.ceil(total_limit / max(1, account_count)))
    return args.limit


def normalize_uid(uid):
    if isinstance(uid, bytes):
        return uid.decode(errors="ignore")
    return str(uid)


def fetch_message(conn, uid):
    uid_text = normalize_uid(uid)
    typ, data = conn.uid("FETCH", uid_text, "(RFC822 FLAGS)")
    if typ != "OK" or not data:
        raise MailToolError(f"imap fetch failed for uid {uid}")
    raw = None
    flags = ""
    for item in data:
        if isinstance(item, tuple):
            flags = item[0].decode(errors="ignore")
            raw = item[1]
            break
    if raw is None:
        typ, data = conn.uid("FETCH", uid_text, "(RFC822)")
        if typ == "OK":
            for item in data:
                if isinstance(item, tuple):
                    raw = item[1]
                    flags = item[0].decode(errors="ignore")
                    break
    if raw is None:
        raise MailToolError(f"message uid {uid_text} not found; fetch response shape: {repr(data[:2])}")
    msg = email.message_from_bytes(raw, policy=policy.default)
    return msg, flags


def message_record(account, uid, msg, flags):
    body = message_body(msg)
    subject = decode_mime(msg.get("Subject", ""))
    sender = decode_mime(msg.get("From", ""))
    category = categorize_message(subject, sender, body)
    return {
        "account_id": account["id"],
        "account_email": account["email"],
        "uid": normalize_uid(uid),
        "message_id": msg.get("Message-ID", ""),
        "from": sender,
        "to": decode_mime(msg.get("To", "")),
        "date": msg.get("Date", ""),
        "subject": subject,
        "flags": flags,
        "is_read": "\\Seen" in flags,
        "category": category,
        "snippet": snippet(body),
    }


def list_accounts(args):
    accounts = load_config(args.config)
    rows = []
    for account in accounts:
        rows.append({
            "id": account.get("id"),
            "display_name": account.get("display_name"),
            "email": account.get("email"),
            "provider": account.get("provider", "imap"),
            "enabled": account.get("enabled", True),
            "imap_host": account.get("imap_host"),
            "imap_port": account.get("imap_port"),
            "smtp_host": account.get("smtp_host"),
            "smtp_port": account.get("smtp_port"),
            "folder": account.get("folder", "INBOX"),
            "password_env": account.get("password_env"),
            "secret_loaded": bool(secret_for(account)),
            "server_verified": account.get("server_verified", False),
            "login_verified": account.get("login_verified", False),
            "imap_resolves_to": account.get("imap_resolves_to", ""),
            "smtp_resolves_to": account.get("smtp_resolves_to", ""),
        })
    print_json({"accounts": rows, "config": str(Path(args.config).resolve()), "env_file": str(DEFAULT_ENV)})


def list_folders(args):
    accounts = select_accounts(load_config(args.config), args.account)
    results = []
    errors = []
    for account in accounts:
        try:
            conn = connect_imap(account)
            try:
                typ, data = conn.list()
                if typ != "OK":
                    raise MailToolError(f"folder list failed: {data}")
                folders = []
                for raw in data:
                    text = raw.decode(errors="ignore") if isinstance(raw, bytes) else str(raw)
                    folders.append(text)
                results.append({"account_id": account["id"], "folders": folders})
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as exc:
            errors.append({"account_id": account.get("id"), "error": str(exc)})
    print_json({"accounts": results, "errors": errors})


def fetch_recent(args):
    accounts = select_accounts(load_config(args.config), args.account)
    since = since_from_days(args.days)
    per_account_limit = effective_limit(args, len(accounts))
    all_records = []
    errors = []
    for account in accounts:
        try:
            conn = connect_imap(account)
            try:
                select_folder(conn, account.get("folder", "INBOX"))
                uids = uid_search(conn, since)
                for uid in reversed(uids[-per_account_limit:]):
                    try:
                        msg, flags = fetch_message(conn, uid)
                        rec = message_record(account, uid, msg, flags)
                        if args.unread_only and rec["is_read"]:
                            continue
                        all_records.append(rec)
                    except Exception as exc:
                        errors.append({"account_id": account.get("id"), "uid": normalize_uid(uid), "error": str(exc)})
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as exc:
            errors.append({"account_id": account.get("id"), "error": str(exc)})
    print_json({
        "scope": {
            "days_filter": args.days if args.days and args.days > 0 else None,
            "total_limit": getattr(args, "total_limit", None),
            "per_account_limit": per_account_limit,
        },
        "messages": all_records,
        "errors": errors,
    })


def summarize_inbox(args):
    accounts = select_accounts(load_config(args.config), args.account)
    since = since_from_days(args.days)
    per_account_limit = effective_limit(args, len(accounts))
    buckets = {"urgent": [], "needs_reply": [], "notification": [], "advertising": [], "fyi": []}
    errors = []
    for account in accounts:
        try:
            conn = connect_imap(account)
            try:
                select_folder(conn, account.get("folder", "INBOX"))
                uids = uid_search(conn, since)
                for uid in reversed(uids[-per_account_limit:]):
                    try:
                        msg, flags = fetch_message(conn, uid)
                        rec = message_record(account, uid, msg, flags)
                        if args.unread_only and rec["is_read"]:
                            continue
                        buckets.setdefault(rec["category"], []).append(rec)
                    except Exception as exc:
                        errors.append({"account_id": account.get("id"), "uid": normalize_uid(uid), "error": str(exc)})
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as exc:
            errors.append({"account_id": account.get("id"), "error": str(exc)})
    summary = {key: len(value) for key, value in buckets.items()}
    numbered_brief = []
    n = 1
    for category in ["urgent", "needs_reply", "notification", "advertising", "fyi"]:
        for rec in buckets.get(category, []):
            numbered_brief.append({
                "number": n,
                "category": category,
                "account_id": rec["account_id"],
                "uid": rec["uid"],
                "from": rec["from"],
                "subject": rec["subject"],
                "one_sentence": brief_sentence(rec),
            })
            n += 1
    print_json({
        "scope": {
            "days_filter": args.days if args.days and args.days > 0 else None,
            "total_limit": getattr(args, "total_limit", None),
            "per_account_limit": per_account_limit,
        },
        "summary": summary,
        "numbered_brief": numbered_brief,
        "buckets": buckets,
        "errors": errors,
    })


def draft_reply(args):
    accounts = select_accounts(load_config(args.config), args.account)
    if len(accounts) != 1:
        raise MailToolError("draft_reply requires one concrete account id")
    account = accounts[0]
    draft = {
        "account_id": account["id"],
        "from": account["email"],
        "to": args.to or "",
        "subject": args.subject or "",
        "body": args.body or "",
        "in_reply_to_uid": args.uid or "",
        "in_reply_to_message_id": "",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if args.uid:
        conn = connect_imap(account)
        try:
            select_folder(conn, account.get("folder", "INBOX"))
            msg, _flags = fetch_message(conn, args.uid)
            draft["to"] = args.to or decode_mime(msg.get("Reply-To") or msg.get("From") or "")
            subject = args.subject or decode_mime(msg.get("Subject", ""))
            draft["subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
            draft["in_reply_to_message_id"] = msg.get("Message-ID", "")
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    DEFAULT_DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = DEFAULT_DRAFT_DIR / f"draft-{account['id']}-{stamp}.json"
    path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    print_json({"draft_path": str(path), "draft": draft})


def connect_smtp(account):
    secret = require_secret(account)
    host = account["smtp_host"]
    port = int(account.get("smtp_port", 465))
    username = account.get("username") or account["email"]
    if port == 465:
        smtp = smtplib.SMTP_SSL(host, port, context=ssl.create_default_context())
    else:
        smtp = smtplib.SMTP(host, port)
        smtp.starttls(context=ssl.create_default_context())
    smtp.login(username, secret)
    return smtp


def send_reply(args):
    if args.confirm != "SEND":
        raise MailToolError("send_reply requires --confirm SEND")
    draft_path = Path(args.draft)
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    accounts = select_accounts(load_config(args.config), draft["account_id"])
    account = accounts[0]
    msg = EmailMessage()
    msg["From"] = draft["from"]
    msg["To"] = draft["to"]
    msg["Subject"] = draft["subject"]
    if draft.get("in_reply_to_message_id"):
        msg["In-Reply-To"] = draft["in_reply_to_message_id"]
        msg["References"] = draft["in_reply_to_message_id"]
    msg.set_content(draft["body"])
    smtp = connect_smtp(account)
    try:
        smtp.send_message(msg)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass
    print_json({"sent": True, "draft_path": str(draft_path), "account_id": account["id"], "to": draft["to"]})


def mark_read(args):
    if args.confirm != "MARK_READ":
        raise MailToolError("mark_read requires --confirm MARK_READ")
    account = select_accounts(load_config(args.config), args.account)[0]
    conn = connect_imap(account)
    try:
        select_folder(conn, account.get("folder", "INBOX"))
        typ, data = conn.uid("STORE", normalize_uid(args.uid), "+FLAGS", "(\\Seen)")
        if typ != "OK":
            raise MailToolError(f"mark read failed: {data}")
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    print_json({"marked_read": True, "account_id": account["id"], "uid": str(args.uid)})


def move_uid(conn, uid, dest_folder):
    uid_text = normalize_uid(uid)
    dest = dest_folder if dest_folder.startswith('"') and dest_folder.endswith('"') else f'"{dest_folder}"'
    typ, data = conn.uid("MOVE", uid_text, dest)
    if typ == "OK":
        return "MOVE"
    typ, data = conn.uid("COPY", uid_text, dest)
    if typ != "OK":
        raise MailToolError(f"move/copy failed: {data}")
    typ, data = conn.uid("STORE", uid_text, "+FLAGS", "(\\Deleted)")
    if typ != "OK":
        raise MailToolError(f"mark deleted after copy failed: {data}")
    return "COPY_AND_DELETE_FLAG"


def archive_or_move(args):
    if args.confirm != "MOVE":
        raise MailToolError("archive_or_move requires --confirm MOVE")
    account = select_accounts(load_config(args.config), args.account)[0]
    conn = connect_imap(account)
    method = None
    try:
        select_folder(conn, account.get("folder", "INBOX"))
        method = move_uid(conn, args.uid, args.dest_folder)
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    print_json({"moved": True, "method": method, "account_id": account["id"], "uid": str(args.uid), "dest_folder": args.dest_folder})


def delete_message(args):
    if args.confirm not in ("DELETE_AD", "DELETE_EXPLICIT"):
        raise MailToolError("delete_message requires --confirm DELETE_AD or --confirm DELETE_EXPLICIT")
    account = select_accounts(load_config(args.config), args.account)[0]
    conn = connect_imap(account)
    try:
        select_folder(conn, account.get("folder", "INBOX"))
        msg, flags = fetch_message(conn, args.uid)
        rec = message_record(account, args.uid, msg, flags)
        explicit_delete = args.allow_explicit and args.confirm == "DELETE_EXPLICIT"
        if rec["category"] != "advertising" and not explicit_delete:
            raise MailToolError(f"refusing delete: message category is {rec['category']}, not advertising")
        dest = args.trash_folder
        method = move_uid(conn, args.uid, dest)
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    print_json({"deleted_to_trash": True, "method": method, "account_id": account["id"], "uid": str(args.uid), "trash_folder": dest})


def delete_advertising(args):
    accounts = select_accounts(load_config(args.config), args.account)
    if args.confirm != "DELETE_ADS":
        raise MailToolError("delete_advertising requires --confirm DELETE_ADS")
    since = since_from_days(args.days)
    per_account_limit = effective_limit(args, len(accounts))
    deleted = []
    skipped = []
    errors = []
    for account in accounts:
        try:
            conn = connect_imap(account)
            try:
                select_folder(conn, account.get("folder", "INBOX"))
                uids = uid_search(conn, since)
                for uid in reversed(uids[-per_account_limit:]):
                    try:
                        msg, flags = fetch_message(conn, uid)
                        rec = message_record(account, uid, msg, flags)
                        if rec["category"] == "advertising":
                            method = move_uid(conn, uid, args.trash_folder)
                            deleted.append({
                                "account_id": account["id"],
                                "uid": rec["uid"],
                                "method": method,
                                "subject": rec["subject"],
                                "from": rec["from"],
                            })
                        else:
                            skipped.append({"account_id": account["id"], "uid": rec["uid"], "category": rec["category"]})
                    except Exception as exc:
                        errors.append({"account_id": account.get("id"), "uid": normalize_uid(uid), "error": str(exc)})
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as exc:
            errors.append({"account_id": account.get("id"), "error": str(exc)})
    print_json({"deleted": deleted, "skipped": skipped, "errors": errors})


def build_parser():
    parser = argparse.ArgumentParser(description="Multi-account IMAP/SMTP mail tool for Codex")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to accounts.json")
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("list_accounts")

    p = sub.add_parser("list_folders")
    p.add_argument("--account", default="all")

    p = sub.add_parser("fetch_recent")
    p.add_argument("--account", default="all")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--total-limit", type=int)
    p.add_argument("--unread-only", action="store_true")

    p = sub.add_parser("summarize_inbox")
    p.add_argument("--account", default="all")
    p.add_argument("--days", type=int, default=0)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--total-limit", type=int)
    p.add_argument("--unread-only", action="store_true")

    p = sub.add_parser("draft_reply")
    p.add_argument("--account", required=True)
    p.add_argument("--uid")
    p.add_argument("--to")
    p.add_argument("--subject")
    p.add_argument("--body", required=True)

    p = sub.add_parser("send_reply")
    p.add_argument("--draft", required=True)
    p.add_argument("--confirm", required=True)

    p = sub.add_parser("mark_read")
    p.add_argument("--account", required=True)
    p.add_argument("--uid", required=True)
    p.add_argument("--confirm", required=True)

    p = sub.add_parser("archive_or_move")
    p.add_argument("--account", required=True)
    p.add_argument("--uid", required=True)
    p.add_argument("--dest-folder", default="Archive")
    p.add_argument("--confirm", required=True)

    p = sub.add_parser("delete_message")
    p.add_argument("--account", required=True)
    p.add_argument("--uid", required=True)
    p.add_argument("--trash-folder", default="Trash")
    p.add_argument("--allow-explicit", action="store_true")
    p.add_argument("--confirm", required=True)

    p = sub.add_parser("delete_advertising")
    p.add_argument("--account", default="all")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--total-limit", type=int)
    p.add_argument("--trash-folder", default="Trash")
    p.add_argument("--confirm", required=True)

    return parser


def main():
    args = build_parser().parse_args()
    actions = {
        "list_accounts": list_accounts,
        "list_folders": list_folders,
        "fetch_recent": fetch_recent,
        "summarize_inbox": summarize_inbox,
        "draft_reply": draft_reply,
        "send_reply": send_reply,
        "mark_read": mark_read,
        "archive_or_move": archive_or_move,
        "delete_message": delete_message,
        "delete_advertising": delete_advertising,
    }
    try:
        actions[args.action](args)
    except SystemExit:
        raise
    except Exception as exc:
        print_json({"error": str(exc)}, status=1)


if __name__ == "__main__":
    main()
