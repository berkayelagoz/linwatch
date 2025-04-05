from fastapi import FastAPI
import psutil

app = FastAPI()

def format_bytes(size_bytes):
    tb = 1024 ** 4
    gb = 1024 ** 3
    mb = 1024 ** 2

    if size_bytes >= tb:
        return f"{round(size_bytes / tb, 2)} TB"
    elif size_bytes >= gb:
        return f"{round(size_bytes / gb, 2)} GB"
    else:
        return f"{round(size_bytes / mb, 2)} MB"

@app.get("/resources")
def get_resources():
    # CPU
    cpu_percent = psutil.cpu_percent(interval=1, percpu=True)
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
        "total_download_tb": round(net.bytes_recv / (1024**4), 2),
        "total_upload_tb": round(net.bytes_sent / (1024**4), 2),
        "bytes_recv": net.bytes_recv,
        "bytes_sent": net.bytes_sent
    }

    # Temperature
    temp_data = {}
    try:
        temps = psutil.sensors_temperatures()
        if "coretemp" in temps:
            for t in temps["coretemp"]:
                label = t.label or "core"
                temp_data[label] = t.current
    except Exception:
        temp_data = {}

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
            "percpu_percent": cpu_percent,
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
