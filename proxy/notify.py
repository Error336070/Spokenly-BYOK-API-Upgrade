"""Windows toast notifications through PowerShell's WinRT bridge (no extra dependencies)."""
import base64
import logging
import subprocess
import sys
import time
from xml.sax.saxutils import escape

log = logging.getLogger("proxy")

# PowerShell's own AppUserModelID: lets toasts show without registering an app.
_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
_PS = """
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml('<toast><visual><binding template="ToastGeneric"><text>TITLE</text><text>BODY</text></binding></visual></toast>')
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('APPID').Show([Windows.UI.Notifications.ToastNotification]::new($x))
"""

_UPSTREAM = {
    401: ("Provider rejected the API key", "Check the API key in .env (route {route})."),
    402: ("OpenRouter credits exhausted", "Top up credits, or switch routes to a native backend in config.yaml."),
    429: ("Provider rate limit hit", "Route {route} is being rate limited; requests are failing."),
}


def send_toast(title: str, body: str) -> None:
    if sys.platform != "win32":
        return
    q = lambda s: escape(s).replace("'", "''")  # XML-escape, then escape for a PS single-quoted string
    script = _PS.replace("TITLE", q(title)).replace("BODY", q(body)).replace("APPID", _APP_ID)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                         creationflags=subprocess.CREATE_NO_WINDOW,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        log.warning("toast failed: %s", e)


class Notifier:
    """Decides when an error deserves a toast; one toast per kind per cooldown."""

    def __init__(self, cfg: dict, send=send_toast):
        self.cfg, self.send = cfg, send
        self.consecutive = 0
        self.last_sent: dict[str, float] = {}

    def _notify(self, kind: str, title: str, body: str) -> None:
        n = self.cfg.get("notifications") or {}
        if not n.get("enabled", True):
            return
        now = time.monotonic()
        if now - self.last_sent.get(kind, -1e9) < n.get("cooldown_s", 600):
            return
        self.last_sent[kind] = now
        log.info("toast: %s | %s", title, body)
        self.send(f"Dictation Proxy: {title}", body[:200])

    def success(self) -> None:
        self.consecutive = 0

    def error(self, route: str, message: str, upstream_status: int | None = None, backend: str = "") -> None:
        self.consecutive += 1
        if backend == "native" and upstream_status in (401, 402, 403):
            # free credits used up (or key revoked): say exactly how to switch back
            self._notify("native-credits", f"Native API rejected route '{route}'",
                         f"Free credits may be used up. In config.yaml set {route}: backend: openrouter "
                         f"(applies instantly). Provider said: {message}")
        elif upstream_status in _UPSTREAM:
            title, body = _UPSTREAM[upstream_status]
            self._notify(f"http{upstream_status}", title, body.format(route=route))
        elif self.consecutive >= (self.cfg.get("notifications") or {}).get("consecutive_errors", 3):
            self._notify("repeated", f"{self.consecutive} failed requests in a row", message)

    def background_error(self, name: str, message: str) -> None:
        self._notify(f"background-{name}", "Background task failed", f"{name}: {message}")

    def config_error(self, message: str) -> None:
        self._notify("config", "config.yaml has an error; keeping the previous config", message)
