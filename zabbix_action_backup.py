#!/usr/bin/env python3
"""
Zabbix Trigger Actions Backup
===============================
Tüm trigger action'ları detaylı şekilde JSON olarak yedekler.
Yedekten geri yükleme (restore) desteği de vardır.

Kullanım:
    python3 zabbix_action_backup.py --url http://zabbix.local --user Admin --password zabbix
    python3 zabbix_action_backup.py --config config.ini
    python3 zabbix_action_backup.py --config config.ini --output /backup/actions.json
    python3 zabbix_action_backup.py --config config.ini --restore /backup/actions_20260213_120000.json

Cron ile günlük yedek:
    0 2 * * * /usr/bin/python3 /path/to/zabbix_action_backup.py --config /etc/zabbix/action_monitor.ini --output /backup/zabbix_actions/ >> /var/log/zabbix_action_backup.log 2>&1
"""

import argparse
import glob
import configparser
import json
import logging
import os
import sys
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

logger = logging.getLogger("zabbix_action_backup")

DEFAULT_BACKUP_DIR = "./backups"


def setup_logging(debug: bool = False):
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


class ZabbixAPI:
    def __init__(self, url, user="", password="", api_token=""):
        self.url = url.rstrip("/") + "/api_jsonrpc.php"
        self.auth = None
        self._request_id = 0

        if api_token:
            self.auth = api_token
        elif user and password:
            self._login(user, password)
        else:
            raise ValueError("API token veya kullanıcı/şifre gerekli.")

    def _call(self, method, params=None):
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": self._request_id,
        }
        no_auth = ("user.login", "apiinfo.version")
        if self.auth and method not in no_auth:
            payload["auth"] = self.auth

        data = json.dumps(payload).encode("utf-8")
        req = Request(self.url, data=data, headers={"Content-Type": "application/json-rpc"})

        try:
            with urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError) as e:
            raise RuntimeError(f"API hatası: {e}") from e

        if "error" in result:
            err = result["error"]
            raise RuntimeError(f"Zabbix API hatası: {err.get('data', err.get('message'))}")

        return result.get("result")

    def _login(self, user, password):
        self.auth = self._call("user.login", {"username": user, "password": password})
        logger.info("Giriş başarılı.")

    def get_all_trigger_actions_full(self):
        """Tüm trigger action'ları TÜM detaylarıyla döndürür (yedek için)."""
        return self._call("action.get", {
            "output": "extend",
            "selectFilter": "extend",
            "selectOperations": "extend",
            "selectRecoveryOperations": "extend",
            "selectUpdateOperations": "extend",
            "filter": {"eventsource": 0},
        })

    def create_action(self, action_data):
        """Yedekten action oluşturur."""
        return self._call("action.create", action_data)


