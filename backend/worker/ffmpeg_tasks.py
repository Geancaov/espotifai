import os
import subprocess
from pathlib import Path


def run_ffmpeg(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)


def convert_to_mp3(input_path: str, output_path: str) -> str:
    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_path,
        "-vn",
        "-acodec", "libmp3lame",
        "-q:a", "2",
        output_path,
    ]
    run_ffmpeg(cmd)
    return output_path


def convert_to_mp4_h264(input_path: str, output_path: str) -> str:
    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_path,
        "-c:v", "libx264",
        "-c:a", "aac",
        "-strict", "experimental",
        output_path,
    ]
    run_ffmpeg(cmd)
    return output_path


def convert_to_hls(input_path: str, output_dir: str, playlist_name: str = "index.m3u8") -> str:
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    playlist_path = os.path.join(output_dir, playlist_name)
    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_path,
        "-codec:", "copy",
        "-c", "copy",
        "-start_number", "0",
        "-hls_time", "5",
        "-hls_list_size", "0",
        "-f", "hls",
        playlist_path,
    ]
    run_ffmpeg(cmd)
    return playlist_path