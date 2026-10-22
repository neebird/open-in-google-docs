"""Uploads documents and spreadsheets to Google Drive as Google Docs / Google Sheets
and opens them in the browser.

Runs from the Finder Quick Actions "Open in Google Docs" and "Open in Google Sheets": one
Drive API request per file (upload + conversion), an access token cached for an hour and
the target folder ID cached on disk. Authorization comes from an rclone remote ("gdrive");
rclone itself is only used to sign in again when Google revokes the refresh token.
Python 3.9 stdlib only (runs on the system /usr/bin/python3).
"""
import configparser
import errno
import hashlib
import http.client
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

REMOTE = "gdrive"  # rclone remote with your own Google OAuth client
FOLDER_NAME = "From Mac"  # Drive folder for the converted files
# Quick Actions run with a minimal PATH, so look where Homebrew (Apple silicon, Intel) and MacPorts put rclone.
RCLONE = next((p for p in ("/opt/homebrew/bin/rclone", "/usr/local/bin/rclone", "/opt/local/bin/rclone")
               if os.path.exists(p)), "rclone")
# rclone's config file: the usual place, or the legacy one if only that exists
RCLONE_CONF = next((p for p in map(os.path.expanduser, ("~/.config/rclone/rclone.conf", "~/.rclone.conf"))
                    if os.path.exists(p)), os.path.expanduser("~/.config/rclone/rclone.conf"))
CACHE_DIR = os.path.expanduser("~/Library/Caches/to_gdocs")
TOKEN_CACHE = os.path.join(CACHE_DIR, "token.json")
FOLDER_CACHE = os.path.join(CACHE_DIR, "folder_id")
LOG = os.path.expanduser("~/Library/Logs/to_gdocs.log")

GOOGLE_DOC = "application/vnd.google-apps.document"
GOOGLE_SHEET = "application/vnd.google-apps.spreadsheet"
GOOGLE_FOLDER = "application/vnd.google-apps.folder"
# extension -> (MIME type of the file, Google type Drive converts it to)
SOURCE_TYPES = {
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", GOOGLE_DOC),
    "doc": ("application/msword", GOOGLE_DOC),
    "odt": ("application/vnd.oasis.opendocument.text", GOOGLE_DOC),
    "rtf": ("application/rtf", GOOGLE_DOC),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", GOOGLE_SHEET),
    "xlsm": ("application/vnd.ms-excel.sheet.macroenabled.12", GOOGLE_SHEET),
    "xls": ("application/vnd.ms-excel", GOOGLE_SHEET),
    "ods": ("application/vnd.oasis.opendocument.spreadsheet", GOOGLE_SHEET),
    "csv": ("text/csv", GOOGLE_SHEET),
    "tsv": ("text/tab-separated-values", GOOGLE_SHEET),
}
EDIT_URLS = {
    GOOGLE_DOC: "https://docs.google.com/document/d/%s/edit",
    GOOGLE_SHEET: "https://docs.google.com/spreadsheets/d/%s/edit",
}
DRIVE = "https://www.googleapis.com/drive/v3/files"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id"
STALL_TIMEOUT = 30  # seconds of silence from Drive before an upload counts as stuck (+4 s per MB)
FOLDER_LOCK = threading.Lock()  # one re-lookup of a deleted target folder for all upload threads

# Notification texts: (title, text) pairs; error hints follow the file name in the text.
T = {
    "unsupported": "This file type isn't supported",
    "not_configured": ("Google Drive isn't set up yet", "Set up the rclone remote “gdrive” — see the README"),
    "no_rclone": ("rclone isn't installed", "Install it with: brew install rclone"),
    "login_now": ("Sign in to Google again", "Opening the sign-in page — click “Allow”, then try again"),
    "login_again": ("Sign in to Google again", "run it once more to open the sign-in page"),
    "drive_down": ("Error — see Library/Logs/to_gdocs.log", "Google Drive is unavailable"),
    "upload_failed": "Upload failed — see Library/Logs/to_gdocs.log",
    "password": ("Google couldn't open the file", "it may be password-protected"),
    "damaged": ("Google couldn't read the file", "it's empty or damaged"),
    "too_large": ("The file is too large for Google", ""),
    "drive_full": ("Your Google Drive is full", ""),
    "rate_limited": ("Google is limiting uploads", "try again in a minute"),
    "no_answer": ("Google didn't respond", "try again"),
    "offline": ("No internet connection", ""),
}


class LoginRequired(Exception):
    pass


class NotConfigured(Exception):
    pass


def log(message):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")