def backup(api, output_path):
    """Tüm trigger action'ları JSON olarak yedekler."""
    actions = api.get_all_trigger_actions_full()

    if not actions:
        logger.warning("Hiç trigger action bulunamadı!")
        return None

    # Dosya adını oluştur
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if os.path.isdir(output_path):
        filepath = os.path.join(output_path, f"trigger_actions_{timestamp}.json")
    elif output_path:
        filepath = output_path
    else:
        os.makedirs(DEFAULT_BACKUP_DIR, exist_ok=True)
        filepath = os.path.join(DEFAULT_BACKUP_DIR, f"trigger_actions_{timestamp}.json")

    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)

    backup_data = {
        "backup_time": datetime.now().isoformat(),
        "action_count": len(actions),
        "actions": actions,
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(backup_data, f, indent=2, ensure_ascii=False)

    logger.info("✔ %d trigger action yedeklendi → %s", len(actions), filepath)
    logger.info("  Dosya boyutu: %.1f KB", os.path.getsize(filepath) / 1024)

    # Özet listesi
    statuses = {0: "Enabled", 1: "Disabled"}
    for a in sorted(actions, key=lambda x: x["name"]):
        conds = a.get("filter", {}).get("conditions", [])
        ops = a.get("operations", [])
        status = statuses.get(int(a.get("status", -1)), "?")
        logger.info("  [%s] %s (conditions: %d, operations: %d)",
                     status, a["name"], len(conds), len(ops))

    return filepath


def cleanup_old_backups(backup_dir, retain_count):
    """Eski yedek dosyalarını temizler, en yeni retain_count kadarını tutar."""
    if not backup_dir or not os.path.isdir(backup_dir):
        return

    pattern = os.path.join(backup_dir, "trigger_actions_*.json")
    files = sorted(glob.glob(pattern), reverse=True)  # en yeniden eskiye

    if len(files) <= retain_count:
        logger.debug("Temizlenecek eski yedek yok (%d dosya, limit: %d).", len(files), retain_count)
        return

    to_delete = files[retain_count:]
    for f in to_delete:
        try:
            os.remove(f)
            logger.info("  🗑 Eski yedek silindi: %s", os.path.basename(f))
        except OSError as e:
            logger.error("  Silinemedi: %s: %s", f, e)

    logger.info("Toplam %d eski yedek temizlendi, %d yedek tutuldu.", len(to_delete), retain_count)


def restore(api, backup_file):
    """Yedekten trigger action'ları geri yükler."""
    with open(backup_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    actions = data.get("actions", [])
    if not actions:
        logger.error("Yedek dosyasında action bulunamadı!")
        return

    logger.info("Yedek dosyası: %s (%s tarihli, %d action)",
                backup_file, data.get("backup_time", "?"), len(actions))

    created = 0
    failed = 0

    for action in actions:
        name = action.get("name", "?")

        # Read-only ve create'te kabul edilmeyen üst seviye alanları kaldır
        readonly_top = {"actionid", "eval_formula", "formula"}
        restore_data = {k: v for k, v in action.items() if k not in readonly_top}

        # Filter içini temizle - sadece create'in kabul ettiği alanları bırak
        if "filter" in restore_data:
            flt = restore_data["filter"]
            # Filter için sadece evaltype ve conditions kabul edilir
            restore_data["filter"] = {
                "evaltype": flt.get("evaltype", "0"),
                "conditions": [],
            }
            # Conditions için sadece conditiontype, value ve operator kabul edilir
            for cond in flt.get("conditions", []):
                clean_cond = {
                    "conditiontype": cond["conditiontype"],
                    "value": cond["value"],
                }
                if "operator" in cond:
                    clean_cond["operator"] = cond["operator"]
                restore_data["filter"]["conditions"].append(clean_cond)

        # Operations temizle - sadece create'in kabul ettiği alanları bırak
        # opmessage kabul edilen alanlar (default_msg=1 ise subject/message kabul edilmez)
        opmessage_fields = {"default_msg", "mediatypeid"}
        opmessage_fields_custom = {"default_msg", "mediatypeid", "subject", "message"}

        # Operation kabul edilen alanlar (tip bazlı farklılık var)
        op_fields_by_type = {
            "operations": {
                "operationtype", "esc_period", "esc_step_from", "esc_step_to",
                "evaltype", "opconditions", "opmessage", "opmessage_grp",
                "opmessage_usr", "opcommand", "opcommand_grp", "opcommand_hst",
                "opgroup", "optemplate",
            },
            "recovery_operations": {
                "operationtype", "opmessage", "opmessage_grp",
                "opmessage_usr", "opcommand", "opcommand_grp", "opcommand_hst",
            },
            "update_operations": {
                "operationtype", "opmessage", "opmessage_grp",
                "opmessage_usr", "opcommand", "opcommand_grp", "opcommand_hst",
            },
        }

        # Alt liste ID alanları (kaldırılacak)
        sub_remove_ids = {
            "operationid", "opmessage_grpid", "opmessage_usrid",
            "opconditionid", "opcommand_grpid", "opcommand_hstid",
        }

        for key in ("operations", "recovery_operations", "update_operations"):
            if key not in restore_data:
                continue
            clean_ops = []
            for op in restore_data[key]:
                clean_op = {k: v for k, v in op.items() if k in op_fields_by_type[key]}

                # opmessage temizle
                if "opmessage" in clean_op:
                    msg = clean_op["opmessage"]
                    if str(msg.get("default_msg", "1")) == "0":
                        # Özel mesaj: tüm alanlar kabul edilir
                        clean_op["opmessage"] = {k: v for k, v in msg.items() if k in opmessage_fields_custom}
                    else:
                        # Varsayılan mesaj: sadece default_msg yeterli
                        clean_op["opmessage"] = {"default_msg": "1"}

                # Alt listelerden auto-generated ID'leri kaldır
                for sub_key in ("opmessage_grp", "opmessage_usr", "opconditions",
                                "opcommand_grp", "opcommand_hst", "optemplate", "opgroup"):
                    if sub_key in clean_op:
                        for item in clean_op[sub_key]:
                            for id_key in list(item.keys()):
                                if id_key in sub_remove_ids:
                                    item.pop(id_key)

                clean_ops.append(clean_op)
            restore_data[key] = clean_ops

        try:
            result = api.create_action(restore_data)
            logger.info("  ✔ Oluşturuldu: '%s' (yeni ID: %s)", name, result.get("actionids", ["?"])[0])
            created += 1
        except RuntimeError as e:
            logger.error("  ✘ Başarısız: '%s': %s", name, e)
            failed += 1

    logger.info("Restore tamamlandı: %d oluşturuldu, %d başarısız.", created, failed)


def load_config(config_path):
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
    }


def main():
    parser = argparse.ArgumentParser(description="Zabbix trigger action'larını yedekle/geri yükle.")
    parser.add_argument("--config", help="Config dosyası (INI)")
    parser.add_argument("--url", help="Zabbix URL")
    parser.add_argument("--user", help="Kullanıcı adı")
    parser.add_argument("--password", help="Şifre")
    parser.add_argument("--api-token", dest="api_token", help="API token")
    parser.add_argument("--output", default="", help="Yedek dosya yolu veya dizini")
    parser.add_argument("--restore", metavar="FILE", help="Bu dosyadan geri yükle")
    parser.add_argument("--retain", type=int, default=0,
                        help="Tutulacak yedek sayısı (eski yedekler silinir, 0=sınırsız)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    setup_logging(args.debug)

    url = args.url or ""
    user = args.user or ""
    password = args.password or ""
    api_token = args.api_token or ""

    if args.config:
        cfg = load_config(args.config)
        url = url or cfg["url"]
        user = user or cfg["user"]
        password = password or cfg["password"]
        api_token = api_token or cfg["api_token"]

    if not url:
        logger.error("Zabbix URL gerekli.")
        sys.exit(1)

    try:
        api = ZabbixAPI(url, user=user, password=password, api_token=api_token)
    except Exception as e:
        logger.error("Bağlantı hatası: %s", e)
        sys.exit(1)

    if args.restore:
        restore(api, args.restore)
    else:
        filepath = backup(api, args.output)

        # Eski yedekleri temizle
        if args.retain > 0 and filepath:
            backup_dir = os.path.dirname(filepath)
            cleanup_old_backups(backup_dir, args.retain)


if __name__ == "__main__":
    main()
