import os
import re
import shutil
import subprocess
import tempfile
import zipfile

FRAME_PATTERN = re.compile(r"^frame_\d+\.jpg$")
DEFAULT_VIDEO_FPS = 24
VIDEO_FILENAME = "timelapse_export.mp4"


def ffmpeg_available():
    return shutil.which("ffmpeg") is not None


def list_session_frames(session_dir):
    if not os.path.isdir(session_dir):
        return []
    return sorted(
        f for f in os.listdir(session_dir)
        if FRAME_PATTERN.match(f)
    )


def create_timelapse_zip(session_dir):
    frames = list_session_frames(session_dir)
    if not frames:
        return None, "No frames to export"

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip", prefix="timelapse_")
    tmp.close()
    try:
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as archive:
            for frame_name in frames:
                archive.write(
                    os.path.join(session_dir, frame_name),
                    arcname=frame_name,
                )
            session_json = os.path.join(session_dir, "session.json")
            if os.path.isfile(session_json):
                archive.write(session_json, arcname="session.json")
        return tmp.name, None
    except Exception as e:
        if os.path.exists(tmp.name):
            os.remove(tmp.name)
        return None, str(e)


def create_timelapse_video(session_dir, fps=DEFAULT_VIDEO_FPS):
    frames = list_session_frames(session_dir)
    if not frames:
        return None, "No frames to export"

    if not ffmpeg_available():
        return None, "ffmpeg is not installed"

    output_path = os.path.join(session_dir, VIDEO_FILENAME)
    latest_frame_mtime = max(
        os.path.getmtime(os.path.join(session_dir, frame_name))
        for frame_name in frames
    )
    if os.path.isfile(output_path) and os.path.getmtime(output_path) >= latest_frame_mtime:
        return output_path, None

    first_frame_num = int(frames[0].split("_")[1].split(".")[0])
    input_pattern = os.path.join(session_dir, "frame_%06d.jpg")
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-framerate",
        str(fps),
        "-start_number",
        str(first_frame_num),
        "-i",
        input_pattern,
        "-frames:v",
        str(len(frames)),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        output_path,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "Video export timed out"
    except Exception as e:
        return None, str(e)

    if result.returncode != 0:
        if os.path.isfile(output_path):
            os.remove(output_path)
        return None, (result.stderr or "ffmpeg failed").strip()

    return output_path, None