def notify(title, text):
    subprocess.Popen(
        ["osascript", "-e", "on run argv",
         "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
         "-e", "end run", "--", title, text],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def write_private(path, text):
    os.makedirs(CACHE_DIR, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def access_token(force_refresh=False):
    conf = configparser.ConfigParser(interpolation=None)
    try:
        conf.read(RCLONE_CONF)
        remote = conf[REMOTE] if conf.has_section(REMOTE) else {}
        client_id, client_secret = remote.get("client_id"), remote.get("client_secret")
        refresh_token = json.loads(remote.get("token") or "{}").get("refresh_token")
    except (configparser.Error, ValueError, AttributeError) as e:
        # Only the error type: rclone.conf lines hold the client secret, and the log may get shared.
        raise NotConfigured("%s: unreadable (%s), e.g. a password-encrypted config" % (RCLONE_CONF, type(e).__name__))
    if not (client_id and client_secret and refresh_token):
        raise NotConfigured("%s: remote [%s] needs client_id, client_secret and token" % (RCLONE_CONF, REMOTE))
    # The cached token is only good for the account it was issued for: the remote may have been re-linked.
    account = hashlib.sha256((client_id + str(refresh_token)).encode()).hexdigest()[:16]
    if not force_refresh:
        try:
            with open(TOKEN_CACHE) as f:
                cached = json.load(f)
            if cached["account"] == account and cached["expires_at"] > time.time() + 60:
                return cached["access_token"]
        except (OSError, ValueError, KeyError, TypeError):
            pass

    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    try:
        with urllib.request.urlopen("https://oauth2.googleapis.com/token", body, timeout=30) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        answer = e.read()
        try:  # Google's reason for the log, the way call() keeps Drive's
            error = json.loads(answer)
            e.drive = [str(error["error"])], str(error.get("error_description", ""))
        except Exception:
            e.drive = [], e.msg
        if b"invalid_grant" in answer:
            try:
                os.remove(TOKEN_CACHE)  # so the next run goes through the login flow in main()
            except OSError:
                pass
            raise LoginRequired()
        if e.drive[0] and e.drive[0][0].endswith("_client"):  # deleted, disabled or mistyped OAuth client
            raise NotConfigured("Google rejected the OAuth client: %s" % detail(e))
        raise
    write_private(TOKEN_CACHE, json.dumps({
        "account": account,
        "access_token": data["access_token"],
        "expires_at": time.time() + data.get("expires_in", 3600),
    }))
    return data["access_token"]


def call(method, url, token, body=None, content_type=None, timeout=120):
    headers = {"Authorization": "Bearer " + token}
    if content_type:
        headers["Content-Type"] = content_type
    if body is not None:
        # Sent in blocks, so the timeout catches a stall instead of capping the whole upload.
        headers["Content-Length"] = str(len(body))
        body = io.BytesIO(body)
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            error = json.load(e)["error"]
            e.drive = [x.get("reason", "") for x in error.get("errors", [])], error.get("message", "")
        except Exception:  # best effort: never replace the HTTP error itself
            e.drive = [], e.msg
        raise


def folder_id(token, refresh=False):
    if not refresh:
        try:
            with open(FOLDER_CACHE) as f:
                cached = f.read().strip()
            if cached:
                return cached
        except OSError:
            pass
    name = FOLDER_NAME
    query = "name = '%s' and mimeType = '%s' and 'root' in parents and trashed = false" % (
        name.replace("\\", "\\\\").replace("'", "\\'"), GOOGLE_FOLDER)
    found = call("GET", DRIVE + "?" + urllib.parse.urlencode(
        {"q": query, "fields": "files(id)", "pageSize": 1}), token, timeout=STALL_TIMEOUT)["files"]
    if found:
        fid = found[0]["id"]
    else:
        fid = call("POST", DRIVE + "?fields=id", token,
                   json.dumps({"name": name, "mimeType": GOOGLE_FOLDER}).encode(),
                   "application/json", STALL_TIMEOUT)["id"]
    write_private(FOLDER_CACHE, fid)
    return fid


def upload(path, token, parent, tag):
    """Returns the edit URL of the Google Doc / Sheet created from the file."""
    base, ext = os.path.splitext(os.path.basename(path))
    source_type, google_type = SOURCE_TYPES[ext[1:].lower()]
    boundary = uuid.uuid4().hex.encode()
    metadata = json.dumps({"name": base, "mimeType": google_type, "parents": [parent],
                           "appProperties": {"to_gdocs": tag}}).encode()
    with open(path, "rb") as f:
        content = f.read()
    body = b"".join([
        b"--", boundary, b"\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n", metadata, b"\r\n",
        b"--", boundary, b"\r\nContent-Type: ", source_type.encode(), b"\r\n\r\n",
        content, b"\r\n--", boundary, b"--\r\n",
    ])
    # Drive answers only after converting, which takes longer for bigger files.
    timeout = STALL_TIMEOUT + 4 * len(content) / 2**20
    doc_id = call("POST", UPLOAD, token, body, "multipart/related; boundary=" + boundary.decode(), timeout)["id"]
    return EDIT_URLS[google_type] % doc_id


def find_upload(path, token, tag):
    """Edit URL of the Google file an upload tagged `tag` created although Drive never answered,
    or None (also when Drive can't be asked)."""
    google_type = SOURCE_TYPES[os.path.splitext(path)[1][1:].lower()][1]
    query = "appProperties has { key='to_gdocs' and value='%s' } and trashed = false" % tag
    try:
        found = call("GET", DRIVE + "?" + urllib.parse.urlencode(
            {"q": query, "fields": "files(id)", "pageSize": 1}), token, timeout=STALL_TIMEOUT)["files"]
    except (OSError, http.client.HTTPException, ValueError):
        return None
    return EDIT_URLS[google_type] % found[0]["id"] if found else None


def stalled(e):
    """Drive went silent, dropped the connection or failed on its side: the file may or may not exist."""
    if isinstance(e, urllib.error.HTTPError):
        return e.code >= 500
    reason = e.reason if isinstance(e, urllib.error.URLError) else e
    return isinstance(reason, (socket.timeout, TimeoutError, ConnectionResetError, ConnectionAbortedError,
                               BrokenPipeError, http.client.IncompleteRead))


def offline(e):
    reason = getattr(e, "reason", None)
    return isinstance(e, urllib.error.URLError) and (
        isinstance(reason, socket.gaierror)
        or getattr(reason, "errno", None) in (errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH))


def explain(e, fallback):
    """Notification title for an error, plus a hint for its text."""
    if isinstance(e, LoginRequired):
        return T["login_again"]
    if isinstance(e, NotConfigured):
        return T["not_configured"]
    reasons = getattr(e, "drive", ([], ""))[0]
    if "badRequest" in reasons:
        return T["password"]
    if "conversionUnsupportedConversionPath" in reasons:
        return T["damaged"]
    if getattr(e, "code", None) == 413:
        return T["too_large"]
    if "storageQuotaExceeded" in reasons:
        return T["drive_full"]
    if getattr(e, "code", None) == 429 or "userRateLimitExceeded" in reasons or "rateLimitExceeded" in reasons:
        return T["rate_limited"]
    if stalled(e):
        return T["no_answer"]
    if offline(e):
        return T["offline"]
    return fallback, ""


def detail(e):
    """The error for the log, with Drive's reason and message when it sent them."""
    if hasattr(e, "drive"):
        reasons, message = e.drive
        return "HTTP %d%s: %s" % (e.code, " " + ",".join(reasons) if reasons else "", message)
    return repr(e)


def convert(path, state):
    """Uploads one file, retrying once on an expired token, a deleted target folder or a Drive
    that went silent (unless the silent upload turns out to have created the file after all)."""
    name = os.path.basename(path)
    started = time.time()
    tag = uuid.uuid4().hex  # marks both attempts, so a silent one can be found by it
    for attempt in (1, 2):
        folder = state["folder"]
        try:
            url = upload(path, state["token"], folder, tag)
            break
        except (OSError, http.client.HTTPException) as e:  # HTTP errors, timeouts, dropped connections
            url = None
            if stalled(e):
                try:
                    state["token"] = access_token()  # a long silence can outlast the cached token
                except (OSError, http.client.HTTPException, ValueError, KeyError):
                    pass  # keep the old token: a failed refresh must not hide the real error
                url = find_upload(path, state["token"], tag)
            if url:
                log("found %s after %s" % (name, detail(e)))
                break
            if attempt == 2:
                raise
            code = getattr(e, "code", None)
            if code == 401:
                state["token"] = access_token(force_refresh=True)
            elif code == 404:
                with FOLDER_LOCK:
                    if state["folder"] == folder:  # no other thread has looked it up again yet
                        state["folder"] = folder_id(state["token"], refresh=True)
            elif stalled(e):
                log("retry %s: %s" % (name, detail(e)))
            else:
                raise
    subprocess.Popen(["open", url])
    log("ok %.1fs  %s -> %s" % (time.time() - started, name, url))


def main(paths):
    files = [p for p in paths if os.path.isfile(p)
             and os.path.splitext(p)[1][1:].lower() in SOURCE_TYPES]
    for p in paths:
        if p not in files:
            notify(T["unsupported"], os.path.basename(p))
    if not files:
        return

    try:
        token = access_token()
        state = {"token": token, "folder": folder_id(token)}
    except LoginRequired:
        log("login required: refresh token rejected, starting rclone reconnect")
        try:
            subprocess.Popen([RCLONE, "config", "reconnect", REMOTE + ":", "--auto-confirm"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            log("error: cannot start rclone: %r" % e)
            notify(*T["no_rclone"])
            return
        notify(*T["login_now"])
        return
    except NotConfigured as e:
        log("not configured: %s" % e)
        notify(*T["not_configured"])
        return
    except Exception as e:
        log("error before upload: %s" % detail(e))
        notify(explain(e, T["drive_down"][0])[0], T["drive_down"][1])
        return

    def run(path):
        name = os.path.basename(path)
        try:
            convert(path, state)
        except Exception as e:
            log("error %s: %s" % (name, detail(e)))
            title, hint = explain(e, T["upload_failed"])
            notify(title, name + (" — " + hint if hint else ""))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(run, files))


if __name__ == "__main__":
    main(sys.argv[1:])
