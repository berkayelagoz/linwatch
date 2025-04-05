from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import subprocess
import os
import psutil

app = FastAPI()

def format_bytes(n):
    # http://code.activestate.com/recipes/578019
    # >>> bytes2human(10000)
    # '9.8K'
    # >>> bytes2human(100001221)
    # '95.4M'
    symbols = ('K', 'M', 'G', 'T', 'P', 'E', 'Z', 'Y')
    prefix = {}
    for i, s in enumerate(symbols):
        prefix[s] = 1 << (i + 1) * 10
    for s in reversed(symbols):
        if abs(n) >= prefix[s]:
            value = float(n) / prefix[s]
            return '%.1f%s' % (value, s)
    return "%sB" % n

@app.get("/resources")
def get_resources():
    # CPU
    cpu_percent = psutil.cpu_percent(interval=None)
    per_cpu_percent = psutil.cpu_percent(interval=None, percpu=True)
    load_avg = psutil.getloadavg()
    
    # RAM
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

    # Disk
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

    # Network
    net = psutil.net_io_counters()
    net_data = {
        "total_download": format_bytes(net.bytes_recv),
        "total_upload": format_bytes(net.bytes_sent),
        "bytes_recv": net.bytes_recv,
        "bytes_sent": net.bytes_sent
    }

    # Temperature
    temp_data = {"unit": "°C"}
    try:
        temps = psutil.sensors_temperatures()
        if "coretemp" in temps:
            for t in temps["coretemp"]:
                label = t.label or "core"
                temp_data[label] = t.current
    except Exception:
        temp_data = {"unit": "°C"}

    # Processes
    processes = []
    for proc in psutil.process_iter(['pid', 'name', 'username', 'memory_info', 'cpu_percent']):
        try:
            processes.append({
                "pid": proc.info['pid'],
                "name": proc.info['name'],
                "user": proc.info['username'],
                "memory": format_bytes(proc.info['memory_info'].rss),
                "memory_raw": proc.info['memory_info'].rss,
                "cpu_percent": proc.info['cpu_percent']
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    top_cpu = sorted(processes, key=lambda p: p['cpu_percent'], reverse=True)[:20]
    top_memory = sorted(processes, key=lambda p: p['memory_raw'], reverse=True)[:20]

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

class LogRequest(BaseModel):
    app_name: str
    app_type: str  # "systemd", "docker", "custom"

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
            raise HTTPException(status_code=400, detail="Geçersiz uygulama türü (systemd, docker, custom)")

        return {
            "app": app_name,
            "type": app_type,
            "lines": [line.strip() for line in lines if line.strip()]
        }

    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Komut çalıştırılamadı: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Hata: {str(e)}")