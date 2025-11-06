# backend/api/metrics.py
from prometheus_client import Counter, Histogram
import time
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
            
            # Actualizar métricas
            api_requests_latency_seconds.labels(
                method=method, path=path, status_code=status_code
            ).observe(latency)
            
            api_requests_total.labels(
                method=method, path=path, status_code=status_code
            ).inc()

        return response