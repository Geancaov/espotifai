import os
import json
import time
import logging
from pathlib import Path

import redis
from prometheus_client import start_http_server, Counter, Gauge

from ffmpeg_tasks import (
    convert_to_mp3,
    convert_to_mp4_h264,
    convert_to_hls,
)
from minio_client import download_object, upload_object

from api.firebase_db import (
    mark_media_job_processing,
    mark_media_job_done,
    mark_media_job_failed,
)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_QUEUE = os.getenv("REDIS_QUEUE", "convert")

WORKER_ID = os.getenv("WORKER_ID", "worker_a")
METRICS_PORT = int(os.getenv("METRICS_PORT", "9102"))

OUTPUT_BASE_DIR = os.getenv("OUTPUT_BASE_DIR", "/tmp/media_jobs")

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


def get_local_input(job_id: str, job: dict) -> str:
    local_path = job.get("local_path") or job.get("source_path")
    if local_path:
        return local_path

    source_bucket = job.get("source_bucket")
    source_object = job.get("source_object")
    if source_bucket and source_object:
        dest_dir = Path(OUTPUT_BASE_DIR) / job_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        ext = Path(source_object).suffix or ".bin"
        dest_path = dest_dir / f"input{ext}"
        return download_object(source_bucket, source_object, str(dest_path))

    raise ValueError("job must include local_path/source_path or source_bucket/source_object")


def upload_result_if_needed(job: dict, job_id: str, local_path: str, is_hls: bool = False) -> None:
    output_bucket = job.get("output_bucket")
    output_prefix = job.get("output_prefix")  # p.ej. "converted/<media_id>/<job_id>"

    if not output_bucket or not output_prefix:
        return

    if not is_hls:
        ext = Path(local_path).suffix
        object_name = f"{output_prefix}{ext}"
        upload_object(output_bucket, object_name, local_path)
        return

    hls_dir = Path(local_path).parent  
    for item in hls_dir.iterdir():
        if item.is_file():
            object_name = f"{output_prefix}/{item.name}"
            upload_object(output_bucket, object_name, str(item))



def process_job(job: dict) -> str:
    job_id = job.get("job_id", "unknown")
    target = job.get("target")
    if not target:
        raise ValueError("job must include 'target'")

    src_path = get_local_input(job_id, job)

    out_dir = Path(OUTPUT_BASE_DIR) / job_id
    out_dir.mkdir(parents=True, exist_ok=True)

    if target == "mp3":
        out_file = out_dir / f"{job_id}.mp3"
        convert_to_mp3(src_path, str(out_file))
        upload_result_if_needed(job, job_id, str(out_file), is_hls=False)
        return str(out_file)

    if target == "mp4":
        out_file = out_dir / f"{job_id}.mp4"
        convert_to_mp4_h264(src_path, str(out_file))
        upload_result_if_needed(job, job_id, str(out_file), is_hls=False)
        return str(out_file)

    if target == "hls":
        hls_out_dir = out_dir / "hls"
        playlist_path = convert_to_hls(src_path, str(hls_out_dir))
        upload_result_if_needed(job, job_id, playlist_path, is_hls=True)
        return playlist_path

    raise ValueError(f"unsupported target: {target}")


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
                job_id = job.get("job_id")
                media_id = job.get("media_id")

                # ---- estado: processing ----
                if media_id and job_id:
                    mark_media_job_processing(media_id, job_id)

                output_path = process_job(job)
                logger.info(f"[{WORKER_ID}] job finished, output at: {output_path}")

                # ---- estado: done ----
                if media_id and job_id:
                    mark_media_job_done(media_id, job_id, output_prefix=job.get("output_prefix"))

                jobs_done_total.labels(worker_id=WORKER_ID).inc()
            except Exception as e:
                logger.exception(f"[{WORKER_ID}] job failed: {e}")
                jobs_failed_total.labels(worker_id=WORKER_ID).inc()
                try:
                    if media_id and job_id:
                        mark_media_job_failed(media_id, job_id, details=str(e))
                except Exception:
                    pass
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