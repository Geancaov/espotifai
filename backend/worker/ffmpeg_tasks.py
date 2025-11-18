# backend/api/worker/ffmpeg_tasks.py
import os
import subprocess
from pathlib import Path


def run_ffmpeg(cmd: list[str]) -> None:
    """Ejecuta ffmpeg y lanza error si falla."""
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed with code {proc.returncode}\nCOMMAND: {' '.join(cmd)}\nSTDERR:\n{proc.stderr}"
        )


def convert_to_mp3(input_path: str, output_path: str) -> str:
    """
    Convierte cualquier audio de entrada (wav, mp3, flac, ogg, etc.) a MP3.
    No hace copy, siempre recodifica para que sea seguro.
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",               # sobreescribir
        "-i", input_path,   # entrada
        "-vn",              # sin video
        "-ar", "44100",     # sample rate
        "-ac", "2",         # 2 canales
        "-b:a", "192k",     # bitrate de audio
        out.as_posix(),
    ]
    run_ffmpeg(cmd)
    return out.as_posix()


def convert_to_mp4_h264(input_path: str, output_path: str) -> str:
    """
    Convierte a MP4 con video H.264 y audio AAC.
    Funciona con entradas de video comunes (mp4, mkv, mov, etc.)
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_path,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        out.as_posix(),
    ]
    run_ffmpeg(cmd)
    return out.as_posix()


def convert_to_hls(input_path: str, output_dir: str, playlist_name: str = "index.m3u8") -> str:
    """
    Convierte el video a HLS (lista .m3u8 + segmentos .ts) en el directorio indicado.
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    playlist_path = out_dir / playlist_name

    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_path,
        "-c:v", "libx264",
        "-c:a", "aac",
        "-start_number", "0",
        "-hls_time", "5",
        "-hls_list_size", "0",
        "-f", "hls",
        playlist_path.as_posix(),
    ]
    run_ffmpeg(cmd)
    return playlist_path.as_posix()
