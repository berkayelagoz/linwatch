
import subprocess
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Any
import psutil
import os
import time
import json
import asyncio
import uvicorn
from datetime import datetime, timezone

app = FastAPI()

# --- Güvenlik ve İzleme Yapıları ---
active_connections: set[WebSocket] = set()
current_active_alerts: Dict[str, Dict[str, Any]] = {}
monitored_apps: List[str] = []
alert_history: List[Dict[str, Any]] = []
last_states = {
    "cpu": None,
    "ram": None,
    "disk": None,
    "temperature": None,
    "apps": {}
}
THRESHOLDS = {
    "cpu_percent": 2,
    "ram_percent": 20,
    "disk_percent": 5,
    "temperature": 85
}

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

async def send_json_to_websocket(websocket: WebSocket, data: dict):
    try:
        await websocket.send_json(data)
    except Exception:
        active_connections.discard(websocket)

async def broadcast_message(data: dict):
    disconnected_clients = set()
    for connection in active_connections:
        try:
            await connection.send_json(data)
        except Exception:
            disconnected_clients.add(connection)
    for client in disconnected_clients:
        active_connections.discard(client)

# --- WebSocket ---
@app.websocket("/ws/notifications")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.add(websocket)

    active_alerts_list = []
    for server_alerts in current_active_alerts.values():
        active_alerts_list.extend(list(server_alerts.values()))

    if active_alerts_list:
        await send_json_to_websocket(websocket, {
            "type": "current_alerts",
            "data": active_alerts_list
        })

    await send_json_to_websocket(websocket, {
        "type": "current_config",
        "data": {"monitored_apps": monitored_apps}
    })

    try:
        while True:
            msg = await websocket.receive_text()
            try:
                data = json.loads(msg)
                msg_type = data.get("type")

                if msg_type == "config_add":
                    app_name = data.get("data", {}).get("app")
                    if app_name and app_name not in monitored_apps:
                        monitored_apps.append(app_name)
                        await send_json_to_websocket(websocket, {
                            "type": "config_ack",
                            "success": True,
                            "message": f"{app_name} eklendi"
                        })
                    for conn in active_connections:
                        await send_json_to_websocket(conn, {
                            "type": "current_config",
                            "data": {"monitored_apps": monitored_apps}
                        })

                elif msg_type == "config_remove":
                    app_name = data.get("data", {}).get("app")
                    if app_name in monitored_apps:
                        monitored_apps.remove(app_name)
                        await send_json_to_websocket(websocket, {
                            "type": "config_ack",
                            "success": True,
                            "message": f"{app_name} kaldırıldı"
                        })
                        for conn in active_connections:
                            await send_json_to_websocket(conn, {
                            "type": "current_config",
                            "data": {"monitored_apps": monitored_apps}
                            })
                elif msg_type == "get_history":
                    await send_json_to_websocket(websocket, {
                        "type": "alert_history",
                        "data": alert_history
                    })  
                elif msg_type == "clear_history":
                    alert_history.clear()
                    await send_json_to_websocket(websocket, {
                        "type": "clear_ack",
                        "message": "Alarm geçmişi temizlendi"
                    })          
            except json.JSONDecodeError:
                continue
    except WebSocketDisconnect:
        pass
    finally:
        active_connections.discard(websocket)

# --- Monitoring ---
async def monitoring_loop():
    while True:
        await check_system_resources()
        await check_apps()
        await asyncio.sleep(2)

