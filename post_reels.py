# post_reel.py
import argparse
import hashlib
import hmac
import os
import sys
import time
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
IG_USER_ID = os.getenv("IG_USER_ID")
APP_SECRET = os.getenv("APP_SECRET")  # optional but recommended

if not ACCESS_TOKEN or not IG_USER_ID:
    print("Missing ACCESS_TOKEN or IG_USER_ID in .env")
    sys.exit(1)

GRAPH = "https://graph.facebook.com/v23.0"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "ig-reels-uploader/1.0"})

def appsecret_proof():
    """Generate appsecret_proof if APP_SECRET is present (recommended)."""
    if not APP_SECRET:
        return None
    dig = hmac.new(APP_SECRET.encode("utf-8"), ACCESS_TOKEN.encode("utf-8"), hashlib.sha256).hexdigest()
    return dig

def params_with_auth(extra=None):
    p = {"access_token": ACCESS_TOKEN}
    proof = appsecret_proof()
    if proof:
        p["appsecret_proof"] = proof
    if extra:
        p.update(extra)
    return p

def normalize_github_raw(url: str) -> str:
    """
    Convert GitHub 'web' raw URLs to true raw.githubusercontent.com form, which
    is more reliable for external fetchers like Instagram.
    Examples:
      https://github.com/user/repo/raw/refs/heads/main/file.mp4
      -> https://raw.githubusercontent.com/user/repo/main/file.mp4
    """
    if "github.com" in url and "/raw/" in url:
        parts = url.split("/")
        # [https:, '', github.com, user, repo, raw, refs, heads, branch, *path]
        try:
            user = parts[3]
            repo = parts[4]
            # handle .../raw/refs/heads/<branch>/...
            idx = parts.index("raw")
            if parts[idx+1:idx+3] == ["refs", "heads"]:
                branch = parts[idx+3]
                path = "/".join(parts[idx+4:])
            else:
                # handle .../raw/<branch>/...
                branch = parts[idx+1]
                path = "/".join(parts[idx+2:])
            return f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/{path}"
        except Exception:
            return url
    return url

def check_video_url_public(url: str):
    """
    Do a lightweight HEAD to ensure the URL is reachable and likely an MP4.
    IG requires a publicly accessible HTTPS URL that supports ranged downloads.
    """
    try:
        r = SESSION.head(url, allow_redirects=True, timeout=20)
    except Exception as e:
        return False, f"HEAD request failed: {e}"

    if r.status_code >= 400:
        return False, f"URL returned HTTP {r.status_code}"

    ctype = r.headers.get("Content-Type", "")
    # Allow 'video/mp4' or generic 'application/octet-stream' that GitHub may use
    if not ("video" in ctype or "octet-stream" in ctype):
        # Some hosts omit Content-Type on HEAD; try GET with stream to peek
        try:
            g = SESSION.get(url, stream=True, allow_redirects=True, timeout=20)
            g.raise_for_status()
            ctype = g.headers.get("Content-Type", "")
            g.close()
        except Exception as e:
            return False, f"Could not validate video content-type: {e}"
        if not ("video" in ctype or "octet-stream" in ctype):
            return False, f"Unexpected Content-Type: {ctype or 'N/A'}"
    return True, "OK"

def create_reel(video_url: str, caption: str):
    """Create a media container for a Reel via video_url ingestion."""
    url = f"{GRAPH}/{IG_USER_ID}/media"
    payload = {
        "media_type": "REELS",
        "caption": caption,
        "video_url": video_url,      # must be public HTTPS
        "share_to_feed": "true"
    }
    r = SESSION.post(url, data=params_with_auth(payload), timeout=120)
    return safe_json(r)

def get_status(creation_id: str):
    url = f"{GRAPH}/{creation_id}"
    params = {
        "fields": "status_code,status",  # no video_id here
        "access_token": ACCESS_TOKEN,
    }
    r = requests.get(url, params=params, timeout=60)
    return r.json()

def publish_reel(creation_id: str):
    """Publish the processed media as a Reel."""
    url = f"{GRAPH}/{IG_USER_ID}/media_publish"
    r = SESSION.post(url, data=params_with_auth({"creation_id": creation_id}), timeout=120)
    return safe_json(r)

def get_permalink(media_id: str):
    """Fetch a permalink to the published Reel (optional)."""
    url = f"{GRAPH}/{media_id}"
    r = SESSION.get(url, params=params_with_auth({"fields": "permalink"}), timeout=60)
    return safe_json(r)

def safe_json(resp: requests.Response):
    try:
        return resp.json()
    except Exception:
        return {"error": {"message": f"Non-JSON response ({resp.status_code}): {resp.text[:500]}"}, "http_status": resp.status_code}

def explain_error(prefix: str, data: dict):
    err = data.get("error") or {}
    msg = err.get("message") or str(data)
    code = err.get("code")
    sub = err.get("error_subcode")
    return f"{prefix}: {msg} (code={code}, subcode={sub})"

def main():
    parser = argparse.ArgumentParser(description="Post an Instagram Reel via API")
    parser.add_argument("--video-url", required=True, help="Public HTTPS .mp4 URL")
    parser.add_argument("--caption", default="Posted via API 🚀")
    args = parser.parse_args()

    # Normalize GitHub URLs to raw.githubusercontent.com (IG fetches more reliably)
    vid_url = normalize_github_raw(args.video_url)

    # Quick sanity check
    ok, why = check_video_url_public(vid_url)
    if not ok:
        print(f"❌ Video URL check failed: {why}")
        sys.exit(1)

    print("1) Creating media container...")
    creation = create_reel(vid_url, args.caption)
    print("Create response:", creation)
    if "error" in creation:
        print("❌", explain_error("Create failed", creation))
        sys.exit(1)

    creation_id = creation.get("id")
    if not creation_id:
        print("❌ No creation_id returned.")
        sys.exit(1)

    print("\n2) Waiting for processing to finish...")
    backoff = 5
    max_backoff = 60
    max_wait_sec = 15 * 60
    waited = 0
    while True:
        status = get_status(creation_id)
        print("Status:", status)
        if "error" in status:
            print("❌", explain_error("Status failed", status))
            sys.exit(1)

        code = str(status.get("status_code") or "").upper()
        if code == "FINISHED":
            break
        if code in {"ERROR", "ERROR_UPLOADING"}:
            print("❌ Processing failed in IG pipeline.")
            sys.exit(1)

        time.sleep(backoff)
        waited += backoff
        backoff = min(max_backoff, int(backoff * 1.5))
        if waited >= max_wait_sec:
            print("❌ Timed out waiting for processing.")
            sys.exit(1)

    print("\n3) Publishing Reel...")
    pub = publish_reel(creation_id)
    print("Publish response:", pub)
    if "error" in pub:
        print("❌", explain_error("Publish failed", pub))
        sys.exit(1)

    media_id = pub.get("id")
    if media_id:
        link = get_permalink(media_id)
        print("\n✅ Done!")
        print("Permalink response:", link)
    else:
        print("\n⚠️ Published, but no media_id returned. Check your account.")

if __name__ == "__main__":
    # If you want a hard default to your video, you can run:
    # python post_reel.py --video-url "https://github.com/osamaPY/chakchabani/raw/refs/heads/main/chakchabani.mp4"
    main()
