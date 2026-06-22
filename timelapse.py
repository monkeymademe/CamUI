import json
import os
import re
import threading
import time
import traceback


class TimelapseSession:
    def __init__(self, session_id, camera_num, settings, output_dir):
        self.session_id = session_id
        self.camera_num = camera_num
        self.settings = settings
        self.output_dir = output_dir
        self.status = "running"
        self.frame_count = 0
        self.started_at = time.time()
        self.last_error = None
        self._stop_event = threading.Event()
        self._thread = None

    def to_dict(self):
        return {
            "session_id": self.session_id,
            "camera_num": self.camera_num,
            "status": self.status,
            "frame_count": self.frame_count,
            "started_at": self.started_at,
            "elapsed_seconds": int(time.time() - self.started_at),
            "settings": self.settings,
            "output_dir": self.output_dir,
            "last_error": self.last_error,
        }

    def save_metadata(self):
        with open(os.path.join(self.output_dir, "session.json"), "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)


class TimelapseManager:
    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()

    def get_session(self, camera_num):
        with self._lock:
            return self._sessions.get(camera_num)

    def is_running(self, camera_num):
        session = self.get_session(camera_num)
        if session is None:
            return False
        if session.status in ("running", "stopping"):
            if session._thread and not session._thread.is_alive():
                session.status = "error"
                session.last_error = session.last_error or "Timelapse worker stopped unexpectedly"
                session.save_metadata()
                return False
            return True
        return False

    def start(self, camera, settings, timelapse_root):
        camera_num = camera.camera_info["Num"]
        if self.is_running(camera_num):
            raise ValueError("Timelapse already running on this camera")

        session_id = self._make_session_id(camera_num, settings.get("session_name"))
        output_dir = os.path.join(timelapse_root, session_id)
        os.makedirs(output_dir, exist_ok=True)

        session = TimelapseSession(session_id, camera_num, settings, output_dir)
        with self._lock:
            self._sessions[camera_num] = session

        session._thread = threading.Thread(
            target=self._run_loop,
            args=(camera, session),
            daemon=True,
        )
        session._thread.start()
        return session

    def stop(self, camera_num):
        session = self.get_session(camera_num)
        if not session or session.status not in ("running", "stopping"):
            raise ValueError("No timelapse running on this camera")
        session.status = "stopping"
        session._stop_event.set()
        if session._thread and session._thread.is_alive():
            session._thread.join(timeout=60)
        if session.status in ("running", "stopping"):
            session.status = "complete"
        session.save_metadata()
        return session

    def _make_session_id(self, camera_num, name):
        slug = re.sub(r"[^\w\-]", "_", (name or "").strip())[:32]
        ts = int(time.time())
        if slug:
            return f"tl_cam{camera_num}_{slug}_{ts}"
        return f"tl_cam{camera_num}_{ts}"

    def _run_loop(self, camera, session):
        settings = session.settings
        interval = max(1, int(settings.get("interval_seconds", 10)))
        max_frames = int(settings.get("max_frames", 0))
        capture_mode = settings.get("capture_mode", "full")
        freeze_settings = settings.get("freeze_settings", True)

        try:
            if freeze_settings:
                with camera.capture_lock:
                    camera.apply_controls_to_hardware()
            with camera.capture_lock:
                camera.use_placeholder = True
                camera.stop_streaming()

            session.save_metadata()
            print(f"Timelapse started: {session.session_id} (mode={capture_mode}, interval={interval}s)")
            next_tick = time.monotonic()

            while not session._stop_event.is_set():
                if max_frames > 0 and session.frame_count >= max_frames:
                    break

                frame_num = session.frame_count + 1
                filepath_base = os.path.join(
                    session.output_dir, f"frame_{frame_num:06d}"
                )

                with camera.capture_lock:
                    path = camera.capture_timelapse_frame(
                        session.camera_num,
                        filepath_base,
                        capture_mode=capture_mode,
                        keep_stream=False,
                    )

                if not path:
                    session.last_error = "Capture failed — check server logs"
                    session.status = "error"
                    session.save_metadata()
                    print(f"Timelapse capture failed for frame {frame_num} (camera {session.camera_num})")
                    break

                session.frame_count = frame_num
                session.save_metadata()

                next_tick += interval
                wait = next_tick - time.monotonic()
                if wait > 0:
                    session._stop_event.wait(wait)
                else:
                    next_tick = time.monotonic()

            if session.status == "running":
                session.status = "complete"

        except Exception as e:
            session.status = "error"
            session.last_error = str(e)
            print(f"Timelapse error (camera {session.camera_num}): {e}")
            traceback.print_exc()
        finally:
            self._cleanup_camera(camera)
            session.save_metadata()

    def _cleanup_camera(self, camera):
        try:
            with camera.capture_lock:
                camera.use_placeholder = False
                camera.picam2.configure(camera.video_config)
        except Exception as e:
            print(f"Timelapse cleanup error: {e}")
