"""Unit tests: the Connect page, its OS detection, and the Windows installer it points at.
Run: .venv/bin/python -m unittest tests.test_connect_page"""
import json
import re
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from server import connect_page
from server import console

PUBLIC = "https://rooms.example.org"
JS = connect_page.STATIC / "connect.js"
PS1 = connect_page.WINDOWS_INSTALLER


class Routes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        env = mock.patch.dict("os.environ", {"HAGENCY_PUBLIC_CONSOLE": PUBLIC})
        env.start()
        self.addCleanup(env.stop)
        app = web.Application()
        app.add_routes(connect_page.routes())
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_the_page_names_this_console_and_forbids_requests(self):
        r = await self.client.get(connect_page.PAGE_PATH)
        page = await r.text()
        self.assertEqual((r.status, r.content_type), (200, "text/html"))
        self.assertIn(f'<meta name="hagency-console" content="{PUBLIC}">', page)
        self.assertNotIn(connect_page.CONSOLE_MARK, page)
        csp = r.headers["Content-Security-Policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertEqual(re.findall(r"connect-src [^;]*", csp), ["connect-src 'self'"])   # the status, nowhere else
        self.assertNotIn("unsafe", csp)
        self.assertEqual((r.headers["Referrer-Policy"], r.headers["Cache-Control"]), ("no-referrer", "no-store"))
        self.assertIn('src="/connect/start.js"', page)
        self.assertIn('href="/connect/start.css"', page)

    async def test_the_request_never_shapes_the_page(self):
        """The code lives in the fragment; even a code sent by mistake in the query isn't echoed or used."""
        plain = await (await self.client.get(connect_page.PAGE_PATH)).text()
        with_query = await (await self.client.get(connect_page.PAGE_PATH + "?code=QRSTU-VWXYZ")).text()
        self.assertEqual(plain, with_query)
        self.assertNotIn("QRSTU-VWXYZ", with_query)
        r = await self.client.post(connect_page.PAGE_PATH, data="QRSTU-VWXYZ")
        self.assertEqual(r.status, 405)

    async def test_script_and_style_are_served_with_their_types(self):
        for path, kind in ((f"{connect_page.PAGE_PATH}.js", "application/javascript"),
                           (f"{connect_page.PAGE_PATH}.css", "text/css")):
            r = await self.client.get(path)
            self.assertEqual((r.status, r.content_type), (200, kind))

    async def test_windows_installer_names_this_console(self):
        r = await self.client.get("/connect.ps1")
        script = await r.text()
        self.assertEqual((r.status, r.content_type, r.charset), (200, "text/plain", "utf-8"))
        self.assertIn(f"'{PUBLIC}'", script)
        self.assertNotIn(connect_page.CONSOLE_MARK, script)
        self.assertEqual(r.headers["Cache-Control"], "no-store")

    async def test_console_serves_them_too(self):
        """panel.py mounts console.routes(); the Connect page comes with it."""
        paths = {(r.method, r.path) for r in console.routes(None, None)}
        self.assertTrue({("GET", "/connect/start"), ("GET", "/connect.ps1")} <= paths)

    def test_every_entry_point_still_imports(self):
        """connect_page and console use each other: whichever is imported first, nothing breaks."""
        for first in ("console", "connect_page", "remote_agent", "panel"):
            out = subprocess.run([sys.executable, "-c", f"from server import {first}; from server import console; console.routes(None, None)"],
                                 capture_output=True, text=True, timeout=60, cwd=connect_page.ROOT)
            self.assertEqual(out.returncode, 0, f"import {first}: {out.stderr[-500:]}")

    def test_page_link_puts_the_code_in_the_fragment(self):
        self.assertEqual(connect_page.page_link("ABCDE-FGHJK"), f"{PUBLIC}/connect/start#ABCDE-FGHJK")
        self.assertEqual(connect_page.page_link("ABCDE-FGHJK", "ollama"), f"{PUBLIC}/connect/start#ABCDE-FGHJK&kind=ollama")
        self.assertEqual(connect_page.page_link("ABCDE-FGHJK", "x&y"), f"{PUBLIC}/connect/start#ABCDE-FGHJK")


class Script(unittest.TestCase):
    def test_its_only_request_is_the_status_post_with_the_code_in_the_body(self):
        text = JS.read_text()
        self.assertEqual(re.findall(r"fetch\(([^,]*),", text), ["STATUS_PATH"])
        self.assertIn('const STATUS_PATH = "/connect/status";', text)
        self.assertIn('method: "POST"', text)
        self.assertIn("body: JSON.stringify({ code })", text)
        self.assertIn('credentials: "omit"', text)
        for word in ("XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource", ".src =", "location.href =", "innerHTML", "?code="):
            self.assertNotIn(word, text)

    def test_the_page_carries_the_kinds_as_data_and_a_link_to_pm(self):
        page = connect_page._page()
        raw = re.search(r'<script id="kinds" type="application/json">(.*?)</script>', page, re.S).group(1)
        self.assertEqual([k["kind"] for k in json.loads(raw)], list(connect_page.agent_kinds.KINDS))
        self.assertNotIn(connect_page.KINDS_MARK, page)
        self.assertIn(f'<meta name="hagency-pm" content="{connect_page.ELEMENT_ORIGIN}/#/user/@pm:', page)
        self.assertEqual(page.count("<script"), 2)                         # the data block and our own file


NODE = shutil.which("node")
CASES = [  # (navigator, expected)
    ({"userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
      "platform": "MacIntel", "maxTouchPoints": 0}, "mac"),
    ({"userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0 Safari/537.36", "platform": "Win32",
      "userAgentData": {"platform": "Windows", "mobile": False}}, "windows"),
    ({"userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
      "platform": "Win32"}, "windows"),
    ({"userAgent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/130.0 Safari/537.36", "platform": "Linux x86_64",
      "userAgentData": {"platform": "Linux", "mobile": False}}, "linux"),
    ({"userAgent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0",
      "platform": "Linux x86_64"}, "linux"),
    ({"userAgent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) Chrome/130.0 Mobile Safari/537.36",
      "platform": "Linux armv8l", "userAgentData": {"platform": "Android", "mobile": True}}, "other"),
    ({"userAgent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Safari/604.1", "platform": "iPhone",
      "maxTouchPoints": 5}, "other"),
    ({"userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15", "platform": "MacIntel",
      "maxTouchPoints": 5}, "other"),                              # an iPad asking for the desktop site
    ({"userAgent": "Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) Chrome/130.0 Safari/537.36", "platform": "Linux x86_64",
      "userAgentData": {"platform": "Chrome OS", "mobile": False}}, "other"),
    ({"userAgent": "", "platform": ""}, "other"),
]


@unittest.skipUnless(NODE, "node isn't installed: the page's OS detection is not exercised")
class ScriptUnderNode(unittest.TestCase):
    def run_js(self, expr: str, data) -> object:
        program = (f"const m = require({json.dumps(str(JS))}); const data = JSON.parse(process.argv[1]);"
                   f"process.stdout.write(JSON.stringify({expr}));")
        out = subprocess.run([NODE, "-e", program, json.dumps(data)], capture_output=True, text=True, timeout=30, check=True)
        return json.loads(out.stdout)

    def test_detects_each_os(self):
        got = self.run_js("data.map(([nav]) => m.detectOS(nav))", CASES)
        self.assertEqual(got, [want for _, want in CASES])

    def test_only_code_shaped_text_reaches_the_line(self):
        got = self.run_js("data.map(m.cleanCode)", ["#ABCDE-FGHJK", "abcde-fghjk", "%23ABCDE-FGHJK", "ABCDE-FGHJK;rm -rf ~",
                                                    "$(id)", "%E0%A4%A", "", "x" * 40])
        self.assertEqual(got, ["ABCDE-FGHJK", "ABCDE-FGHJK", "ABCDE-FGHJK", "", "", "", "", ""])

    def test_the_lines_people_paste(self):
        got = self.run_js("['mac', 'linux', 'windows'].map(os => m.commandFor(os, data, 'ABCDE-FGHJK'))", PUBLIC)
        self.assertEqual(got, [f"curl -fsSL {PUBLIC}/connect.sh | sh -s -- ABCDE-FGHJK"] * 2 +
                         [f"& ([scriptblock]::Create((irm -UseBasicParsing {PUBLIC}/connect.ps1))) -Code ABCDE-FGHJK"])
        kinded = self.run_js("['mac', 'windows'].map(os => m.commandFor(os, data, 'ABCDE-FGHJK', 'ollama'))", PUBLIC)
        self.assertEqual(kinded, [f"curl -fsSL {PUBLIC}/connect.sh | sh -s -- ABCDE-FGHJK --agent ollama",
                                  f"& ([scriptblock]::Create((irm -UseBasicParsing {PUBLIC}/connect.ps1))) -Code ABCDE-FGHJK -Agent ollama"])
        for line in got + kinded:                                   # copied whole: no prompt sign, no line break
            self.assertEqual(line, line.strip())
            self.assertFalse(line.startswith(("$", ">", "PS")))
            self.assertNotIn("\n", line)


class WindowsInstaller(unittest.TestCase):
    """Static checks on connect.ps1 (PowerShell itself parses it below when it's installed)."""
    def setUp(self):
        self.text = PS1.read_text()

    def test_plain_ascii(self):
        """Windows PowerShell 5.1 can misread non-ASCII in a downloaded script."""
        self.assertTrue(self.text.isascii())

    def test_the_code_is_checked_and_secrets_stay_off_command_lines(self):
        self.assertIn("[Parameter(Mandatory = $true)][string]$Code", self.text)
        self.assertIn("$Code -notmatch $CodePattern", self.text)
        self.assertNotRegex(self.text, r"(?i)password|Invoke-Expression")
        self.assertIn("--pair-only", self.text)                   # connect.py saves; the task runs the agent
        self.assertIn("connect_win.py", self.text)

    def test_never_closes_the_persons_window(self):
        """It runs inside their own PowerShell: `exit` would close it."""
        self.assertIsNone(re.search(r"(?im)^\s*exit\b", self.text))

    def test_one_sign_in_folder_for_the_script_and_both_python_entry_points(self):
        """GPT review: the folder the ACL protects is the one connect.py writes and the task reads."""
        self.assertIn("--home $HagencyHome $Code", self.text)
        self.assertIn("--home $HagencyHome --task-name", self.text)
        self.assertIn("'\"{0}\" --home \"{1}\"' -f $runner, $HagencyHome", self.text)

    def test_only_a_cli_the_bridge_can_start_is_picked(self):
        """GPT review: a .ps1 wrapper passes Get-Command but not the bridge's shell-less start. npm's
        .cmd wrapper is fine now: cli_path.py starts the program it wraps, without a shell."""
        self.assertIn('Get-Command "$Name.exe", "$Name.cmd" -CommandType Application', self.text)

    def test_the_folder_lock_needs_no_admin(self):
        """GPT review: Set-Acl also writes the audit list (admin only); SetAccessControl writes the DACL."""
        self.assertNotRegex(self.text, r"(?m)^[^#]*\bSet-Acl -")
        self.assertIn("SetAccessControl($acl)", self.text)

    def test_other_installations_entries_are_left_alone(self):
        """GPT review: ~/.hagency must not match ~/.hagency-test: the quoted folder is matched whole."""
        self.assertIn("""('--home "{0}"' -f $HagencyHome)""", self.text)

    def test_a_failed_rerun_starts_the_old_agent_again(self):
        """GPT review: an expired code or no network must not leave a working agent stopped."""
        self.assertIn("foreach ($task in $state.Stopped)", self.text)
        self.assertIn("foreach ($link in $state.Relaunch)", self.text)
        self.assertNotIn("$script:", self.text)                 # it runs in the person's own session

    def test_the_task_restarts_and_runs_as_the_person(self):
        for piece in ("New-ScheduledTaskTrigger -AtLogOn", "-RestartCount 255", "-RunLevel Limited", "-LogonType Interactive"):
            self.assertIn(piece, self.text)                       # 255: Task Scheduler's most restarts

    def test_python_output_cant_crash_on_a_cp1252_pc(self):
        """GPT review: connect.py prints a check mark after saving; piped on cp1252 that raised."""
        self.assertIn("$env:PYTHONIOENCODING = 'utf-8'", self.text)
        self.assertIn("$env:PYTHONIOENCODING = $savedEncoding", self.text)


PWSH = shutil.which("pwsh") or shutil.which("powershell")


@unittest.skipUnless(PWSH, "PowerShell isn't installed here: connect.ps1 is not parsed (only the static checks ran)")
class WindowsInstallerParses(unittest.TestCase):
    def test_no_syntax_errors(self):
        program = ("$errors = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                   f"'{PS1}', [ref]$null, [ref]$errors); $errors | ForEach-Object {{ $_.ToString() }}")
        out = subprocess.run([PWSH, "-NoProfile", "-Command", program], capture_output=True, text=True, timeout=60)
        self.assertEqual((out.returncode, out.stdout.strip()), (0, ""))


if __name__ == "__main__":
    unittest.main()
