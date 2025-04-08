import psutil
import time
import requests
import json
import os
from datetime import datetime, timezone

# Ayarlar
FASTAPI_URL = "http://127.0.0.1:8000/internal/broadcast_alert"  # main.py backend
SECRET_TOKEN = "BURAYA_COK_GIZLI_BIR_TOKEN_YAZIN"  # main.py ile aynı olmalı
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "monitored_config.json")
HOSTNAME = os.uname().nodename  # Sunucu ismi
CHECK_INTERVAL = 10  # saniye

# Eşik Değerler
THRESHOLDS = {
    "cpu_percent": 90,
    "ram_percent": 90,
    "disk_percent": 90
}

# Önceki durumları takip etmek için
last_states = {
    "cpu": None,
    "ram": None,
    "disk": None,
    "apps": {}
}

def read_config():
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"Config okunamadı: {e}")
        return {"monitored_apps": []}

def send_alert(alert_type, status, metric=None, value=None, threshold=None, message=None):
    payload = {
        "server_name": HOSTNAME,
        "alert_type": alert_type,
        "status": status,
        "metric": metric,
        "value": value,
        "threshold": threshold,
        "message": message or "",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    try:
        response = requests.post(
            FASTAPI_URL,
            json=payload,
            headers={"X-Internal-Token": SECRET_TOKEN},
            timeout=5
        )
        print(f"🔔 {alert_type} ({status}) → {response.status_code}")
    except Exception as e:
        print(f"❌ Uyarı gönderilemedi: {e}")

def check_system_resources():
    # CPU
    cpu_percent = psutil.cpu_percent(interval=1)
    if cpu_percent > THRESHOLDS["cpu_percent"]:
        if last_states["cpu"] != "ALERT":
            send_alert("CPU", "ALERT", "cpu_percent", cpu_percent, THRESHOLDS["cpu_percent"], "CPU kullanımı yüksek")
            last_states["cpu"] = "ALERT"
    elif last_states["cpu"] == "ALERT" and cpu_percent < THRESHOLDS["cpu_percent"] - 5:
        send_alert("CPU", "RECOVERY", "cpu_percent", cpu_percent, THRESHOLDS["cpu_percent"], "CPU normale döndü")
        last_states["cpu"] = "RECOVERY"

    # RAM
    ram = psutil.virtual_memory()
    if ram.percent > THRESHOLDS["ram_percent"]:
        if last_states["ram"] != "ALERT":
            send_alert("RAM", "ALERT", "ram_percent", ram.percent, THRESHOLDS["ram_percent"], "RAM kullanımı yüksek")
            last_states["ram"] = "ALERT"
    elif last_states["ram"] == "ALERT" and ram.percent < THRESHOLDS["ram_percent"] - 5:
        send_alert("RAM", "RECOVERY", "ram_percent", ram.percent, THRESHOLDS["ram_percent"], "RAM normale döndü")
        last_states["ram"] = "RECOVERY"

    # Disk
    disk = psutil.disk_usage('/')
    if disk.percent > THRESHOLDS["disk_percent"]:
        if last_states["disk"] != "ALERT":
            send_alert("DISK", "ALERT", "disk_percent", disk.percent, THRESHOLDS["disk_percent"], "Disk kullanımı yüksek")
            last_states["disk"] = "ALERT"
    elif last_states["disk"] == "ALERT" and disk.percent < THRESHOLDS["disk_percent"] - 5:
        send_alert("DISK", "RECOVERY", "disk_percent", disk.percent, THRESHOLDS["disk_percent"], "Disk normale döndü")
        last_states["disk"] = "RECOVERY"

def check_apps():
    config = read_config()
    current_apps = {p.info['name']: p.info['pid'] for p in psutil.process_iter(['name', 'pid'])}

    for app in config.get("monitored_apps", []):
        is_running = any(app in name for name in current_apps)
        if is_running and last_states["apps"].get(app) == "DOWN":
            send_alert(f"APP_{app}", "RECOVERY", message=f"{app} tekrar çalışıyor")
            last_states["apps"][app] = "UP"
        elif not is_running and last_states["apps"].get(app) != "DOWN":
            send_alert(f"APP_{app}", "ALERT", message=f"{app} çalışmıyor")
            last_states["apps"][app] = "DOWN"

def main_loop():
    while True:
        check_system_resources()
        check_apps()
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    print("🟢 Monitoring agent başlatıldı.")
    main_loop()