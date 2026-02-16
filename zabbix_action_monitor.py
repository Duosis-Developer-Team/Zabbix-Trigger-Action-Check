#!/usr/bin/env python3
"""
Zabbix Trigger Action Monitor
==============================
Zabbix 6.4 trigger action'larını periyodik olarak kontrol eder.
Condition'u boş olan action'ları disable eder ve e-posta ile bildirir.

AWX/Ansible Tower uyumlu çıktı ve exit code desteği:
  Exit 0 = Sorun yok
  Exit 2 = Disable edilen action var (changed)

Kullanım:
    python zabbix_action_monitor.py --config config.ini
    python zabbix_action_monitor.py --config config.ini --dry-run
    python zabbix_action_monitor.py --config config.ini --mailto admin@example.com
    python zabbix_action_monitor.py --config config.ini --report
"""

import argparse
import configparser
import json
import logging
import os
import signal
import smtplib
import sys
import time
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_INTERVAL = 300  # 5 dakika
DEFAULT_LOG_FILE = ""
DEFAULT_SMTP_SERVER = "localhost"
DEFAULT_SMTP_PORT = 25
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("zabbix_action_monitor")


def setup_logging(log_file: str = "", debug: bool = False):
    level = logging.DEBUG if debug else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, handlers=handlers)


# ---------------------------------------------------------------------------
# Zabbix API Client (minimal, dependency-free)
# ---------------------------------------------------------------------------
class ZabbixAPI:
    """Zabbix JSON-RPC API için minimal istemci."""

    def __init__(self, url: str, user: str = "", password: str = "", api_token: str = ""):
        self.url = url.rstrip("/") + "/api_jsonrpc.php"
        self.auth = None
        self._request_id = 0

        if api_token:
            self.auth = api_token
            logger.info("API token ile bağlantı kurulacak: %s", self.url)
        elif user and password:
            logger.info("Kullanıcı/şifre ile bağlantı kurulacak: %s", self.url)
            self._login(user, password)
        else:
            raise ValueError("API token veya kullanıcı/şifre gerekli.")

    def _call(self, method: str, params=None):
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params if params is not None else {},
            "id": self._request_id,
        }
        no_auth_methods = ("user.login", "apiinfo.version")
        if self.auth and method not in no_auth_methods:
            payload["auth"] = self.auth

        data = json.dumps(payload).encode("utf-8")
        req = Request(self.url, data=data, headers={"Content-Type": "application/json-rpc"})

        try:
            with urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            raise RuntimeError(f"HTTP hatası: {e.code} {e.reason}") from e
        except URLError as e:
            raise RuntimeError(f"Bağlantı hatası: {e.reason}") from e

        if "error" in result:
            err = result["error"]
            raise RuntimeError(f"Zabbix API hatası [{err.get('code')}]: {err.get('data', err.get('message'))}")

        return result.get("result")

    def _login(self, user: str, password: str):
        self.auth = self._call("user.login", {"username": user, "password": password})
        logger.info("Giriş başarılı.")

    # ---- Action İşlemleri ----

    def get_all_trigger_actions(self) -> list:
        """Tüm trigger action'ları döndürür (durum bilgisi ile)."""
        return self._call("action.get", {
            "output": ["actionid", "name", "status", "eventsource"],
            "selectFilter": ["evaltype", "conditions"],
            "filter": {
                "eventsource": 0,
            },
        })

    def disable_action(self, action_id: str) -> dict:
        """Bir action'ı disable eder (status=1)."""
        return self._call("action.update", {
            "actionid": action_id,
            "status": "1",
        })

    def get_api_version(self) -> str:
        return self._call("apiinfo.version")


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def send_email(subject: str, body: str, mailto: str, mailfrom: str = "",
               smtp_server: str = DEFAULT_SMTP_SERVER, smtp_port: int = DEFAULT_SMTP_PORT,
               smtp_user: str = "", smtp_password: str = "", use_tls: bool = False):
    """E-posta gönderir."""
    if not mailto:
        return

    mailfrom = mailfrom or f"zabbix-monitor@{smtp_server}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = mailfrom
    msg["To"] = mailto
    msg["Date"] = datetime.now().strftime("%a, %d %b %Y %H:%M:%S %z")

    # Plain text
    msg.attach(MIMEText(body, "plain", "utf-8"))

    # HTML versiyon
    html_body = body.replace("\n", "<br>\n")
    html = f"""<html><body style="font-family: monospace; font-size: 13px;">
<h3 style="color: #cc0000;">⚠ Zabbix Trigger Action Uyarısı</h3>
{html_body}
<hr><p style="color: #888; font-size: 11px;">Bu e-posta Zabbix Action Monitor tarafından otomatik gönderilmiştir.</p>
</body></html>"""
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        if use_tls:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=15)
            server.starttls()
        else:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=15)

        if smtp_user and smtp_password:
            server.login(smtp_user, smtp_password)

        server.sendmail(mailfrom, mailto.split(","), msg.as_string())
        server.quit()
        logger.info("📧 E-posta gönderildi → %s", mailto)
    except Exception as e:
        logger.error("E-posta gönderilemedi: %s", e)


