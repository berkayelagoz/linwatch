from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Depends, Header
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
import subprocess
import os
import psutil
import uvicorn
import time
import json
from datetime import datetime, timezone

app = FastAPI()

# --- Güvenlik Ayarı ---
INTERNAL_SECRET_TOKEN = "BURAYA_COK_GIZLI_BIR_TOKEN_YAZIN"

# --- Global Değişkenler ---
active_connections: set[WebSocket] = set()
current_active_alerts: Dict[str, Dict[str, Any]] = {}
CONFIG_FILE_PATH = os.path.join(os.path.dirname(__file__), "monitored_config.json")

cached_processes_data = None
last_cache_time = 0
CACHE_TTL_SECONDS = 5

# --- Yardımcı Fonksiyonlar ---
def format_bytes(n):
    symbols = ('K', 'M', 'G', 'T', 'P', 'E', 'Z', 'Y')
    prefix = {}
    for i, s in enumerate(symbols):
        prefix[s] = 1 << (i + 1) * 10
    for s in reversed(symbols):
        if abs(n) >= prefix[s]:
            value = float(n) / prefix[s]
            return '%.1f%s' % (value, s)
    return "%sB" % n

def load_monitoring_config() -> Dict[str, List[str]]:
    try:
        if os.path.exists(CONFIG_FILE_PATH):
            with open(CONFIG_FILE_PATH, "r") as f:
                return json.load(f)
        else:
            return {"monitored_apps": [], "disabled_apps": []}
    except:
        return {"monitored_apps": [], "disabled_apps": []}

def save_monitoring_config(config_data: Dict[str, List[str]]) -> bool:
    try:
        with open(CONFIG_FILE_PATH, "w") as f:
            json.dump(config_data, f, indent=4)
        return True
    except:
        return False

async def send_json_to_websocket(websocket: WebSocket, data: dict):
    try:
        await websocket.send_json(data)
    except:
        active_connections.discard(websocket)

async def broadcast_message(data: dict):
    disconnected_clients = set()
    for connection in active_connections:
        try:
            await connection.send_json(data)
        except:
            disconnected_clients.add(connection)
    for client in disconnected_clients:
        active_connections.discard(client)

async def verify_internal_token(x_internal_token: Optional[str] = Header(None)):
    if x_internal_token != INTERNAL_SECRET_TOKEN:
        raise HTTPException(status_code=403, detail="Geçersiz veya eksik internal token")
    return True

# --- WebSocket ---
@app.websocket("/ws/notifications")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.add(websocket)

    # Mevcut alarmlar
    active_alerts_list = []
    for server_alerts in current_active_alerts.values():
        active_alerts_list.extend(list(server_alerts.values()))

    if active_alerts_list:
        await send_json_to_websocket(websocket, {
            "type": "current_alerts",
            "data": active_alerts_list
        })

    # Mevcut konfigürasyon
    current_config = load_monitoring_config()
    await send_json_to_websocket(websocket, {
        "type": "current_config",
        "data": current_config
    })

    try:
        while True:
            msg = await websocket.receive_text()
            print(f"📩 WebSocket mesajı alındı ({websocket.client}): {msg[:100]}...")
            try:
                data = json.loads(msg)
                msg_type = data.get("type")
                current_config = load_monitoring_config()

                if msg_type == "config_add":
                    app_to_add = data.get("data", {}).get("app")
                    if app_to_add and app_to_add not in current_config["monitored_apps"]:
                        current_config["monitored_apps"].append(app_to_add)
                        if save_monitoring_config(current_config):
                            await send_json_to_websocket(websocket, {
                                "type": "config_ack",
                                "success": True,
                                "message": f"'{app_to_add}' başarıyla eklendi."
                            })
                    else:
                        await send_json_to_websocket(websocket, {
                            "type": "config_ack",
                            "success": False,
                            "message": "Uygulama zaten mevcut veya geçersiz."
                        })

                elif msg_type == "config_remove":
                    app_to_remove = data.get("data", {}).get("app")
                    if app_to_remove in current_config["monitored_apps"]:
                        current_config["monitored_apps"].remove(app_to_remove)
                        if save_monitoring_config(current_config):
                            await send_json_to_websocket(websocket, {
                                "type": "config_ack",
                                "success": True,
                                "message": f"'{app_to_remove}' başarıyla kaldırıldı."
                            })
                    else:
                        await send_json_to_websocket(websocket, {
                            "type": "config_ack",
                            "success": False,
                            "message": "Uygulama listede bulunamadı."
                        })

                else:
                    await send_json_to_websocket(websocket, {
                        "type": "config_ack",
                        "success": False,
                        "message": "Geçersiz mesaj türü."
                    })

            except json.JSONDecodeError:
                print(f"⚠️ Geçersiz JSON alındı ({websocket.client}): {msg[:100]}...")
                continue
            except Exception as e:
                print(f"💥 WebSocket mesaj işleme hatası ({websocket.client}): {e}")
                await send_json_to_websocket(websocket, {"type": "error", "message": "İşlem sırasında hata oluştu."})

    except WebSocketDisconnect:
        print(f"❌ WebSocket bağlantısı koptu: {websocket.client}")
    finally:
        active_connections.discard(websocket)
        print(f"🔌 WebSocket bağlantısı kapatıldı: {websocket.client}")

