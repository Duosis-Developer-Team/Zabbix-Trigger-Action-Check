# Zabbix Trigger Action Monitor & Backup

Zabbix 6.4 trigger action'larını kontrol eden iki script:

1. **`zabbix_action_monitor.py`** — Condition'u boş olan trigger action'ları otomatik **disable** eder ve e-posta ile bildirir
2. **`zabbix_action_backup.py`** — Tüm trigger action'ları JSON olarak **yedekler** ve geri yükler

## Problem

Zabbix 6.4'te bir trigger action'ın condition'ı silindiğinde action aktif kalırsa, **tüm alarmlar** tanımlı kullanıcılara gider. Bu scriptler bu durumu otomatik tespit edip önler.

## Özellikler

- **Sıfır bağımlılık** — Sadece Python 3 standart kütüphanesi
- **Environment variable desteği** — AWX/Ansible Tower credential entegrasyonu
- **E-posta bildirimi** — Boş condition tespit edildiğinde SMTP ile bildirim
- **AWX uyumlu exit code** — `0`=sorun yok, `2`=changed (disable edilen action var)

---

## AWX / Ansible Tower Entegrasyonu

### Credential Tanımları

AWX'te **Custom Credential Type** oluşturun:

**Input Configuration:**
```yaml
fields:
  - id: zabbix_url
    type: string
    label: Zabbix URL
  - id: zabbix_user
    type: string
    label: Zabbix User
  - id: zabbix_password
    type: string
    label: Zabbix Password
    secret: true
  - id: mailto
    type: string
    label: Notification Email
  - id: smtp_server
    type: string
    label: SMTP Server
required:
  - zabbix_url
```

**Injector Configuration:**
```yaml
env:
  ZABBIX_URL: '{{ zabbix_url }}'
  ZABBIX_USER: '{{ zabbix_user }}'
  ZABBIX_PASSWORD: '{{ zabbix_password }}'
  MAILTO: '{{ mailto }}'
  SMTP_SERVER: '{{ smtp_server }}'
```

### Job Template Komutu

```bash
python3 /opt/zabbix-action-monitor/zabbix_action_monitor.py
```

> Credential'lar environment variable olarak inject edilir, config dosyası veya komut satırı argümanı gerekmez.

### Exit Code'lar

| Exit Code | Anlam          | AWX Durumu |
|-----------|----------------|------------|
| `0`       | Sorun yok      | Success    |
| `2`       | Action disable edildi | Changed |
| `1`       | Hata           | Failed     |

---

## Environment Variables

Tüm ayarlar environment variable ile verilebilir. Öncelik: **ENV > CLI > Config Dosyası**

| Variable           | Açıklama                  |
|--------------------|---------------------------|
| `ZABBIX_URL`       | Zabbix frontend URL       |
| `ZABBIX_USER`      | Kullanıcı adı             |
| `ZABBIX_PASSWORD`  | Şifre                     |
| `ZABBIX_API_TOKEN` | API Token                 |
| `MAILTO`           | Bildirim e-posta adresi   |
| `MAIL_FROM`        | Gönderen adres            |
| `SMTP_SERVER`      | SMTP sunucusu             |
| `SMTP_PORT`        | SMTP portu                |
| `SMTP_USER`        | SMTP kullanıcı            |
| `SMTP_PASSWORD`    | SMTP şifre                |
| `SMTP_TLS`         | TLS kullan (true/false)   |
| `DRY_RUN`          | Sadece raporla            |

---

## Kurulum

```bash
sudo mkdir -p /opt/zabbix-action-monitor
sudo cp zabbix_action_monitor.py zabbix_action_backup.py /opt/zabbix-action-monitor/
```

---

## Monitor Script — `zabbix_action_monitor.py`

### Kullanım

```bash
# AWX ile (env variable'lar AWX'ten gelir)
python3 zabbix_action_monitor.py

# Config dosyası ile
python3 zabbix_action_monitor.py --config config.ini

# Dry-run
python3 zabbix_action_monitor.py --config config.ini --dry-run

# Direkt env variable ile
ZABBIX_URL=http://zabbix.local ZABBIX_USER=Admin ZABBIX_PASSWORD=zabbix \
  MAILTO=admin@example.com SMTP_SERVER=smtp.local \
  python3 zabbix_action_monitor.py

# Rapor
python3 zabbix_action_monitor.py --config config.ini --report
```

### CLI Parametreleri

| Parametre       | Açıklama                | Varsayılan  |
|-----------------|-------------------------|-------------|
| `--config`      | Config dosyası          | -           |
| `--url`         | Zabbix URL              | -           |
| `--user`        | Kullanıcı adı           | -           |
| `--password`    | Şifre                   | -           |
| `--api-token`   | API Token               | -           |
| `--mailto`      | Bildirim e-postası      | -           |
| `--smtp-server` | SMTP sunucusu           | `localhost` |
| `--smtp-port`   | SMTP portu              | `25`        |
| `--daemon`      | Daemon modu             | false       |
| `--interval`    | Kontrol aralığı (sn)    | 300         |
| `--dry-run`     | Sadece raporla          | false       |
| `--report`      | Tüm action raporu       | false       |
| `--log-file`    | Log dosyası             | stdout      |
| `--debug`       | Debug seviyesi          | false       |

---

## Backup Script — `zabbix_action_backup.py`

```bash
# Yedekle
python3 zabbix_action_backup.py --config config.ini

# Geri yükle
python3 zabbix_action_backup.py --config config.ini --restore ./backups/trigger_actions_20260216.json

# Son 7 yedeği tut
python3 zabbix_action_backup.py --config config.ini --retain 7
```

---

## Cron Konfigürasyonu

```bash
# Her dakika condition kontrolü
* * * * * /usr/bin/python3 /opt/zabbix-action-monitor/zabbix_action_monitor.py --config /etc/zabbix/action_monitor.ini >> /var/log/zabbix_action_monitor.log 2>&1

# Günde 1 kez yedek, son 30 yedeği tut
0 2 * * * /usr/bin/python3 /opt/zabbix-action-monitor/zabbix_action_backup.py --config /etc/zabbix/action_monitor.ini --output /backup/zabbix/ --retain 30 >> /var/log/zabbix_action_backup.log 2>&1
```

## Çıktı Örnekleri

### Sorun yok (exit 0)
```
2026-02-16 10:00:00 [INFO] Ayar kaynakları: environment
2026-02-16 10:00:00 [INFO] Giriş başarılı.
2026-02-16 10:00:00 [INFO] Zabbix API versiyonu: 6.4.0
2026-02-16 10:00:00 [INFO] ✔ Tüm trigger action'ların condition'ları mevcut. Sorun yok.
```

### Disable + e-posta (exit 2)
```
2026-02-16 10:00:00 [WARNING] ⚠  BOŞ CONDITION → Action: 'test11' (ID: 42, Status: Enabled)
2026-02-16 10:00:00 [INFO]    ✔ Action DISABLE edildi → 'test11' (ID: 42)
2026-02-16 10:00:00 [INFO] 📧 E-posta gönderildi → admin@example.com
```
