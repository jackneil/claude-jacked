#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["requests"]
# ///
"""X API v2 poster + v1.1 chunked video upload, OAuth 1.0a user context.

Creds from env: X_API_KEY, X_API_KEY_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET.
Commands:
  verify
  upload <video.mp4>                                 -> prints media_id
  launch <video.mp4> <main.txt> <r1.txt> <r2.txt>    -> upload+tweet+2 replies
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.parse

import requests

CK = os.environ["X_API_KEY"]
CS = os.environ["X_API_KEY_SECRET"]
AT = os.environ["X_ACCESS_TOKEN"]
ATS = os.environ["X_ACCESS_TOKEN_SECRET"]
UP = "https://upload.twitter.com/1.1/media/upload.json"
TW = "https://api.twitter.com/2/tweets"
ME = "https://api.twitter.com/2/users/me"


def enc(s):
    return urllib.parse.quote(str(s), safe="~")


def auth(method, url, params=None):
    o = {
        "oauth_consumer_key": CK,
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": AT,
        "oauth_version": "1.0",
    }
    allp = dict(o)
    if params:
        allp.update({k: str(v) for k, v in params.items()})
    bp = "&".join(f"{enc(k)}={enc(allp[k])}" for k in sorted(allp))
    base = "&".join([method.upper(), enc(url), enc(bp)])
    key = f"{enc(CS)}&{enc(ATS)}"
    o["oauth_signature"] = base64.b64encode(
        hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()
    ).decode()
    return "OAuth " + ", ".join(f'{enc(k)}="{enc(o[k])}"' for k in sorted(o))


def verify():
    r = requests.get(ME, headers={"Authorization": auth("GET", ME)}, timeout=30)
    print(r.status_code, r.text)
    return r.ok


def whoami():
    """Return the authed account's @handle (username) for the status URL, or None."""
    try:
        r = requests.get(ME, headers={"Authorization": auth("GET", ME)}, timeout=30)
        return r.json().get("data", {}).get("username") if r.ok else None
    except Exception:
        return None


def upload_video(path):
    total = os.path.getsize(path)
    p = {
        "command": "INIT",
        "total_bytes": total,
        "media_type": "video/mp4",
        "media_category": "tweet_video",
    }
    r = requests.post(UP, data=p, headers={"Authorization": auth("POST", UP, p)}, timeout=60)
    if not r.ok:
        raise RuntimeError(f"INIT {r.status_code}: {r.text}")
    mid = r.json()["media_id_string"]
    print("INIT ok media_id", mid, file=sys.stderr)
    seg = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(4 * 1024 * 1024)
            if not chunk:
                break
            q = {"command": "APPEND", "media_id": mid, "segment_index": seg}
            rr = requests.post(
                UP,
                params=q,
                files={"media": chunk},
                headers={"Authorization": auth("POST", UP, q)},
                timeout=180,
            )
            if not rr.ok:
                raise RuntimeError(f"APPEND {seg} {rr.status_code}: {rr.text}")
            seg += 1
            print("APPEND", seg, file=sys.stderr)
    p = {"command": "FINALIZE", "media_id": mid}
    r = requests.post(UP, data=p, headers={"Authorization": auth("POST", UP, p)}, timeout=60)
    if not r.ok:
        raise RuntimeError(f"FINALIZE {r.status_code}: {r.text}")
    pi = r.json().get("processing_info")
    while pi and pi.get("state") in ("pending", "in_progress"):
        wait = pi.get("check_after_secs", 3)
        print("processing… wait", wait, file=sys.stderr)
        time.sleep(wait)
        q = {"command": "STATUS", "media_id": mid}
        r = requests.get(UP, params=q, headers={"Authorization": auth("GET", UP, q)}, timeout=60)
        if not r.ok:
            raise RuntimeError(f"STATUS {r.status_code}: {r.text}")
        pi = r.json().get("processing_info")
    if pi and pi.get("state") == "failed":
        raise RuntimeError("processing failed: " + json.dumps(pi))
    print("media ready", mid, file=sys.stderr)
    return mid


def post(text, media_ids=None, reply_to=None):
    body = {"text": text}
    if media_ids:
        body["media"] = {"media_ids": [str(m) for m in media_ids]}
    if reply_to:
        body["reply"] = {"in_reply_to_tweet_id": str(reply_to)}
    r = requests.post(
        TW,
        json=body,
        headers={"Authorization": auth("POST", TW), "Content-Type": "application/json"},
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f"tweet {r.status_code}: {r.text}")
    return r.json()["data"]["id"]


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "verify":
        verify()
    elif cmd == "upload":
        print(upload_video(sys.argv[2]))
    elif cmd == "launch":
        vid, mainf, r1f, r2f = sys.argv[2:6]
        mid = upload_video(vid)
        t1 = post(open(mainf).read().strip(), media_ids=[mid])
        print("MAIN", t1)
        t2 = post(open(r1f).read().strip(), reply_to=t1)
        print("REPLY1", t2)
        t3 = post(open(r2f).read().strip(), reply_to=t2)
        print("REPLY2", t3)
        handle = whoami()  # derive from the authed account — never hardcode a handle
        url = f"https://x.com/{handle}/status/{t1}" if handle else f"https://x.com/i/status/{t1}"
        print("DONE " + url)
    else:
        print("unknown")
        sys.exit(1)
