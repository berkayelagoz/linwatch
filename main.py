from fastapi import FastAPI
import psutil

app = FastAPI()

@app.get("/resources")
def get_system_resources():
    cpu = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory().percent
    disk = psutil.disk_usage('/').percent
    net = psutil.net_io_counters()
    net_data = {
        "bytes_sent": net.bytes_sent,
        "bytes_recv": net.bytes_recv
    }

    return {
        "cpu": cpu,
        "ram": ram,
        "disk": disk,
        "network": net_data
    }
