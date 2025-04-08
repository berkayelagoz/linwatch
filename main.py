from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import subprocess
import os
import psutil
import uvicorn
import time

app = FastAPI()

# --- Önbellek için global değişkenler ---
cached_processes_data = None # Önbelleğe alınmış işlem verilerini tutacak
last_cache_time = 0          # Önbelleğin en son ne zaman güncellendiğini tutacak
CACHE_TTL_SECONDS = 5        # Önbelleğin geçerlilik süresi (saniye cinsinden). 5 saniye iyi bir başlangıç olabilir.

cached_processes_data = None # Önbelleğe alınmış işlem verilerini tutacak
last_cache_time = 0          # Önbelleğin en son ne zaman güncellendiğini tutacak
CACHE_TTL_SECONDS = 5        # Önbelleğin geçerlilik süresi (saniye cinsinden). 5 saniye iyi bir başlangıç olabilir.

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

    global cached_processes_data, last_cache_time # Global değişkenleri kullanacağımızı belirtiyoruz

    current_time = time.time()

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
    # Önbelleği kontrol et
    if cached_processes_data and (current_time - last_cache_time < CACHE_TTL_SECONDS):
        print("Process list cache HIT") # Konsolda görmek için (opsiyonel)
        top_cpu = cached_processes_data['top_cpu']
        top_memory = cached_processes_data['top_memory']
    else:
        print("Process list cache MISS or EXPIRED - Recalculating...") # Konsolda görmek için (opsiyonel)
        processes = []
        # interval=0.1 ile CPU yüzdesini almak daha anlamlı olabilir, ama iterasyon yavaşlar.
        # Interval olmadan almak daha hızlıdır ama ilk çağrıda %0 verebilir. Burada interval'siz devam edelim.
        for proc in psutil.process_iter(['pid', 'name', 'username', 'memory_info', 'cpu_percent']):
            try:
                # process_iter CPU yüzdesini hesaplarken de interval kullanabilir.
                # Eğer yukarıdaki cpu_percent çağrılarında interval varsa, buradaki cpu_percent
                # biraz daha eski olabilir. Genelde bu fark ihmal edilebilir.
                proc_info = proc.info
                processes.append({
                    "pid": proc_info['pid'],
                    "name": proc_info['name'],
                    "user": proc_info['username'],
                    "memory": format_bytes(proc_info['memory_info'].rss) if proc_info.get('memory_info') else 'N/A',
                    "memory_raw": proc_info['memory_info'].rss if proc_info.get('memory_info') else 0,
                    "cpu_percent": proc_info['cpu_percent'] if proc_info.get('cpu_percent') is not None else 0.0
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception as e: # Diğer olası hataları yakala
                print(f"Could not get info for process {proc.pid if hasattr(proc, 'pid') else 'N/A'}: {e}")
                continue

        # Sıralama
        # Bellek bilgisi olmayanları filtrele veya sona at (isteğe bağlı)
        top_cpu = sorted(processes, key=lambda p: p.get('cpu_percent', 0), reverse=True)[:20]
        top_memory = sorted(processes, key=lambda p: p.get('memory_raw', 0), reverse=True)[:20]

        # Önbelleği güncelle
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