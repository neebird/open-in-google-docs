"""Offline tests for to_gdocs against a fake Google Drive on 127.0.0.1.

The fake speaks plain HTTP to the real urllib/http.client stack, keeps file records with their
appProperties tags and answers each upload from a per-file script, so retries, stalls and
error messages are exercised end to end without touching Google.
Run: /usr/bin/python3 -m unittest discover -s tests -v
"""
import http.server
import io
import json
import os
import plistlib
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import to_gdocs as g  # noqa: E402

MIME_EXT = {mime: ext for ext, (mime, _) in g.SOURCE_TYPES.items()}
SHEET = g.EDIT_URLS[g.GOOGLE_SHEET]
DOC = g.EDIT_URLS[g.GOOGLE_DOC]
EN = g.TEXTS["en"]


class FakeDrive:
    """Answers uploads from `script` ("<name>.<ext>" -> actions) and tag searches from `records`.

    Actions: ok:ID | lost:ID (created, no answer) | lostexpire:ID (same, and the token expires) |
    stall (nothing, no answer) | reset | resetcreated:ID | cut200:ID (created, body cut) | cut401 |
    html502 | <code>:<reason>.
    Uploads into a folder listed in `deleted` get 404 without using an action; any request with
    a token listed in `expired` gets 401.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.clear()
        drive = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def reply(self, code, body, ctype="application/json", length=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body) if length is None else length))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def expired(self):
                if self.headers.get("Authorization", "")[len("Bearer "):] not in drive.expired:
                    return False
                self.reply(401, json.dumps({"error": {"code": 401, "message": "Invalid Credentials", "errors": [
                    {"reason": "authError", "message": "Invalid Credentials", "domain": "global"}]}}).encode())
                return True

            def do_GET(self):
                if self.expired():
                    return
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)["q"][0]
                tag = re.fullmatch(r"appProperties has \{ key='to_gdocs' and value='([0-9a-f]{32})' \}"
                                   r" and trashed = false", q).group(1)
                with drive.lock:
                    drive.queries.append(q)
                    hits = [r["id"] for r in drive.records if r["tag"] == tag]
                self.reply(200, json.dumps({"files": [{"id": i} for i in hits[:1]]}).encode())

            def do_POST(self):
                if self.path.startswith("/slowread"):
                    time.sleep(8)  # never reads the body: the client has to time out while sending
                    return
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.expired():
                    return
                head = body.split(b"\r\n\r\n", 2)[1]
                meta = json.loads(head.split(b"\r\n", 1)[0])
                mime = re.search(rb"Content-Type: ([^\r]+)$", head).group(1).decode()
                key = meta["name"] + "." + MIME_EXT[mime]
                parent, tag = meta["parents"][0], meta["appProperties"]["to_gdocs"]
                with drive.lock:
                    drive.uploads.append({"key": key, "parent": parent, "tag": tag, "type": meta["mimeType"],
                                          "length": self.headers["Content-Length"], "body": len(body),
                                          "chunked": self.headers.get("Transfer-Encoding")})
                    action = "404:notFound" if parent in drive.deleted else drive.script[key].pop(0)
                kind, _, arg = action.partition(":")
                if kind in ("ok", "lost", "lostexpire", "resetcreated", "cut200"):
                    with drive.lock:
                        drive.records.append({"id": arg, "tag": tag})
                if kind == "lostexpire":
                    with drive.lock:
                        drive.expired.add(self.headers["Authorization"][len("Bearer "):])
                if kind == "ok":
                    self.reply(200, json.dumps({"id": arg}).encode())
                elif kind in ("lost", "lostexpire", "stall"):
                    time.sleep(3)
                elif kind in ("reset", "resetcreated"):
                    self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\1\0\0\0\0\0\0\0")
                    self.connection.close()
                elif kind == "cut200":
                    self.reply(200, b'{"id": "' + arg.encode(), length=200)
                elif kind == "cut401":
                    self.reply(401, b'{"error": {"code": 401, "mess', length=300)
                elif kind == "html502":
                    self.reply(502, b"<html><body>Bad Gateway</body></html>", ctype="text/html")
                else:
                    message = "Bad Request" if arg == "badRequest" else arg
                    self.reply(int(kind), json.dumps({"error": {"code": int(kind), "message": message, "errors": [
                        {"reason": arg, "message": message, "domain": "global"}]}}).encode())

        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True
            request_queue_size = 64  # the default 5 resets parallel connects on macOS

            def handle_error(self, request, client_address):
                pass  # resets and stalls break handler threads on purpose

        self.server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def clear(self):
        self.script, self.records, self.queries, self.uploads = {}, [], [], []
        self.deleted, self.expired = set(), set()


DRIVE = None


def setUpModule():
    global DRIVE
    DRIVE = FakeDrive()


def tearDownModule():
    DRIVE.server.shutdown()
    DRIVE.server.server_close()


class FakeTo(unittest.TestCase):
    """Runs to_gdocs.main() against the fake Drive with English texts and no real side effects."""

    def setUp(self):
        DRIVE.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.events = []
        self.token_refreshes = 0
        self.folder = "FOLDER"
        self.folder_refreshes = 0
        self.login_fails = False
        test = self

        class Popen:
            def __init__(self, args, **kwargs):
                if args[0] == "open":
                    test.events.append(("open", args[1]))
                elif args[0] == "osascript":
                    test.events.append(("notify", args[-2], args[-1]))
                elif os.path.exists(args[0]):
                    test.events.append(("spawn", args))
                else:
                    raise FileNotFoundError(2, "No such file or directory", args[0])

        for name, value in [("UPLOAD", DRIVE.base + "/upload/drive/v3/files?uploadType=multipart&fields=id"),
                            ("DRIVE", DRIVE.base + "/drive/v3/files"), ("STALL_TIMEOUT", 1),
                            ("LOG", os.path.join(self.tmp.name, "log")), ("T", EN),
                            ("access_token", self.fake_token), ("folder_id", self.fake_folder)]:
            patcher = mock.patch.object(g, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(g.subprocess, "Popen", Popen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_token(self, force_refresh=False):
        if force_refresh:
            self.token_refreshes += 1
            if self.login_fails:
                raise g.LoginRequired()
        return "fresh" if "token" in DRIVE.expired else "token"  # like the cache: a new one once expired

    def fake_folder(self, token, refresh=False):
        if refresh:
            with DRIVE.lock:
                self.folder_refreshes += 1
                self.folder = "FOLDER%d" % (self.folder_refreshes + 1)
            time.sleep(0.2)  # widen the window for parallel 404s
        return self.folder

    def make(self, name, subfolder=""):
        folder = os.path.join(self.tmp.name, subfolder)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "wb") as f:
            f.write(os.urandom(2048))
        return path

    def run_main(self, files, script=None, deleted=()):
        DRIVE.script.update(script or {})
        DRIVE.deleted.update(deleted)
        g.main(files)
        if not os.path.exists(g.LOG):
            return ""
        with open(g.LOG, encoding="utf-8") as f:
            return f.read()

    def opened(self):
        return [e[1] for e in self.events if e[0] == "open"]

    def notes(self):
        return [e[1:] for e in self.events if e[0] == "notify"]


class HappyPath(FakeTo):
    def test_one_request_with_length_and_tag(self):
        self.run_main([self.make("Report.xlsx")], {"Report.xlsx": ["ok:A"]})
        self.assertEqual(self.opened(), [SHEET % "A"])
        [upload] = DRIVE.uploads
        self.assertEqual(upload["length"], str(upload["body"]))
        self.assertIsNone(upload["chunked"])
        self.assertRegex(upload["tag"], "^[0-9a-f]{32}$")
        self.assertEqual(DRIVE.queries, [])

    def test_documents_become_docs(self):
        self.run_main([self.make("Letter.docx")], {"Letter.docx": ["ok:D1"]})
        self.assertEqual(self.opened(), [DOC % "D1"])
        self.assertEqual(DRIVE.uploads[0]["type"], g.GOOGLE_DOC)

    def test_unsupported_and_missing_files_are_reported(self):
        os.makedirs(os.path.join(self.tmp.name, "folder.xlsx"))
        paths = [self.make("Budget.numbers"), self.make("notes.txt"), os.path.join(self.tmp.name, "gone.docx"),
                 os.path.join(self.tmp.name, "folder.xlsx")]
        self.run_main(paths)
        self.assertEqual(self.notes(), [(EN["unsupported"], os.path.basename(p)) for p in paths])
        self.assertEqual(DRIVE.uploads, [])


class Stalls(FakeTo):
    def test_lost_answer_is_found_by_tag_without_second_upload(self):
        log = self.run_main([self.make("Lost.xlsx")], {"Lost.xlsx": ["lost:L1"]})
        self.assertEqual(self.opened(), [SHEET % "L1"])
        self.assertEqual(len(DRIVE.uploads), 1)
        self.assertIn("found Lost.xlsx after timeout", log)

    def test_stall_without_file_retries_with_the_same_tag(self):
        log = self.run_main([self.make("Stuck.xlsx")], {"Stuck.xlsx": ["stall", "ok:R2"]})
        self.assertEqual(self.opened(), [SHEET % "R2"])
        self.assertEqual(len({u["tag"] for u in DRIVE.uploads}), 1)
        self.assertIn("retry Stuck.xlsx: timeout", log)

    def test_file_created_by_the_second_attempt_is_found(self):
        self.run_main([self.make("Stuck2.xlsx")], {"Stuck2.xlsx": ["stall", "lost:Y2"]})
        self.assertEqual(self.opened(), [SHEET % "Y2"])
        self.assertEqual(self.notes(), [])

    def test_two_stalls_say_google_did_not_respond(self):
        self.run_main([self.make("Stuck3.xlsx")], {"Stuck3.xlsx": ["stall", "stall"]})
        self.assertEqual(self.notes(), [(EN["no_answer"][0], "Stuck3.xlsx — " + EN["no_answer"][1])])
        self.assertEqual(self.opened(), [])

    def test_reset_retries_and_reset_after_create_is_found(self):
        self.run_main([self.make("Reset.xlsx"), self.make("Reset2.xlsx")],
                      {"Reset.xlsx": ["reset", "ok:R3"], "Reset2.xlsx": ["resetcreated:R4"]})
        self.assertEqual(sorted(self.opened()), sorted([SHEET % "R3", SHEET % "R4"]))

    def test_cut_success_body_is_found(self):
        log = self.run_main([self.make("Cut.xlsx")], {"Cut.xlsx": ["cut200:C2"]})
        self.assertEqual(self.opened(), [SHEET % "C2"])
        self.assertIn("IncompleteRead", log)

    def test_server_errors_retry(self):
        log = self.run_main([self.make("Five.xlsx"), self.make("Gateway.xlsx")],
                            {"Five.xlsx": ["500:backendError", "ok:R5"], "Gateway.xlsx": ["html502", "html502"]})
        self.assertEqual(self.opened(), [SHEET % "R5"])
        self.assertIn("retry Five.xlsx: HTTP 500 backendError: backendError", log)
        self.assertIn("error Gateway.xlsx: HTTP 502: Bad Gateway", log)
        self.assertIn((EN["no_answer"][0], "Gateway.xlsx — " + EN["no_answer"][1]), self.notes())

    def test_token_that_expires_during_a_stall_is_renewed_before_the_check(self):
        self.run_main([self.make("Long.xlsx")], {"Long.xlsx": ["lostexpire:E1"]})
        self.assertEqual(self.opened(), [SHEET % "E1"])
        self.assertEqual(len(DRIVE.uploads), 1)

    def test_same_named_files_keep_their_own_documents(self):
        files = [self.make("Report.xlsx"), self.make("Report.csv"), self.make("Price.xlsx", "a"),
                 self.make("Price.xlsx", "b")]
        self.run_main(files, {"Report.xlsx": ["stall", "ok:X2"], "Report.csv": ["ok:C1"],
                              "Price.xlsx": ["ok:P1", "stall", "ok:P3"]})
        self.assertEqual(len(self.opened()), 4)
        self.assertEqual(len(set(self.opened())), 4)


class AuthAndFolder(FakeTo):
    def test_401_refreshes_the_token_once(self):
        self.run_main([self.make("Token.xlsx"), self.make("Token2.xlsx")],
                      {"Token.xlsx": ["401:authError", "ok:T1"], "Token2.xlsx": ["cut401", "ok:T2"]})
        self.assertEqual(sorted(self.opened()), sorted([SHEET % "T1", SHEET % "T2"]))
        self.assertEqual(self.token_refreshes, 2)
        self.assertEqual(DRIVE.queries, [])

    def test_deleted_folder_is_looked_up_once_for_all_threads(self):
        files = [self.make("Folder%d.xlsx" % i) for i in range(5)]
        self.run_main(files, {"Folder%d.xlsx" % i: ["ok:F%d" % i] for i in range(5)}, deleted={"FOLDER"})
        self.assertEqual(len(self.opened()), 5)
        self.assertEqual(self.folder_refreshes, 1)

    def test_revoked_grant_inside_a_worker_asks_to_sign_in(self):
        self.login_fails = True
        self.run_main([self.make("Login.xlsx")], {"Login.xlsx": ["401:authError"]})
        self.assertEqual(self.notes(), [(EN["login_again"][0], "Login.xlsx — " + EN["login_again"][1])])

    def test_revoked_grant_before_upload_starts_rclone_reconnect(self):
        with mock.patch.object(g, "access_token", side_effect=g.LoginRequired()), \
                mock.patch.object(g, "RCLONE", sys.executable):
            self.run_main([self.make("A.xlsx")])
        self.assertEqual([e[1][1:4] for e in self.events if e[0] == "spawn"], [["config", "reconnect", "gdrive:"]])
        self.assertEqual(self.notes(), [EN["login_now"]])

    def test_missing_rclone_is_reported(self):
        with mock.patch.object(g, "access_token", side_effect=g.LoginRequired()), \
                mock.patch.object(g, "RCLONE", "/nonexistent/rclone"):
            log = self.run_main([self.make("A.xlsx")])
        self.assertEqual(self.notes(), [EN["no_rclone"]])
        self.assertIn("cannot start rclone", log)

    def test_not_configured_is_reported(self):
        with mock.patch.object(g, "access_token", side_effect=g.NotConfigured("no remote")):
            self.run_main([self.make("A.xlsx")])
        self.assertEqual(self.notes(), [EN["not_configured"]])


class ErrorMessages(FakeTo):
    def test_definite_errors_are_explained_without_retry(self):
        cases = [("400:badRequest", "password"), ("400:conversionUnsupportedConversionPath", "damaged"),
                 ("413:uploadTooLarge", "too_large"), ("403:storageQuotaExceeded", "drive_full"),
                 ("429:rateLimitExceeded", "rate_limited"), ("403:userRateLimitExceeded", "rate_limited")]
        for action, key in cases:
            with self.subTest(action=action):
                DRIVE.clear()
                self.events.clear()
                log = self.run_main([self.make("Bad.xlsx")], {"Bad.xlsx": [action]})
                title, hint = EN[key]
                self.assertEqual(self.notes(), [(title, "Bad.xlsx" + (" — " + hint if hint else ""))])
                self.assertEqual(len(DRIVE.uploads), 1)
                self.assertIn("HTTP %s %s:" % tuple(action.split(":")), log)

    def test_unknown_error_falls_back_to_the_log_hint(self):
        log = self.run_main([self.make("Teapot.xlsx")], {"Teapot.xlsx": ["418:teapot"]})
        self.assertEqual(self.notes(), [(EN["upload_failed"], "Teapot.xlsx")])
        self.assertIn("HTTP 418 teapot: teapot", log)

    def test_offline_at_upload(self):
        with mock.patch.object(g, "UPLOAD", "http://drive.invalid/upload"):
            self.run_main([self.make("Offline.xlsx")])
        self.assertEqual(self.notes(), [(EN["offline"][0], "Offline.xlsx")])

    def test_offline_before_upload(self):
        dns_error = urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided, or not known"))
        with mock.patch.object(g, "access_token", side_effect=dns_error):
            self.run_main([self.make("Offline.xlsx")])
        self.assertEqual(self.notes(), [(EN["offline"][0], EN["drive_down"][1])])


class Units(unittest.TestCase):
    def test_stall_classification(self):
        for e in [socket.timeout(), TimeoutError(60, "x"), urllib.error.URLError(TimeoutError(60, "x")),
                  urllib.error.URLError(socket.timeout()), ConnectionResetError(), BrokenPipeError(),
                  g.http.client.IncompleteRead(b"x", 10),
                  urllib.error.HTTPError("u", 503, "Service Unavailable", {}, io.BytesIO())]:
            with self.subTest(e=e):
                self.assertTrue(g.stalled(e))
        for e in [urllib.error.URLError(socket.gaierror(8, "x")),
                  urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO())]:
            with self.subTest(e=e):
                self.assertFalse(g.stalled(e))

    def test_offline_classification(self):
        self.assertTrue(g.offline(urllib.error.URLError(socket.gaierror(8, "x"))))
        self.assertTrue(g.offline(urllib.error.URLError(OSError(51, "Network is unreachable"))))
        self.assertFalse(g.offline(urllib.error.URLError(socket.timeout())))

    def test_detail_formats(self):
        e = urllib.error.HTTPError("u", 502, "Bad Gateway", {}, io.BytesIO())
        e.drive = [], "Bad Gateway"
        self.assertEqual(g.detail(e), "HTTP 502: Bad Gateway")
        e.drive = ["badRequest"], "Bad Request"
        self.assertEqual(g.detail(e), "HTTP 502 badRequest: Bad Request")

    def test_upload_timeout_grows_with_size(self):
        with tempfile.NamedTemporaryFile(suffix=".xlsx") as f:
            f.write(os.urandom(2**20))
            f.flush()
            with mock.patch.object(g, "call", return_value={"id": "Z"}) as call:
                g.upload(f.name, "token", "FOLDER", "0" * 32)
        self.assertAlmostEqual(call.call_args[0][5], g.STALL_TIMEOUT + 4.0, places=2)

    def test_folder_name_is_escaped_in_the_query(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(g, "FOLDER_CACHE", os.path.join(tmp, "folder_id")), \
                mock.patch.object(g, "CACHE_DIR", tmp), \
                mock.patch.object(g, "FOLDER_NAME", "Ann's \\ folder"), \
                mock.patch.object(g, "call", return_value={"files": [{"id": "F"}]}) as call:
            self.assertEqual(g.folder_id("token"), "F")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(call.call_args[0][1]).query)["q"][0]
        self.assertIn("name = 'Ann\\'s \\\\ folder'", query)

    def test_access_token_needs_a_configured_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "rclone.conf")
            for text in ["", "[gdrive]\ntype = drive\n", "[gdrive]\nclient_id =\nclient_secret = s\ntoken = {}\n"]:
                with self.subTest(conf=text), open(conf, "w") as f:
                    f.write(text)
                with mock.patch.object(g, "RCLONE_CONF", conf), \
                        mock.patch.object(g, "TOKEN_CACHE", os.path.join(tmp, "none.json")):
                    self.assertRaises(g.NotConfigured, g.access_token)

    def test_revoked_refresh_token_clears_the_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf, cache = os.path.join(tmp, "rclone.conf"), os.path.join(tmp, "token.json")
            with open(conf, "w") as f:
                f.write('[gdrive]\nclient_id = id\nclient_secret = secret\ntoken = {"refresh_token": "r"}\n')
            with open(cache, "w") as f:
                json.dump({"access_token": "old", "expires_at": time.time() + 1800}, f)
            revoked = urllib.error.HTTPError("https://oauth2.googleapis.com/token", 400, "Bad Request", {},
                                             io.BytesIO(b'{"error": "invalid_grant"}'))
            with mock.patch.object(g, "RCLONE_CONF", conf), mock.patch.object(g, "TOKEN_CACHE", cache), \
                    mock.patch.object(g.urllib.request, "urlopen", side_effect=revoked):
                self.assertRaises(g.LoginRequired, g.access_token, force_refresh=True)
            self.assertFalse(os.path.exists(cache))

    def test_oauth_client_errors_mean_not_configured_and_keep_googles_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "rclone.conf")
            with open(conf, "w") as f:
                f.write('[gdrive]\nclient_id = id\nclient_secret = secret\ntoken = {"refresh_token": "r"}\n')

            def answer(code, body):
                return urllib.error.HTTPError("https://oauth2.googleapis.com/token", code, "Error", {},
                                              io.BytesIO(json.dumps(body).encode()))

            deleted = answer(401, {"error": "deleted_client", "error_description": "The OAuth client was deleted."})
            other = answer(400, {"error": "invalid_request", "error_description": "Missing parameter."})
            with mock.patch.object(g, "RCLONE_CONF", conf), \
                    mock.patch.object(g, "TOKEN_CACHE", os.path.join(tmp, "token.json")):
                with mock.patch.object(g.urllib.request, "urlopen", side_effect=deleted), \
                        self.assertRaises(g.NotConfigured) as raised:
                    g.access_token(force_refresh=True)
                self.assertIn("HTTP 401 deleted_client: The OAuth client was deleted.", str(raised.exception))
                with mock.patch.object(g.urllib.request, "urlopen", side_effect=other), \
                        self.assertRaises(urllib.error.HTTPError) as raised:
                    g.access_token(force_refresh=True)
                self.assertEqual(g.detail(raised.exception), "HTTP 400 invalid_request: Missing parameter.")

    def test_translations_are_complete(self):
        for language, texts in g.TEXTS.items():
            with self.subTest(language=language):
                self.assertEqual(set(texts), set(EN))
                for key, value in texts.items():
                    self.assertIs(type(value), type(EN[key]), key)
                    if isinstance(value, tuple):
                        self.assertEqual(len(value), 2, key)
                        self.assertEqual(bool(value[1]), bool(EN[key][1]), key)

    def test_system_language_picks_the_first_translated_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs = os.path.join(tmp, "prefs.plist")
            for languages, expected in [(["uk-UA", "en-US"], "uk"), (["de-DE", "ru-RU"], "ru"),
                                        (["de-DE"], "en"), ([], "en")]:
                with self.subTest(languages=languages):
                    with open(prefs, "wb") as f:
                        plistlib.dump({"AppleLanguages": languages}, f, fmt=plistlib.FMT_BINARY)
                    self.assertEqual(g.system_language(prefs), expected)
            self.assertEqual(g.system_language(os.path.join(tmp, "missing.plist")), "en")


class SlowNetwork(FakeTo):
    def test_stall_while_sending_counts_as_stalled(self):
        path = self.make("Big.xlsx")
        with open(path, "wb") as f:
            f.write(os.urandom(2**20))
        with mock.patch.object(g, "UPLOAD", DRIVE.base + "/slowread"):
            with self.assertRaises(OSError) as raised:
                g.upload(path, "token", "FOLDER", "0" * 32)
        self.assertTrue(g.stalled(raised.exception))


if __name__ == "__main__":
    unittest.main()
