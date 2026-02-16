#!/usr/bin/env python3
"""
Zabbix Trigger Action Monitor
==============================
Zabbix 6.4 trigger action'larını periyodik olarak kontrol eder.
Condition'u boş olan enabled action'ları otomatik olarak siler.

Kullanım:
    python zabbix_action_monitor.py                    # Tek seferlik çalıştır
    python zabbix_action_monitor.py --daemon           # Daemon modunda çalıştır
    python zabbix_action_monitor.py --interval 60      # 60 saniye aralıkla kontrol et
    python zabbix_action_monitor.py --dry-run          # Sadece raporla, silme
    python zabbix_action_monitor.py --config config.ini # Config dosyası kullan

Cron ile kullanım (her 5 dakikada):
    */5 * * * * /usr/bin/python3 /path/to/zabbix_action_monitor.py >> /var/log/zabbix_action_monitor.log 2>&1
"""

import argparse
import configparser
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_INTERVAL = 300  # 5 dakika
DEFAULT_LOG_FILE = ""   # Boşsa stdout'a yazar
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

    def _call(self, method: str, params: dict = None) -> dict:
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
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

    def get_enabled_trigger_actions(self) -> list:
        """Enabled olan trigger (eventsource=0) action'ları döndürür."""
        return self._call("action.get", {
            "output": ["actionid", "name", "status", "eventsource"],
            "selectFilter": ["evaltype", "conditions"],
            "filter": {
                "eventsource": 0,  # trigger actions
                "status": 0,       # enabled
            },
        })

    def get_all_trigger_actions(self) -> list:
        """Tüm trigger action'ları döndürür (durum bilgisi ile)."""
        return self._call("action.get", {
            "output": ["actionid", "name", "status", "eventsource"],
            "selectFilter": ["evaltype", "conditions"],
            "filter": {
                "eventsource": 0,
            },
        })

    def delete_action(self, action_id: str) -> dict:
        """Bir action'ı siler."""
        return self._call("action.delete", [action_id])

    def get_api_version(self) -> str:
        return self._call("apiinfo.version")


# ---------------------------------------------------------------------------
# Monitor Logic
# ---------------------------------------------------------------------------
def check_and_delete(api: ZabbixAPI, dry_run: bool = False) -> list:
    """
    Condition'u boş olan trigger action'ları tespit eder ve siler (enabled/disabled fark etmez).
    Silinen action listesini döndürür.
    """
    deleted = []

    try:
        actions = api.get_all_trigger_actions()
    except RuntimeError as e:
        logger.error("Action listesi alınamadı: %s", e)
        return deleted

    logger.debug("Toplam %d adet enabled trigger action bulundu.", len(actions))

    for action in actions:
        action_id = action["actionid"]
        action_name = action["name"]
        conditions = action.get("filter", {}).get("conditions", [])

        if not conditions:
            logger.warning(
                "⚠  BOŞ CONDITION TESPİT EDİLDİ → Action: '%s' (ID: %s)",
                action_name, action_id,
            )

            if dry_run:
                logger.info("   [DRY-RUN] Silinmedi, sadece raporlandı.")
            else:
                try:
                    api.delete_action(action_id)
                    logger.info(
                        "   ✔ Action SİLİNDİ → '%s' (ID: %s)",
                        action_name, action_id,
                    )
                except RuntimeError as e:
                    logger.error(
                        "   ✘ Silme BAŞARISIZ → '%s' (ID: %s): %s",
                        action_name, action_id, e,
                    )
                    continue

            deleted.append({"actionid": action_id, "name": action_name})

    if not deleted:
        logger.info("✔ Tüm enabled trigger action'ların condition'ları mevcut. Sorun yok.")
    else:
        logger.info(
            "Toplam %d adet boş condition'lı action %s.",
            len(deleted),
            "raporlandı (dry-run)" if dry_run else "silindi",
        )

    return deleted