async def check_system_resources():
    cpu_percent = psutil.cpu_percent(interval=1)
    if cpu_percent > THRESHOLDS["cpu_percent"]:
        if last_states["cpu"] != "ALERT":
            await send_alert("CPU", "ALERT", "cpu_percent", cpu_percent, THRESHOLDS["cpu_percent"], "CPU yüksek")
            last_states["cpu"] = "ALERT"
    elif last_states["cpu"] == "ALERT" and cpu_percent < THRESHOLDS["cpu_percent"] - 5:
        await send_alert("CPU", "RECOVERY", "cpu_percent", cpu_percent, THRESHOLDS["cpu_percent"], "CPU normale döndü")
        last_states["cpu"] = "RECOVERY"

    ram = psutil.virtual_memory()
    if ram.percent > THRESHOLDS["ram_percent"]:
        if last_states["ram"] != "ALERT":
            await send_alert("RAM", "ALERT", "ram_percent", ram.percent, THRESHOLDS["ram_percent"], "RAM yüksek")
            last_states["ram"] = "ALERT"
    elif last_states["ram"] == "ALERT" and ram.percent < THRESHOLDS["ram_percent"] - 5:
        await send_alert("RAM", "RECOVERY", "ram_percent", ram.percent, THRESHOLDS["ram_percent"], "RAM normale döndü")
        last_states["ram"] = "RECOVERY"

    disk = psutil.disk_usage('/')
    if disk.percent > THRESHOLDS["disk_percent"]:
        if last_states["disk"] != "ALERT":
            await send_alert("DISK", "ALERT", "disk_percent", disk.percent, THRESHOLDS["disk_percent"], "Disk yüksek")
            last_states["disk"] = "ALERT"
    elif last_states["disk"] == "ALERT" and disk.percent < THRESHOLDS["disk_percent"] - 5:
        await send_alert("DISK", "RECOVERY", "disk_percent", disk.percent, THRESHOLDS["disk_percent"], "Disk normale döndü")
        last_states["disk"] = "RECOVERY"
    
    try:
        temps = psutil.sensors_temperatures()
        if "coretemp" in temps and temps["coretemp"]:
            temp = temps["coretemp"][0].current
            if temp > THRESHOLDS["temperature"]:
                if last_states.get("temperature") != "ALERT":
                    await send_alert("TEMPERATURE", "ALERT", "temperature", temp, THRESHOLDS["temperature"], "Sıcaklık yüksek")
                    last_states["temperature"] = "ALERT"
            elif last_states.get("temperature") == "ALERT" and temp < THRESHOLDS["temperature"] - 5:
                await send_alert("TEMPERATURE", "RECOVERY", "temperature", temp, THRESHOLDS["temperature"], "Sıcaklık normale döndü")
                last_states["temperature"] = "RECOVERY"
    except Exception as e:
        print(f"No temperature data: {e}")

async def check_apps():
    current_apps = {p.info['name']: p.info['pid'] for p in psutil.process_iter(['name', 'pid'])}
    for app in monitored_apps:
        is_running = any(app in name for name in current_apps)
        if is_running and last_states["apps"].get(app) == "DOWN":
            await send_alert(f"APP_{app}", "RECOVERY", message=f"{app} tekrar çalışıyor")
            last_states["apps"][app] = "UP"
        elif not is_running and last_states["apps"].get(app) != "DOWN":
            await send_alert(f"APP_{app}", "ALERT", message=f"{app} çalışmıyor")
            last_states["apps"][app] = "DOWN"

async def send_alert(alert_type, status, metric=None, value=None, threshold=None, message=None):
    alert_data = {
        "server_name": os.uname().nodename,
        "alert_type": alert_type,
        "status": status,
        "metric": metric,
        "value": value,
        "threshold": threshold,
        "message": message or "",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    server_name = alert_data["server_name"]

    alert_history.append(alert_data)

    # Aktif alarm listesini güncelle (ayrıca)
    if status == "ALERT":
        if server_name not in current_active_alerts:
            current_active_alerts[server_name] = {}
        current_active_alerts[server_name][alert_type] = alert_data
    elif status == "RECOVERY":
        if server_name in current_active_alerts and alert_type in current_active_alerts[server_name]:
            del current_active_alerts[server_name][alert_type]
            if not current_active_alerts[server_name]:
                del current_active_alerts[server_name]

    await broadcast_message({"type": "realtime_alert", "data": alert_data})

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
    try:
        usage = psutil.disk_usage('/')
        disks.append({
            "mount": "/",
            "total": format_bytes(usage.total),
            "used": format_bytes(usage.used),
            "free": format_bytes(usage.free),
            "percent": usage.percent
        })
    except Exception as e:
        print(f"Disk bilgisi alınamadı: {e}")

    net = psutil.net_io_counters()
    net_data = {
        "total_download": format_bytes(net.bytes_recv),
        "total_upload": format_bytes(net.bytes_sent),
        "bytes_recv": net.bytes_recv,
        "bytes_sent": net.bytes_sent
    }

    temp_data = None

    try:
        temps = psutil.sensors_temperatures()
        if "coretemp" in temps and temps["coretemp"]:
            temp_data = temps["coretemp"][0].current
    except:
        temp_data = None

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
                "onemin": load_avg[0],
                "fivemin": load_avg[1],
                "fifteenmin": load_avg[2]
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
            log_path = f"/var/log/{app_name}"
            cmd = ["tail", "-n", "50", log_path]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            lines = result.stdout.strip().split("\n")
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


# --- Startup ---
@app.on_event("startup")
async def start_monitoring():
    asyncio.create_task(monitoring_loop())

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
