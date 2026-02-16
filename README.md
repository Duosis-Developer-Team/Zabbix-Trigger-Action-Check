# Zabbix Trigger Action Monitor & Backup

Zabbix 6.4 trigger action'larını kontrol eden iki script:

1. **`zabbix_action_monitor.py`** — Condition'u boş olan trigger action'ları otomatik **disable** eder ve e-posta ile bildirir
2. **`zabbix_action_backup.py`** — Tüm trigger action'ları JSON olarak **yedekler** ve geri yükler

## Problem

Zabbix 6.4'te bir trigger action'ın condition'ı silindiğinde action aktif kalırsa, **tüm alarmlar** tanımlı kullanıcılara gider. Bu scriptler bu durumu otomatik tespit edip önler ve güvenli yedekleme sağlar.

## Özellikler

- **Sıfır bağımlılık** — Sadece Python 3 standart kütüphanesi
- **API Token** veya **kullanıcı/şifre** ile kimlik doğrulama
- **E-posta bildirimi** — Boş condition tespit edildiğinde SMTP ile bildirim
- **AWX/Ansible Tower uyumlu** — Exit code 0=sorun yok, 2=disable edilen action var
- **Ortak config dosyası** — İki script de aynı `config.ini` dosyasını kullanır

---

## Kurulum

```bash
sudo mkdir -p /opt/zabbix-action-monitor
sudo cp zabbix_action_monitor.py zabbix_action_backup.py /opt/zabbix-action-monitor/
sudo cp config.ini.example /etc/zabbix/action_monitor.ini
sudo chmod 600 /etc/zabbix/action_monitor.ini
sudo nano /etc/zabbix/action_monitor.ini
```

---

## Monitor Script — `zabbix_action_monitor.py`

Condition'u boş olan trigger action'ları (enabled/disabled fark etmez) tespit eder, **disable** eder ve e-posta ile bildirir.

### Kullanım

```bash
# Tek seferlik kontrol
python3 zabbix_action_monitor.py --config config.ini

# Dry-run (sadece raporla)
python3 zabbix_action_monitor.py --config config.ini --dry-run

# E-posta bildirimli
python3 zabbix_action_monitor.py --config config.ini --mailto admin@example.com

# Tüm action'ların raporu
python3 zabbix_action_monitor.py --config config.ini --report

# Daemon modunda
python3 zabbix_action_monitor.py --config config.ini --daemon --interval 300
```

### AWX / Ansible Tower ile Kullanım

AWX üzerinden doğrudan çalıştırılabilir:

- **Exit 0** → Sorun yok, hiçbir action disable edilmedi
- **Exit 2** → Disable edilen action var (AWX'te "changed" olarak görünür)

AWX Job Template'te şu komutu kullanın:
```bash
python3 /opt/zabbix-action-monitor/zabbix_action_monitor.py --config /etc/zabbix/action_monitor.ini --mailto admin@example.com
```

### Parametreler

| Parametre       | Açıklama                               | Varsayılan  |
|-----------------|----------------------------------------|-------------|
| `--config`      | Config dosyası yolu                    | -           |
| `--url`         | Zabbix frontend URL'i                  | (zorunlu)   |
| `--user`        | Kullanıcı adı                          | -           |
| `--password`    | Şifre                                  | -           |
| `--api-token`   | API Token                              | -           |
| `--mailto`      | Bildirim e-postası                     | -           |
| `--smtp-server` | SMTP sunucusu                          | `localhost`  |
| `--smtp-port`   | SMTP portu                             | `25`        |
| `--daemon`      | Daemon modunda çalış                   | false       |
| `--interval`    | Kontrol aralığı (saniye)               | 300         |
| `--dry-run`     | Sadece raporla                         | false       |
| `--report`      | Tüm action raporu                      | false       |
| `--log-file`    | Log dosyası yolu                       | stdout      |
| `--debug`       | Debug seviyesi                         | false       |

---

## Backup Script — `zabbix_action_backup.py`

Tüm trigger action'ları detaylı JSON olarak yedekler. Geri yükleme (restore) desteği vardır.

### Kullanım

```bash
# Yedekle (./backups/ dizinine)
python3 zabbix_action_backup.py --config config.ini

# Geri yükle
python3 zabbix_action_backup.py --config config.ini --restore ./backups/trigger_actions_20260216.json

# Son 7 yedeği tut, eskilerini sil
python3 zabbix_action_backup.py --config config.ini --retain 7
```

### Parametreler

| Parametre     | Açıklama                            | Varsayılan  |
|---------------|-------------------------------------|-------------|
| `--config`    | Config dosyası yolu                 | -           |
| `--output`    | Yedek dosya yolu veya dizini        | `./backups` |
| `--restore`   | Bu dosyadan geri yükle              | -           |
| `--retain`    | Tutulacak yedek sayısı (0=sınırsız) | 0           |

---

## Config Dosyası

`config.ini.example` dosyasını kopyalayıp düzenleyin:

```ini
[zabbix]
url = http://your-zabbix-server.com
user = Admin
password = zabbix
interval = 300
dry_run = false
log_file = /var/log/zabbix_action_monitor.log

[email]
mailto = admin@example.com
mail_from = zabbix-monitor@example.com
smtp_server = localhost
smtp_port = 25
smtp_tls = false
```

---

## Önerilen Cron Konfigürasyonu

```bash
# Her dakika condition kontrolü + e-posta bildirim
* * * * * /usr/bin/python3 /opt/zabbix-action-monitor/zabbix_action_monitor.py --config /etc/zabbix/action_monitor.ini >> /var/log/zabbix_action_monitor.log 2>&1

# Günde 1 kez yedek al, son 30 yedeği tut
0 2 * * * /usr/bin/python3 /opt/zabbix-action-monitor/zabbix_action_backup.py --config /etc/zabbix/action_monitor.ini --output /backup/zabbix_actions/ --retain 30 >> /var/log/zabbix_action_backup.log 2>&1
```

## Çıktı Örnekleri

### Sorun yok (exit 0)
```
2026-02-16 10:00:00 [INFO] Giriş başarılı.
2026-02-16 10:00:00 [INFO] Zabbix API versiyonu: 6.4.0
2026-02-16 10:00:00 [INFO] ✔ Tüm trigger action'ların condition'ları mevcut. Sorun yok.
```

### Boş condition tespit + disable (exit 2)
```
2026-02-16 10:00:00 [WARNING] ⚠  BOŞ CONDITION → Action: 'test11' (ID: 42, Status: Enabled)
2026-02-16 10:00:00 [INFO]    ✔ Action DISABLE edildi → 'test11' (ID: 42)
2026-02-16 10:00:00 [INFO] 📧 E-posta gönderildi → admin@example.com
```