# --- Alert Endpoint ---
@app.post("/internal/broadcast_alert", dependencies=[Depends(verify_internal_token)])
async def broadcast_alert_internal(alert_data: dict):
    server_name = alert_data.get("server_name")
    alert_type = alert_data.get("alert_type")
    status = alert_data.get("status", "ALERT")

    if not server_name or not alert_type:
        raise HTTPException(status_code=400, detail="Eksik bilgi")

    if status == "ALERT":
        if server_name not in current_active_alerts:
            current_active_alerts[server_name] = {}
        alert_data["received_timestamp"] = datetime.now(timezone.utc).isoformat()
        current_active_alerts[server_name][alert_type] = alert_data
    elif status == "RECOVERY":
        if server_name in current_active_alerts and alert_type in current_active_alerts[server_name]:
            del current_active_alerts[server_name][alert_type]
            if not current_active_alerts[server_name]:
                del current_active_alerts[server_name]

    await broadcast_message({"type": "realtime_alert", "data": alert_data})
    return {"status": "ok", "message": "Alert işlendi"}

# --- Resources Endpoint ---
@app.get("/resources")
def get_resources():
    global cached_processes_data, last_cache_time

    current_time = time.time()
    cpu_percent = psutil.cpu_percent(interval=None)
    per_cpu_percent = psutil.cpu_percent(interval=None, percpu=True)
    load_avg = psutil.getloadavg()

    ram = psutil.virtual_memory()
    swap = psutil.swap_memory()
    ram_data = {
        "total": format_bytes(ram.total),
        "used": format_bytes(ram.used),
        "available": format_bytes(ram.available),
        "free": format_bytes(ram.free),
        "percent": ram.percent
    }
    swap_data = {
        "total": format_bytes(swap.total),
        "used": format_bytes(swap.used),
        "free": format_bytes(swap.free),
        "percent": swap.percent
    } if swap.total > 0 else None

    disks = []
    for part in psutil.disk_partitions():
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append({
                "mount": part.mountpoint,
                "total": format_bytes(usage.total),
                "used": format_bytes(usage.used),
                "free": format_bytes(usage.free),
                "percent": usage.percent
            })
        except PermissionError:
            continue

    net = psutil.net_io_counters()
    net_data = {
        "total_download": format_bytes(net.bytes_recv),
        "total_upload": format_bytes(net.bytes_sent),
        "bytes_recv": net.bytes_recv,
        "bytes_sent": net.bytes_sent
    }

    temp_data = {"unit": "°C"}
    try:
        temps = psutil.sensors_temperatures()
        if "coretemp" in temps:
            for t in temps["coretemp"]:
                label = t.label or "core"
                temp_data[label] = t.current
    except:
        temp_data = {"unit": "°C"}

    if cached_processes_data and (current_time - last_cache_time < CACHE_TTL_SECONDS):
        top_cpu = cached_processes_data['top_cpu']
        top_memory = cached_processes_data['top_memory']
    else:
        processes = []
        for proc in psutil.process_iter(['pid', 'name', 'username', 'memory_info', 'cpu_percent']):
            try:
                proc_info = proc.info
                processes.append({
                    "pid": proc_info['pid'],
                    "name": proc_info['name'],
                    "user": proc_info['username'],
                    "memory": format_bytes(proc_info['memory_info'].rss) if proc_info.get('memory_info') else 'N/A',
                    "memory_raw": proc_info['memory_info'].rss if proc_info.get('memory_info') else 0,
                    "cpu_percent": proc_info['cpu_percent'] if proc_info.get('cpu_percent') is not None else 0.0
                })
            except:
                continue
        top_cpu = sorted(processes, key=lambda p: p.get('cpu_percent', 0), reverse=True)[:20]
        top_memory = sorted(processes, key=lambda p: p.get('memory_raw', 0), reverse=True)[:20]
        cached_processes_data = {'top_cpu': top_cpu, 'top_memory': top_memory}
        last_cache_time = current_time

    return {
        "cpu": {
            "percent": cpu_percent,
            "percpu_percent": per_cpu_percent,
            "load_avg": {
                "1min": load_avg[0],
                "5min": load_avg[1],
                "15min": load_avg[2]
            }
        },
        "ram": ram_data,
        "swap": swap_data,
        "disks": disks,
        "network": net_data,
        "temperature": temp_data,
        "top_cpu_processes": top_cpu,
        "top_memory_processes": top_memory
    }

# --- Logs Endpoint ---
class LogRequest(BaseModel):
    app_name: str
    app_type: str

@app.post("/logs")
def get_logs(req: LogRequest):
    app_name = req.app_name
    app_type = req.app_type.lower()
    lines = []

    try:
        if app_type == "systemd":
            cmd = ["journalctl", "-u", f"{app_name}.service", "-n", "50", "--no-pager"]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            lines = result.stdout.strip().split("\n")
        elif app_type == "docker":
            cmd = ["docker", "logs", "--tail", "50", app_name]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            lines = result.stdout.strip().split("\n")
        elif app_type == "custom":
            log_path = f"/var/log/{app_name}.log"
            if not os.path.isfile(log_path):
                raise HTTPException(status_code=404, detail="Log dosyası bulunamadı.")
            with open(log_path, "r") as f:
                all_lines = f.readlines()
                lines = all_lines[-50:]
        else:
            raise HTTPException(status_code=400, detail="Geçersiz uygulama türü")

        return {
            "app": app_name,
            "type": app_type,
            "lines": [line.strip() for line in lines if line.strip()]
        }

    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Komut hatası: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Hata: {str(e)}")

if __name__ == "__main__":
    if not os.path.exists(CONFIG_FILE_PATH):
        save_monitoring_config({"monitored_apps": [], "disabled_apps": []})
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
