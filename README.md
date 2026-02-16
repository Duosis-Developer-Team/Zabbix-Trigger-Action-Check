# Zabbix Trigger Action Monitor & Backup

Zabbix 6.4 trigger action'larını kontrol eden iki script:

1. **`zabbix_action_monitor.py`** — Condition'u boş olan trigger action'ları otomatik **siler**
2. **`zabbix_action_backup.py`** — Tüm trigger action'ları JSON olarak **yedekler** ve geri yükler

## Problem

Zabbix 6.4'te bir trigger action'ın condition'ı silindiğinde action aktif kalırsa, **tüm alarmlar** tanımlı kullanıcılara gider. Bu scriptler bu durumu otomatik tespit edip önler ve güvenli yedekleme sağlar.

## Özellikler

- **Sıfır bağımlılık** — Sadece Python 3 standart kütüphanesi
- **API Token** veya **kullanıcı/şifre** ile kimlik doğrulama
- **Ortak config dosyası** — İki script de aynı `config.ini` dosyasını kullanır

---

## Kurulum

### 1. Dosyaları sunucuya kopyala

```bash
sudo mkdir -p /opt/zabbix-action-monitor
sudo cp zabbix_action_monitor.py zabbix_action_backup.py /opt/zabbix-action-monitor/
```

### 2. Config dosyasını oluştur

```bash
sudo cp config.ini.example /etc/zabbix/action_monitor.ini
sudo chmod 600 /etc/zabbix/action_monitor.ini
sudo nano /etc/zabbix/action_monitor.ini
```

---

## Monitor Script — `zabbix_action_monitor.py`

Condition'u boş olan trigger action'ları (enabled/disabled fark etmez) tespit edip **siler**.

### Kullanım

```bash
# Tek seferlik kontrol
python3 zabbix_action_monitor.py --config config.ini

# Sadece raporla, silme (dry-run)
python3 zabbix_action_monitor.py --config config.ini --dry-run

# Tüm action'ların durumunu raporla
python3 zabbix_action_monitor.py --config config.ini --report

# Daemon modunda sürekli çalış (5 dk aralıkla)
python3 zabbix_action_monitor.py --config config.ini --daemon --interval 300
```

### Cron ile çalıştırma (önerilen)

```bash
# Her dakika kontrol
* * * * * /usr/bin/python3 /opt/zabbix-action-monitor/zabbix_action_monitor.py --config /etc/zabbix/action_monitor.ini >> /var/log/zabbix_action_monitor.log 2>&1
```

### Parametreler

| Parametre     | Açıklama                         | Varsayılan |
|---------------|----------------------------------|------------|
| `--url`       | Zabbix frontend URL'i            | (zorunlu)  |
| `--user`      | Kullanıcı adı                    | -          |
| `--password`  | Şifre                            | -          |
| `--api-token` | API Token (user/password yerine) | -          |
| `--config`    | Config dosyası yolu              | -          |
| `--daemon`    | Daemon modunda çalış             | false      |
| `--interval`  | Kontrol aralığı (saniye)         | 300        |
| `--dry-run`   | Sadece raporla, silme            | false      |
| `--report`    | Tüm action'ları raporla          | false      |
| `--log-file`  | Log dosyası yolu                 | stdout     |
| `--debug`     | Debug log seviyesi               | false      |

---

## Backup Script — `zabbix_action_backup.py`

Tüm trigger action'ları detaylı JSON olarak yedekler. Geri yükleme (restore) desteği vardır.

### Yedek alma

```bash
# Varsayılan dizine (./backups/) yedekle
python3 zabbix_action_backup.py --config config.ini

# Belirtilen dizine yedekle
python3 zabbix_action_backup.py --config config.ini --output /backup/zabbix/

# Son 7 yedeği tut, eskilerini sil
python3 zabbix_action_backup.py --config config.ini --retain 7
```

### Geri yükleme

```bash
python3 zabbix_action_backup.py --config config.ini --restore ./backups/trigger_actions_20260216_080032.json
```

### Cron ile günlük yedek (son 30 gün tutulur)

```bash
0 2 * * * /usr/bin/python3 /opt/zabbix-action-monitor/zabbix_action_backup.py --config /etc/zabbix/action_monitor.ini --output /backup/zabbix_actions/ --retain 30 >> /var/log/zabbix_action_backup.log 2>&1
```

### Parametreler

| Parametre     | Açıklama                                   | Varsayılan  |
|---------------|--------------------------------------------|-------------|
| `--url`       | Zabbix frontend URL'i                      | (zorunlu)   |
| `--user`      | Kullanıcı adı                              | -           |
| `--password`  | Şifre                                      | -           |
| `--api-token` | API Token                                  | -           |
| `--config`    | Config dosyası yolu                        | -           |
| `--output`    | Yedek dosya yolu veya dizini               | `./backups` |
| `--restore`   | Bu dosyadan geri yükle                     | -           |
| `--retain`    | Tutulacak yedek sayısı (0=sınırsız)        | 0           |
| `--debug`     | Debug log seviyesi                         | false       |

---

## Önerilen Cron Konfigürasyonu

```bash
# Günde 1 kez yedek al (gece 02:00), son 30 yedeği tut
0 2 * * * /usr/bin/python3 /opt/zabbix-action-monitor/zabbix_action_backup.py --config /etc/zabbix/action_monitor.ini --output /backup/zabbix_actions/ --retain 30 >> /var/log/zabbix_action_backup.log 2>&1

# Dakikada 1 condition kontrolü yap
* * * * * /usr/bin/python3 /opt/zabbix-action-monitor/zabbix_action_monitor.py --config /etc/zabbix/action_monitor.ini >> /var/log/zabbix_action_monitor.log 2>&1
```

## Çıktı Örnekleri

### Monitor — sorun yok
```
2026-02-16 10:00:00 [INFO] Giriş başarılı.
2026-02-16 10:00:00 [INFO] Zabbix API versiyonu: 6.4.0
2026-02-16 10:00:00 [INFO] ✔ Tüm enabled trigger action'ların condition'ları mevcut. Sorun yok.
```

### Monitor — boş condition tespit
```
2026-02-16 10:00:00 [WARNING] ⚠  BOŞ CONDITION TESPİT EDİLDİ → Action: 'test11' (ID: 42)
2026-02-16 10:00:00 [INFO]    ✔ Action SİLİNDİ → 'test11' (ID: 42)
2026-02-16 10:00:00 [INFO] Toplam 1 adet boş condition'lı action silindi.
```

### Backup
```
2026-02-16 02:00:00 [INFO] ✔ 2 trigger action yedeklendi → /backup/trigger_actions_20260216_020000.json
2026-02-16 02:00:00 [INFO]   Dosya boyutu: 3.2 KB
2026-02-16 02:00:00 [INFO]   [Enabled] production-alerts (conditions: 3, operations: 2)
2026-02-16 02:00:00 [INFO]   [Disabled] test-action (conditions: 1, operations: 1)
```