# ---------------------------------------------------------------------------
# Monitor Logic
# ---------------------------------------------------------------------------
def check_and_disable(api: ZabbixAPI, dry_run: bool = False,
                      mailto: str = "", email_cfg: dict = None) -> list:
    """
    Condition'u boş olan trigger action'ları tespit eder ve disable eder.
    Disable edilen action listesini döndürür.
    """
    disabled_actions = []

    try:
        actions = api.get_all_trigger_actions()
    except RuntimeError as e:
        logger.error("Action listesi alınamadı: %s", e)
        return disabled_actions

    logger.debug("Toplam %d adet trigger action bulundu.", len(actions))

    statuses = {"0": "Enabled", "1": "Disabled"}

    for action in actions:
        action_id = action["actionid"]
        action_name = action["name"]
        action_status = statuses.get(action.get("status", "?"), "?")
        conditions = action.get("filter", {}).get("conditions", [])

        if not conditions:
            logger.warning(
                "⚠  BOŞ CONDITION → Action: '%s' (ID: %s, Status: %s)",
                action_name, action_id, action_status,
            )

            if dry_run:
                logger.info("   [DRY-RUN] Disable edilmedi, sadece raporlandı.")
            else:
                # Zaten disabled ise tekrar disable etmeye gerek yok
                if action.get("status") == "1":
                    logger.info("   ℹ Zaten disabled, atlanıyor.")
                    disabled_actions.append({
                        "actionid": action_id,
                        "name": action_name,
                        "previous_status": action_status,
                        "action_taken": "already_disabled",
                    })
                    continue

                try:
                    api.disable_action(action_id)
                    logger.info(
                        "   ✔ Action DISABLE edildi → '%s' (ID: %s)",
                        action_name, action_id,
                    )
                except RuntimeError as e:
                    logger.error(
                        "   ✘ Disable BAŞARISIZ → '%s' (ID: %s): %s",
                        action_name, action_id, e,
                    )
                    continue

            disabled_actions.append({
                "actionid": action_id,
                "name": action_name,
                "previous_status": action_status,
                "action_taken": "dry_run" if dry_run else "disabled",
            })

    if not disabled_actions:
        logger.info("✔ Tüm trigger action'ların condition'ları mevcut. Sorun yok.")
    else:
        logger.info(
            "Toplam %d adet boş condition'lı action %s.",
            len(disabled_actions),
            "raporlandı (dry-run)" if dry_run else "işlendi",
        )

        # E-posta gönder
        if mailto and disabled_actions:
            _send_report_email(disabled_actions, mailto, email_cfg or {}, dry_run)

    return disabled_actions


