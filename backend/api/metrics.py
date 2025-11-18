# backend/api/metrics.py
from prometheus_client import Counter, Histogram, Gauge
import time
import psutil
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint


# --- Definición de Métricas ---
# (Basado en la checklist de la imagen)

# 1. Métrica de latencia (Histograma)
api_requests_latency_seconds = Histogram(
    "api_requests_latency_seconds",
    "Latencia de las peticiones a la API",
    ["method", "path", "status_code"]
)

# 2. Métrica de peticiones (Contador)
api_requests_total = Counter(
    "api_requests_total",
    "Total de peticiones a la API",
    ["method", "path", "status_code"]
)

# 3. Métrica de trabajos encolados (Contador)
api_jobs_enqueued_total = Counter(
    "api_jobs_enqueued_total",
    "Total de trabajos de conversión encolados",
    ["target_format"] # Ej: mp3, hls
)

# 4. Métrica de subidas de archivos (Contador)
api_media_uploads_total = Counter(
    "api_media_uploads_total",
    "Total de archivos subidos"
)

# 5. Métrica de tamaño de cola (Gauge)
# (Lo implementaremos en app.py usando la conexión a Redis de jobs.py)
from prometheus_client import Gauge
api_queue_size = Gauge(
    "api_queue_size",
    "Tamaño actual de la cola de conversión",
    ["queue_name"]
)

# --- Métricas de uso de recursos del sistema (CPU, RAM, red) ---

system_cpu_percent = Gauge(
    "system_cpu_percent",
    "Uso de CPU del proceso",
    ["service"],
)

system_memory_percent = Gauge(
    "system_memory_percent",
    "Uso de memoria del proceso",
    ["service"],
)

system_net_bytes_sent = Gauge(
    "system_net_bytes_sent",
    "Bytes enviados por la interfaz de red",
    ["service"],
)

system_net_bytes_recv = Gauge(
    "system_net_bytes_recv",
    "Bytes recibidos por la interfaz de red",
    ["service"],
)


def update_system_metrics(service_name: str = "api") -> None:
    """Actualiza métricas de CPU, RAM y red para el proceso actual."""
    cpu = psutil.cpu_percent(interval=0)
    mem = psutil.virtual_memory().percent
    net = psutil.net_io_counters()

    system_cpu_percent.labels(service=service_name).set(cpu)
    system_memory_percent.labels(service=service_name).set(mem)
    system_net_bytes_sent.labels(service=service_name).set(net.bytes_sent)
    system_net_bytes_recv.labels(service=service_name).set(net.bytes_recv)



# --- Middleware para tracking automático ---

class PrometheusMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start_time = time.time()
        
        # Excluir el endpoint de métricas de sí mismo
        if request.url.path == "/metrics":
            return await call_next(request)

        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception as e:
            status_code = 500
            raise e
        finally:
            end_time = time.time()
            latency = end_time - start_time
            
            path = request.url.path
            method = request.method
            
            # Actualizar métricas de API
            api_requests_latency_seconds.labels(
                method=method, path=path, status_code=status_code
            ).observe(latency)
            
            api_requests_total.labels(
                method=method, path=path, status_code=status_code
            ).inc()

            # <<< NUEVO: actualizar métricas de CPU / RAM / red del servicio API >>>
            update_system_metrics("api")

        return response
