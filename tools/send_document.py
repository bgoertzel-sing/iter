DESCRIPTION = "Post a document/file to the protocosmo2 Telegram group. Args: path (file path, must exist), caption (optional text). Use for review docs, PDFs, logs, or any artifact too large for a chat message."

import json
import os
import sys
import urllib.request

_IDENTITY = "iter-outer-channel/protocosmo2/v1"
_MAX_BYTES = 45 * 1024 * 1024  # telegram bot limit is 50MB; stay under


def _channel_root():
    root = os.environ.get("ITER_OUTER_CHANNEL_ROOT")
    if not root:
        raise RuntimeError("ITER_OUTER_CHANNEL_ROOT is not set (protocosmo2 channel inactive)")
    return os.path.realpath(root)


def _source():
    """Extract the bound source envelope of the active request (same contract as the channel)."""
    root = _channel_root()
    proc = os.path.join(root, "processing")
    active = sorted(f for f in os.listdir(proc) if f.endswith(".json"))
    if len(active) != 1:
        raise RuntimeError("no single active request bound (cannot bind document to a chat)")
    with open(os.path.join(proc, active[0]), encoding="utf-8") as fh:
        request = json.load(fh)
    src = request.get("source") or {}
    chat_id = src.get("chat_id")
    if not isinstance(chat_id, int):
        raise RuntimeError("active request has no integer chat_id")
    return chat_id


def _bot_token():
    # reuse the repo token resolution: env first, then telegram_token.txt beside the repo
    token = os.environ.get("TG_BOT_TOKEN")
    if token:
        return token.strip()
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (
        os.path.join(here, "..", "telegram_token.txt"),
        os.path.expanduser("~/research-agent/projects/omegaclaw/protocosmo2/iter-port/telegram_token.txt"),
    ):
        cand = os.path.realpath(cand)
        if os.path.isfile(cand):
            with open(cand, encoding="utf-8") as fh:
                return fh.read().strip()
    raise RuntimeError("no TG_BOT_TOKEN env and no telegram_token.txt found")


def run(file_path: str, caption: str = ""):
    # NOTE: parameter must NOT be named ``path`` — it collides with
    # invoke_dynamic(path, function, *args, **kwargs) in iter.py, causing
    # "got multiple values for argument 'path'" when the model passes
    # {"path": "..."} as tool arguments.
    file_path = os.path.realpath(os.path.expanduser(file_path))
    if not os.path.isfile(file_path):
        return f"error: file not found: {file_path}"
    size = os.path.getsize(file_path)
    if size == 0 or size > _MAX_BYTES:
        return f"error: file size {size} out of range (1..{_MAX_BYTES})"
    chat_id = _source()
    token = _bot_token()
    filename = os.path.basename(file_path)
    boundary = "----IterDoc" + os.urandom(8).hex()
    body = bytearray()
    fields = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption[:1024]
    for k, v in fields.items():
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    body.extend(
        f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode()
    )
    with open(file_path, "rb") as fh:
        body.extend(fh.read())
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.load(resp)
    if not payload.get("ok"):
        return f"error: telegram rejected document: {payload}"
    msg_id = payload.get("result", {}).get("message_id")
    return f"document posted: {filename} ({size}B) -> chat {chat_id}, message_id {msg_id}"
