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
                      mailto: str = "", email_cfg: dict = None,
                      excluded_names: list = None) -> list:
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

    excluded = set(excluded_names or [])
    statuses = {"0": "Enabled", "1": "Disabled"}

    for action in actions:
        action_id = action["actionid"]
        action_name = action["name"]
        action_status = statuses.get(action.get("status", "?"), "?")
        conditions = action.get("filter", {}).get("conditions", [])

        # Dışlama listesinde ise atla
        if action_name in excluded:
            logger.debug("   ⏭  Dışlama listesinde, atlanıyor → '%s'", action_name)
            continue

        if not conditions:
            logger.warning(
                "⚠  BOŞ CONDITION → Action: '%s' (ID: %s, Status: %s)",
                action_name, action_id, action_status,
            )

            if dry_run:
                logger.info("   [DRY-RUN] Disable edilmedi, sadece raporlandı.")
                disabled_actions.append({
                    "actionid": action_id,
                    "name": action_name,
                    "previous_status": action_status,
                    "action_taken": "dry_run",
                })
            else:
                # Zaten disabled ise sadece logla, listeye ekleme (mail bombardımanı önlemi)
                if action.get("status") == "1":
                    logger.info("   ℹ Zaten disabled, atlanıyor.")
                    continue

                try:
                    api.disable_action(action_id)
                    logger.info(
                        "   ✔ Action DISABLE edildi → '%s' (ID: %s)",
                        action_name, action_id,
                    )
                    disabled_actions.append({
                        "actionid": action_id,
                        "name": action_name,
                        "previous_status": action_status,
                        "action_taken": "disabled",
                    })
                except RuntimeError as e:
                    logger.error(
                        "   ✘ Disable BAŞARISIZ → '%s' (ID: %s): %s",
                        action_name, action_id, e,
                    )

    if not disabled_actions:
        logger.info("✔ Tüm trigger action'ların condition'ları mevcut. Sorun yok.")
    else:
        logger.info(
            "Toplam %d adet action bu turda disable edildi.",
            len(disabled_actions),
        )

        # E-posta gönder — sadece bu turda YENİ disable edilenler için
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
               mailto: str = "", email_cfg: dict = None,
               excluded_names: list = None):
    """Belirtilen aralıkla kontrol döngüsü çalıştırır."""
    global _running

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    logger.info("Daemon modu başlatıldı. Kontrol aralığı: %d saniye.", interval)

    while _running:
        try:
            check_and_disable(api, dry_run=dry_run, mailto=mailto, email_cfg=email_cfg,
                              excluded_names=excluded_names)
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

    # exclude_actions: virgülle ayrılmış action isimleri
    raw_exclude = cfg.get(section, "exclude_actions", fallback="")
    excluded = [n.strip() for n in raw_exclude.split(",") if n.strip()]

    result = {
        "url": cfg.get(section, "url", fallback=""),
        "user": cfg.get(section, "user", fallback=""),
        "password": cfg.get(section, "password", fallback=""),
        "api_token": cfg.get(section, "api_token", fallback=""),
        "interval": cfg.getint(section, "interval", fallback=DEFAULT_INTERVAL),
        "log_file": cfg.get(section, "log_file", fallback=DEFAULT_LOG_FILE),
        "dry_run": cfg.getboolean(section, "dry_run", fallback=False),
        "debug": cfg.getboolean(section, "debug", fallback=False),
        "exclude_actions": excluded,
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
Environment Variables (AWX/CI uyumlu):
  ZABBIX_URL          Zabbix frontend URL
  ZABBIX_USER         Zabbix kullanıcı adı
  ZABBIX_PASSWORD     Zabbix şifresi
  ZABBIX_API_TOKEN    Zabbix API token
  MAILTO              Bildirim e-posta adresi
  MAIL_FROM           Gönderen e-posta adresi
  SMTP_SERVER         SMTP sunucusu
  SMTP_PORT           SMTP portu
  SMTP_USER           SMTP kullanıcı
  SMTP_PASSWORD       SMTP şifre
  SMTP_TLS            true/false

Öncelik sırası: Environment Variable > CLI Argüman > Config Dosyası

Örnekler:
  %(prog)s --config config.ini
  ZABBIX_URL=http://zabbix.local ZABBIX_API_TOKEN=abc123 %(prog)s --mailto admin@example.com
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
    parser.add_argument(
        "--exclude-action",
        dest="exclude_actions",
        action="append",
        default=[],
        metavar="ACTION_NAME",
        help="Dışlanacak trigger action adı (birden fazla kez kullanılabilir)",
    )

    return parser.parse_args()


def _resolve_settings(args) -> dict:
    """
    Ayarları çözer. Öncelik: Environment Variable > CLI Argüman > Config Dosyası
    """
    # 1) Config dosyasından (en düşük öncelik)
    cfg = {}
    if args.config:
        cfg = load_config(args.config)

    # 2) Her ayar için: ENV > CLI > Config
    def resolve(env_name, cli_value, cfg_key, default=""):
        env_val = os.environ.get(env_name, "")
        if env_val:
            return env_val
        if cli_value:
            return cli_value
        return cfg.get(cfg_key, default)

    settings = {
        "url": resolve("ZABBIX_URL", args.url, "url"),
        "user": resolve("ZABBIX_USER", args.user, "user"),
        "password": resolve("ZABBIX_PASSWORD", args.password, "password"),
        "api_token": resolve("ZABBIX_API_TOKEN", args.api_token, "api_token"),
        "mailto": resolve("MAILTO", args.mailto, "mailto"),
        "log_file": resolve("LOG_FILE", args.log_file, "log_file"),
    }

    # Boolean/int ayarlar
    settings["interval"] = int(os.environ.get("INTERVAL", "0")) or args.interval
    if settings["interval"] == DEFAULT_INTERVAL:
        settings["interval"] = cfg.get("interval", DEFAULT_INTERVAL)

    settings["dry_run"] = (
        os.environ.get("DRY_RUN", "").lower() in ("true", "1", "yes")
        or args.dry_run
        or cfg.get("dry_run", False)
    )
    settings["debug"] = (
        os.environ.get("DEBUG", "").lower() in ("true", "1", "yes")
        or args.debug
        or cfg.get("debug", False)
    )

    # Dışlama listesi: ENV > CLI > Config
    env_exclude = os.environ.get("EXCLUDE_ACTIONS", "")
    env_exclude_list = [n.strip() for n in env_exclude.split(",") if n.strip()]
    cfg_exclude_list = cfg.get("exclude_actions", [])
    # ENV varsa önce onu al, sonra CLI, sonra config
    if env_exclude_list:
        settings["exclude_actions"] = env_exclude_list
    elif args.exclude_actions:
        settings["exclude_actions"] = args.exclude_actions
    else:
        settings["exclude_actions"] = cfg_exclude_list

    # E-posta ayarları
    settings["email_cfg"] = {
        "mail_from": resolve("MAIL_FROM", "", "mail_from"),
        "smtp_server": resolve("SMTP_SERVER", args.smtp_server, "smtp_server", DEFAULT_SMTP_SERVER),
        "smtp_port": int(resolve("SMTP_PORT", str(args.smtp_port or ""), "smtp_port", str(DEFAULT_SMTP_PORT))),
        "smtp_user": resolve("SMTP_USER", "", "smtp_user"),
        "smtp_password": resolve("SMTP_PASSWORD", "", "smtp_password"),
        "smtp_tls": os.environ.get("SMTP_TLS", "").lower() in ("true", "1", "yes") or cfg.get("smtp_tls", False),
    }

    return settings


def main():
    args = parse_args()
    s = _resolve_settings(args)

    setup_logging(s["log_file"], s["debug"])

    # Hangi kaynaktan geldiğini logla
    sources = []
    if any(os.environ.get(k) for k in ("ZABBIX_URL", "ZABBIX_USER", "ZABBIX_API_TOKEN")):
        sources.append("environment")
    if args.config:
        sources.append("config")
    if args.url or args.user or args.api_token:
        sources.append("cli")
    logger.info("Ayar kaynakları: %s", ", ".join(sources) if sources else "varsayılan")

    if not s["url"]:
        logger.error("Zabbix URL gerekli. ZABBIX_URL env, --url veya config dosyası kullanın.")
        sys.exit(1)

    # Bağlan
    try:
        api = ZabbixAPI(s["url"], user=s["user"], password=s["password"], api_token=s["api_token"])
        version = api.get_api_version()
        logger.info("Zabbix API versiyonu: %s", version)
    except Exception as e:
        logger.error("Zabbix API bağlantısı kurulamadı: %s", e)
        sys.exit(1)

    # Rapor modu
    if args.report:
        report_all_actions(api)
        return

    # Dışlama listesi
    excluded = s.get("exclude_actions", [])
    if excluded:
        logger.info("Dışlanan action'lar (%d): %s", len(excluded), ", ".join(excluded))

    # Çalıştır
    if args.daemon:
        run_daemon(api, s["interval"], dry_run=s["dry_run"],
                   mailto=s["mailto"], email_cfg=s["email_cfg"],
                   excluded_names=excluded)
    else:
        result = check_and_disable(api, dry_run=s["dry_run"],
                                   mailto=s["mailto"], email_cfg=s["email_cfg"],
                                   excluded_names=excluded)
        # AWX uyumlu exit code: 2 = changed (disable edilen action var)
        if result and not s["dry_run"]:
            sys.exit(2)


if __name__ == "__main__":
    main()

