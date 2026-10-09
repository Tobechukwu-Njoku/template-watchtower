#!/usr/bin/env python3
"""team.py token against a local stand-in for GitHub's App API, which checks the
token request's signature with the App's public key. No network."""
import base64
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "team"))
import team as T  # noqa: E402

REPO = T.GH._detect()
REVIEWERS, TIM = T.CFG["apps"]["reviewers"]["slug"], T.CFG["apps"]["implementer"]["slug"]
tmp = Path(tempfile.mkdtemp())
keys = tmp / "apps"
keys.mkdir()
subprocess.run(["openssl", "genrsa", "-out", str(keys / f"{REVIEWERS}.pem"), "2048"], capture_output=True, check=True)
subprocess.run(["openssl", "rsa", "-in", str(keys / f"{REVIEWERS}.pem"), "-pubout", "-out", str(tmp / "pub.pem")],
               capture_output=True, check=True)
(keys / f"{REVIEWERS}.json").write_text(json.dumps({"id": 7, "slug": REVIEWERS, "client_id": "Iv1.rev"}))
calls = []
unb = lambda x: base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))  # noqa: E731


class API(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _signed(self):
        h, b, s = self.headers["Authorization"].split()[1].split(".")
        (tmp / "sig").write_bytes(unb(s))
        ok = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(tmp / "pub.pem"), "-signature", str(tmp / "sig")],
                            input=f"{h}.{b}".encode(), capture_output=True).returncode == 0
        return ok and json.loads(unb(b))["iss"] == "Iv1.rev"

    def _reply(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def do_GET(self):
        calls.append(("GET", self.path, self._signed()))
        self._reply(200, {"id": 42}) if self.path == f"/repos/{REPO}/installation" else self._reply(404, {"message": "Not Found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        calls.append(("POST", self.path, self._signed(), body))
        self._reply(201, {"token": "ghs_fake_installation_token"})


srv = http.server.HTTPServer(("127.0.0.1", 0), API)
threading.Thread(target=srv.serve_forever, daemon=True).start()
env = dict(os.environ, WATCHTOWER_HOME=str(tmp), GITHUB_API_URL=f"http://127.0.0.1:{srv.server_port}")
env.pop("GITHUB_REPOSITORY", None)
env.pop("WATCHTOWER_AGENT", None)


def run(*args, **extra):
    return subprocess.run([sys.executable, str(ROOT / "team/team.py"), *args], capture_output=True, text=True,
                          env=dict(env, **extra))


fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {detail}"))
    fails += 0 if cond else 1


r = run("token", "--as", "victor-stone")
check("a reviewer gets the token for the reviewers' App", r.returncode == 0 and r.stdout.strip() == "ghs_fake_installation_token", r.stderr)
check("the request is signed with the App's key", calls[:1] == [("GET", f"/repos/{REPO}/installation", True)], calls[:1])
check("the token is limited to this repository", calls[1][:3] == ("POST", "/app/installations/42/access_tokens", True)
      and calls[1][3] == {"repositories": [REPO.split("/", 1)[1]]}, calls[1:2])
r = run("token", "--as", "tim-drake")
check("missing credentials are reported", r.returncode != 0 and f"no credentials for {TIM}" in r.stderr, r.stderr)
r = run("token", "--as", "james-gordon")
check("the gate has no App", r.returncode != 0 and "GitHub Actions" in r.stderr, r.stderr)
r = run("token", "--as", "victor-stone", WATCHTOWER_AGENT="")
check("refused inside an agent, even with the marker set to nothing", r.returncode != 0 and "not for a running agent" in r.stderr, r.stderr)
r = run("token", "--as", "barbara-gordon", GITHUB_REPOSITORY="someone/elsewhere")
check("an App not installed on the repo is reported", r.returncode != 0 and "not installed on someone/elsewhere" in r.stderr, r.stderr)
sys.exit(1 if fails else 0)
