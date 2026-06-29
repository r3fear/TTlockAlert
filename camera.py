import logging
import os
import subprocess
from datetime import datetime

logger = logging.getLogger("camera")


def capture_frame(rtsp_url: str, output_path: str, ffmpeg_path: str = "ffmpeg") -> bool:
    """Capture a single frame from an RTSP stream using ffmpeg.

    Uses TCP transport to improve reliability on SWANN DVRs.
    Returns True on success, False on any failure (process, timeout, not found).
    """
    cmd = [
        ffmpeg_path,
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-vframes", "1",
        "-update", "1",
        "-y",
        output_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            timeout=30,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            logger.error(
                "ffmpeg exited with code %d: %s",
                result.returncode,
                result.stderr.decode(errors="replace").strip(),
            )
            return False
        logger.info("Frame captured: %s", output_path)
        return True
    except FileNotFoundError:
        logger.error("ffmpeg not found at path: %s", ffmpeg_path)
        return False
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg timed out after 30s capturing from %s", rtsp_url)
        return False
    except Exception as e:
        logger.error("Unexpected error capturing frame: %s", e)
        return False


def get_photo_path(photos_dir: str, event_type: str) -> str:
    """Build path fotos/YYYY/MM/DD/tipo_HHMMSS.jpg and create parent directories."""
    now = datetime.now()
    date_dir = os.path.join(photos_dir, now.strftime("%Y"), now.strftime("%m"), now.strftime("%d"))
    os.makedirs(date_dir, exist_ok=True)
    filename = f"{event_type}_{now.strftime('%H%M%S')}.jpg"
    return os.path.join(date_dir, filename)


if __name__ == "__main__":
    import sys
    import yaml

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
    if not os.path.exists(config_path):
        logger.error("config.yaml not found — copy config.yaml.example and fill in your values")
        sys.exit(1)

    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    rtsp_url = config["camera"]["rtsp_url"]
    ffmpeg_path = config["camera"].get("ffmpeg_path", "ffmpeg")
    photos_dir = config["storage"]["photos_dir"]

    output_path = get_photo_path(photos_dir, "test")
    logger.info("Capturing test frame from %s", rtsp_url)
    logger.info("Output path: %s", output_path)

    ok = capture_frame(rtsp_url, output_path, ffmpeg_path)
    if ok:
        size = os.path.getsize(output_path)
        logger.info("Success — file size: %d bytes", size)
    else:
        logger.error("Capture failed")
        sys.exit(1)