def _send_report_email(actions: list, mailto: str, email_cfg: dict, dry_run: bool):
    """Disable edilen action'lar hakkında e-posta raporu gönderir."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mode = "[DRY-RUN] " if dry_run else ""

    subject = f"{mode}Zabbix Action Monitor - {len(actions)} adet boş condition tespit edildi"

    lines = [
        f"Tarih: {timestamp}",
        f"Mod: {'Dry-Run (değişiklik yapılmadı)' if dry_run else 'Otomatik Disable'}",
        f"Tespit edilen action sayısı: {len(actions)}",
        "",
        "─" * 60,
        f"{'ID':<8}  {'Önceki Durum':<14}  {'İşlem':<18}  Ad",
        "─" * 60,
    ]

    for a in actions:
        action_taken = {
            "disabled": "→ DISABLE edildi",
            "already_disabled": "Zaten disabled",
            "dry_run": "[raporlandı]",
        }.get(a["action_taken"], a["action_taken"])

        lines.append(f"{a['actionid']:<8}  {a['previous_status']:<14}  {action_taken:<18}  {a['name']}")

    lines.extend([
        "─" * 60,
        "",
        "Bu action'ların condition'ları boş olduğu için otomatik olarak disable edilmiştir.",
        "Lütfen Zabbix arayüzünden kontrol ediniz.",
    ])

    body = "\n".join(lines)

    send_email(
        subject=subject,
        body=body,
        mailto=mailto,
        mailfrom=email_cfg.get("mail_from", ""),
        smtp_server=email_cfg.get("smtp_server", DEFAULT_SMTP_SERVER),
        smtp_port=int(email_cfg.get("smtp_port", DEFAULT_SMTP_PORT)),
        smtp_user=email_cfg.get("smtp_user", ""),
        smtp_password=email_cfg.get("smtp_password", ""),
        use_tls=email_cfg.get("smtp_tls", False),
    )


def report_all_actions(api: ZabbixAPI):
    """Tüm trigger action'ların durumunu raporlar."""
    actions = api.get_all_trigger_actions()
    statuses = {"0": "Enabled", "1": "Disabled"}

    print("=" * 70)
    print(f"TRIGGER ACTIONS RAPORU ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
    print("=" * 70)
    print(f"{'ID':<8}  {'Status':<10}  {'Cond.#':<8}  Name")
    print("-" * 70)

    empty_count = 0
    for a in sorted(actions, key=lambda x: x["name"]):
        conds = a.get("filter", {}).get("conditions", [])
        status = statuses.get(a.get("status", "?"), "?")
        flag = " ⚠ BOŞ" if not conds else ""
        print(f"{a['actionid']:<8}  {status:<10}  {len(conds):<8}  {a['name']}{flag}")
        if not conds:
            empty_count += 1

    print("-" * 70)
    print(f"Toplam: {len(actions)} action, {empty_count} tanesi boş condition'a sahip.")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Daemon Mode
# ---------------------------------------------------------------------------
_running = True


def _signal_handler(signum, frame):
    global _running
    logger.info("Sinyal alındı (%s), durduruluyor...", signal.Signals(signum).name)
    _running = False


def run_daemon(api: ZabbixAPI, interval: int, dry_run: bool = False,
               mailto: str = "", email_cfg: dict = None):
    """Belirtilen aralıkla kontrol döngüsü çalıştırır."""
    global _running

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    logger.info("Daemon modu başlatıldı. Kontrol aralığı: %d saniye.", interval)

    while _running:
        try:
            check_and_disable(api, dry_run=dry_run, mailto=mailto, email_cfg=email_cfg)
        except Exception as e:
            logger.error("Kontrol sırasında beklenmeyen hata: %s", e)

        wait_until = time.time() + interval
        while _running and time.time() < wait_until:
            time.sleep(1)

    logger.info("Daemon durduruldu.")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config(config_path: str) -> dict:
    """INI formatındaki config dosyasını okur."""
    cfg = configparser.ConfigParser()
    cfg.read(config_path, encoding="utf-8")

    section = "zabbix"
    if not cfg.has_section(section):
        raise ValueError(f"Config dosyasında [{section}] bölümü bulunamadı.")

    result = {
        "url": cfg.get(section, "url", fallback=""),
        "user": cfg.get(section, "user", fallback=""),
        "password": cfg.get(section, "password", fallback=""),
        "api_token": cfg.get(section, "api_token", fallback=""),
        "interval": cfg.getint(section, "interval", fallback=DEFAULT_INTERVAL),
        "log_file": cfg.get(section, "log_file", fallback=DEFAULT_LOG_FILE),
        "dry_run": cfg.getboolean(section, "dry_run", fallback=False),
        "debug": cfg.getboolean(section, "debug", fallback=False),
    }

    # E-posta ayarları
    if cfg.has_section("email"):
        result["mailto"] = cfg.get("email", "mailto", fallback="")
        result["mail_from"] = cfg.get("email", "mail_from", fallback="")
        result["smtp_server"] = cfg.get("email", "smtp_server", fallback=DEFAULT_SMTP_SERVER)
        result["smtp_port"] = cfg.getint("email", "smtp_port", fallback=DEFAULT_SMTP_PORT)
        result["smtp_user"] = cfg.get("email", "smtp_user", fallback="")
        result["smtp_password"] = cfg.get("email", "smtp_password", fallback="")
        result["smtp_tls"] = cfg.getboolean("email", "smtp_tls", fallback=False)

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(
        description="Zabbix trigger action'larını condition kontrolü ile izler.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Örnekler:
  %(prog)s --config config.ini
  %(prog)s --config config.ini --dry-run
  %(prog)s --config config.ini --mailto admin@example.com
  %(prog)s --config config.ini --report
        """,
    )

    parser.add_argument("--config", help="Config dosyası yolu (INI formatı)")
    parser.add_argument("--url", help="Zabbix frontend URL'i")
    parser.add_argument("--user", help="Zabbix kullanıcı adı")
    parser.add_argument("--password", help="Zabbix şifresi")
    parser.add_argument("--api-token", dest="api_token", help="Zabbix API token")
    parser.add_argument("--daemon", action="store_true", help="Daemon modunda sürekli çalış")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help=f"Kontrol aralığı (saniye, varsayılan: {DEFAULT_INTERVAL})")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="Sadece raporla, disable etme")
    parser.add_argument("--report", action="store_true",
                        help="Tüm trigger action'ların durumunu raporla")
    parser.add_argument("--mailto", default="",
                        help="Bildirim e-postası (virgülle ayırarak birden fazla)")
    parser.add_argument("--smtp-server", dest="smtp_server", default="",
                        help=f"SMTP sunucusu (varsayılan: {DEFAULT_SMTP_SERVER})")
    parser.add_argument("--smtp-port", dest="smtp_port", type=int, default=0,
                        help=f"SMTP portu (varsayılan: {DEFAULT_SMTP_PORT})")
    parser.add_argument("--log-file", dest="log_file", default="",
                        help="Log dosyası yolu")
    parser.add_argument("--debug", action="store_true", help="Debug log seviyesi")

    return parser.parse_args()


def main():
    args = parse_args()

    # Config + CLI merge
    url = args.url or ""
    user = args.user or ""
    password = args.password or ""
    api_token = args.api_token or ""
    interval = args.interval
    log_file = args.log_file
    dry_run = args.dry_run
    debug = args.debug
    mailto = args.mailto
    email_cfg = {}

    if args.config:
        cfg = load_config(args.config)
        url = url or cfg["url"]
        user = user or cfg["user"]
        password = password or cfg["password"]
        api_token = api_token or cfg["api_token"]
        interval = args.interval if args.interval != DEFAULT_INTERVAL else cfg["interval"]
        log_file = log_file or cfg.get("log_file", "")
        dry_run = dry_run or cfg.get("dry_run", False)
        debug = debug or cfg.get("debug", False)
        mailto = mailto or cfg.get("mailto", "")

        email_cfg = {
            "mail_from": cfg.get("mail_from", ""),
            "smtp_server": args.smtp_server or cfg.get("smtp_server", DEFAULT_SMTP_SERVER),
            "smtp_port": args.smtp_port or cfg.get("smtp_port", DEFAULT_SMTP_PORT),
            "smtp_user": cfg.get("smtp_user", ""),
            "smtp_password": cfg.get("smtp_password", ""),
            "smtp_tls": cfg.get("smtp_tls", False),
        }

    # CLI SMTP override
    if args.smtp_server:
        email_cfg["smtp_server"] = args.smtp_server
    if args.smtp_port:
        email_cfg["smtp_port"] = args.smtp_port

    setup_logging(log_file, debug)

    if not url:
        logger.error("Zabbix URL gerekli. --url veya config dosyası kullanın.")
        sys.exit(1)

    # Bağlan
    try:
        api = ZabbixAPI(url, user=user, password=password, api_token=api_token)
        version = api.get_api_version()
        logger.info("Zabbix API versiyonu: %s", version)
    except Exception as e:
        logger.error("Zabbix API bağlantısı kurulamadı: %s", e)
        sys.exit(1)

    # Rapor modu
    if args.report:
        report_all_actions(api)
        return

    # Çalıştır
    if args.daemon:
        run_daemon(api, interval, dry_run=dry_run, mailto=mailto, email_cfg=email_cfg)
    else:
        result = check_and_disable(api, dry_run=dry_run, mailto=mailto, email_cfg=email_cfg)
        # AWX uyumlu exit code: 2 = changed (disable edilen action var)
        if result and not dry_run:
            sys.exit(2)


if __name__ == "__main__":
    main()