def report_all_actions(api: ZabbixAPI):
    """Tüm trigger action'ların durumunu raporlar."""
    actions = api.get_all_trigger_actions()
    statuses = {0: "Enabled", 1: "Disabled"}

    logger.info("=" * 70)
    logger.info("TRIGGER ACTIONS RAPORU (%s)", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("=" * 70)
    logger.info("%-8s  %-10s  %-8s  %s", "ID", "Status", "Cond.#", "Name")
    logger.info("-" * 70)

    empty_count = 0
    for a in sorted(actions, key=lambda x: x["name"]):
        conds = a.get("filter", {}).get("conditions", [])
        status = statuses.get(int(a["status"]), "?")
        flag = " ⚠ BOŞ" if not conds else ""
        logger.info("%-8s  %-10s  %-8d  %s%s", a["actionid"], status, len(conds), a["name"], flag)
        if not conds:
            empty_count += 1

    logger.info("-" * 70)
    logger.info("Toplam: %d action, %d tanesi boş condition'a sahip.", len(actions), empty_count)
    logger.info("=" * 70)


# ---------------------------------------------------------------------------
# Daemon Mode
# ---------------------------------------------------------------------------
_running = True


def _signal_handler(signum, frame):
    global _running
    logger.info("Sinyal alındı (%s), durduruluyor...", signal.Signals(signum).name)
    _running = False


def run_daemon(api: ZabbixAPI, interval: int, dry_run: bool = False):
    """Belirtilen aralıkla kontrol döngüsü çalıştırır."""
    global _running

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    logger.info("Daemon modu başlatıldı. Kontrol aralığı: %d saniye.", interval)

    while _running:
        try:
            check_and_delete(api, dry_run=dry_run)
        except Exception as e:
            logger.error("Kontrol sırasında beklenmeyen hata: %s", e)

        # Interval boyunca bekle, ama sinyal gelirse erken çık
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

    return {
        "url": cfg.get(section, "url", fallback=""),
        "user": cfg.get(section, "user", fallback=""),
        "password": cfg.get(section, "password", fallback=""),
        "api_token": cfg.get(section, "api_token", fallback=""),
        "interval": cfg.getint(section, "interval", fallback=DEFAULT_INTERVAL),
        "log_file": cfg.get(section, "log_file", fallback=DEFAULT_LOG_FILE),
        "dry_run": cfg.getboolean(section, "dry_run", fallback=False),
        "debug": cfg.getboolean(section, "debug", fallback=False),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(
        description="Zabbix trigger action'larını condition kontrolü ile izler.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Örnekler:
  %(prog)s --url http://zabbix.local --user Admin --password zabbix
  %(prog)s --url http://zabbix.local --api-token abc123 --daemon --interval 120
  %(prog)s --config /etc/zabbix/action_monitor.ini --dry-run
  %(prog)s --url http://zabbix.local --user Admin --password zabbix --report
        """,
    )

    parser.add_argument("--config", help="Config dosyası yolu (INI formatı)")
    parser.add_argument("--url", help="Zabbix frontend URL'i (örn: http://zabbix.local)")
    parser.add_argument("--user", help="Zabbix kullanıcı adı")
    parser.add_argument("--password", help="Zabbix şifresi")
    parser.add_argument("--api-token", dest="api_token", help="Zabbix API token")
    parser.add_argument("--daemon", action="store_true", help="Daemon modunda sürekli çalış")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help=f"Kontrol aralığı (saniye, varsayılan: {DEFAULT_INTERVAL})")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="Sadece raporla, silme")
    parser.add_argument("--report", action="store_true",
                        help="Tüm trigger action'ların durumunu raporla")
    parser.add_argument("--log-file", dest="log_file", default="",
                        help="Log dosyası yolu")
    parser.add_argument("--debug", action="store_true", help="Debug log seviyesi")

    return parser.parse_args()


def main():
    args = parse_args()

    # Config dosyasından yükle, ardından CLI argümanları ile override et
    url = args.url or ""
    user = args.user or ""
    password = args.password or ""
    api_token = args.api_token or ""
    interval = args.interval
    log_file = args.log_file
    dry_run = args.dry_run
    debug = args.debug

    if args.config:
        cfg = load_config(args.config)
        url = url or cfg["url"]
        user = user or cfg["user"]
        password = password or cfg["password"]
        api_token = api_token or cfg["api_token"]
        interval = args.interval if args.interval != DEFAULT_INTERVAL else cfg["interval"]
        log_file = log_file or cfg["log_file"]
        dry_run = dry_run or cfg["dry_run"]
        debug = debug or cfg["debug"]

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
        run_daemon(api, interval, dry_run=dry_run)
    else:
        result = check_and_delete(api, dry_run=dry_run)
        if result and not dry_run:
            sys.exit(2)  # Silinen action varsa exit code 2


if __name__ == "__main__":
    main()
