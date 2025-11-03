import os
import json
import time
import logging

import redis
from prometheus_client import start_http_server, Counter, Gauge

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_QUEUE = os.getenv("REDIS_QUEUE", "convert")

WORKER_ID = os.getenv("WORKER_ID", "worker_a")
METRICS_PORT = int(os.getenv("METRICS_PORT", "9102"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

jobs_in_progress = Gauge("worker_jobs_in_progress", "Current jobs in progress", ["worker_id"])
jobs_done_total = Counter("worker_jobs_done_total", "Jobs finished", ["worker_id"])
jobs_failed_total = Counter("worker_jobs_failed_total", "Jobs failed", ["worker_id"])


def get_redis_client() -> redis.Redis:
    client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB)
    client.ping()
    logger.info(f"Connected to Redis at {REDIS_HOST}:{REDIS_PORT} db={REDIS_DB}")
    return client


def process_job(job: dict) -> None:
    job_id = job.get("job_id", "unknown")
    target = job.get("target", "unknown")
    logger.info(f"[{WORKER_ID}] processing job {job_id} -> {target}")
    time.sleep(3)
    logger.info(f"[{WORKER_ID}] job {job_id} done")


def main() -> None:
    start_http_server(METRICS_PORT)
    logger.info(f"[{WORKER_ID}] metrics on :{METRICS_PORT}")

    while True:
        try:
            r = get_redis_client()
            break
        except Exception as e:
            logger.error(f"Redis connection failed: {e}")
            time.sleep(5)

    logger.info(f"[{WORKER_ID}] listening on queue '{REDIS_QUEUE}'")

    while True:
        try:
            item = r.blpop(REDIS_QUEUE, timeout=5)
            if item is None:
                continue

            _, data = item
            try:
                job = json.loads(data.decode("utf-8"))
            except json.JSONDecodeError:
                logger.error(f"[{WORKER_ID}] invalid JSON: {data}")
                jobs_failed_total.labels(worker_id=WORKER_ID).inc()
                continue

            jobs_in_progress.labels(worker_id=WORKER_ID).inc()
            try:
                process_job(job)
                jobs_done_total.labels(worker_id=WORKER_ID).inc()
            except Exception as e:
                logger.exception(f"[{WORKER_ID}] error: {e}")
                jobs_failed_total.labels(worker_id=WORKER_ID).inc()
            finally:
                jobs_in_progress.labels(worker_id=WORKER_ID).dec()

        except redis.ConnectionError as e:
            logger.error(f"[{WORKER_ID}] redis lost: {e}")
            time.sleep(5)
        except Exception as e:
            logger.exception(f"[{WORKER_ID}] loop error: {e}")
            time.sleep(2)


if __name__ == "__main__":
    main()