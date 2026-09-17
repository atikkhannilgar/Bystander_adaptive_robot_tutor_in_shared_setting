"""Face + pose registration, room scan, and other-person detection for Ohbot."""

import math
import os
import sys
import threading
import time
import urllib.request

import cv2
import numpy as np
from ohbot import ohbot

from mediapipe.tasks.python.core import base_options as base_options_module
from mediapipe.tasks.python.vision import face_landmarker, pose_landmarker
from mediapipe.tasks.python.vision.core import image as image_module
from mediapipe.tasks.python.vision.core import vision_task_running_mode as running_mode_module

# pip install "numpy==1.26.4" mediapipe opencv-python

FACE_DATA_DIR = os.path.expanduser("~/.face_detector")
EMBEDDINGS_FILE = os.path.join(FACE_DATA_DIR, "embeddings.npz")
FACE_MODEL_FILE = os.path.join(FACE_DATA_DIR, "face_landmarker.task")
POSE_MODEL_FILE = os.path.join(FACE_DATA_DIR, "pose_landmarker_lite.task")
FACE_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/"
    "face_landmarker/face_landmarker/float16/1/face_landmarker.task"
)
POSE_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/"
    "pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task"
)

SAMPLES_NEEDED = 50
SAMPLE_INTERVAL_SEC = 0.35
READY_COUNTDOWN_SEC = 3

# cv2.imshow from a background thread (e.g. tkinter panel) crashes on macOS.
_opencv_preview_disabled = False


def reset_opencv_preview():
    global _opencv_preview_disabled
    _opencv_preview_disabled = False


def opencv_preview_frame(frame, window_name="Video", enabled=True):
    """Show frame if enabled; disable preview after first OpenCV GUI failure."""
    global _opencv_preview_disabled
    if not enabled or _opencv_preview_disabled:
        return False
    try:
        cv2.imshow(window_name, frame)
        return True
    except cv2.error as exc:
        _opencv_preview_disabled = True
        print(f"[opencv] Preview disabled: {exc}")
        return False


def opencv_preview_wait_key(delay_ms=1, enabled=True):
    global _opencv_preview_disabled
    if not enabled or _opencv_preview_disabled:
        time.sleep(max(0.0, delay_ms) / 1000.0)
        return -1
    try:
        return cv2.waitKey(delay_ms) & 0xFF
    except cv2.error:
        _opencv_preview_disabled = True
        time.sleep(max(0.0, delay_ms) / 1000.0)
        return -1


PREVIEW_CALLBACK_INTERVAL_SEC = 1.0 / 24.0


def emit_preview_frame(frame, show_window=True, on_frame=None, last_emit_time=None):
    """OpenCV window and/or ``on_frame`` callback (for tkinter on the main thread)."""
    now = time.monotonic()
    if on_frame is not None:
        emit = True
        if last_emit_time is not None:
            emit = (now - last_emit_time[0]) >= PREVIEW_CALLBACK_INTERVAL_SEC
        if emit:
            try:
                on_frame(frame)
            except Exception:
                pass
            if last_emit_time is not None:
                last_emit_time[0] = now
    if show_window:
        opencv_preview_frame(frame, enabled=True)
        return opencv_preview_wait_key(1, enabled=True)
    if on_frame is not None:
        time.sleep(PREVIEW_CALLBACK_INTERVAL_SEC)
    else:
        time.sleep(0.01)
    return -1


MATCH_MARGIN = 0.05
USER_MATCH_THRESHOLD = 0.90
USER_MATCH_RELAX = 0.72
OTHER_MATCH_MAX_SCORE = 0.70
BBOX_OVERLAP_SAME_PERSON = 0.25
OTHER_CONFIRM_FRAMES = 3
SCAN_STARTUP_SEC = 0
# Abort scan if the camera stops delivering frames (lesson continues without blocking).
NO_FRAME_ABORT_SEC = 2.0
MAX_CONSECUTIVE_NO_FRAME = 60
SCAN_MOTION_TICK_S = 1.0 / 50.0
SCAN_MOTION_DT_CAP = 0.05
# Peak slew during step scan between left/right check poses (motor units / second).
SCAN_MAX_TURN_RATE = 10.0
SCAN_MIN_HEAD_DELTA = 0.04
SCAN_MOVE_SPEED = 10
SCAN_DEGREES = 45.0
SCAN_HOLD_SEC = 2.0
SCAN_ARRIVE_DELTA = 0.18
# Motor 0/10 is ~±90° from centre on Ohbot; 45° scan ≈ 2.5 units (not 5).
SCAN_HEAD_HALF_RANGE_DEGREES = 90.0
SCAN_MAX_ROUNDS = 2
REST_HEAD_DELTA = 0.08
REST_MIN_HEAD_DELTA = 0.03
REST_SETTLE_MAX_S = 12.0
RETURN_MAX_TURN_RATE = 10.0
RETURN_MOVE_SPEED = 10
HEAD_CENTER = 5.0
HEAD_LEFT = 10.0
HEAD_RIGHT = 0.0
SCAN_WARMUP_SEC = 0
PHASE_CALIBRATION = "calibration"
PHASE_SCANNING = "scanning"
PHASE_RETURN_REST = "return_rest"
MIN_POSE_LANDMARKS = 8

ENROLLMENT_POSES = [
    ("CENTER", "Look straight at the camera"),
    ("LEFT", "Turn your head LEFT"),
    ("RIGHT", "Turn your head RIGHT"),
]
SAMPLES_PER_POSE = SAMPLES_NEEDED // len(ENROLLMENT_POSES)


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


def ensure_model(model_path, model_url, label):
    if os.path.isfile(model_path):
        return model_path

    os.makedirs(FACE_DATA_DIR, exist_ok=True)
    print(f"Downloading {label} to {model_path} ...")
    urllib.request.urlretrieve(model_url, model_path)
    print(f"{label} download complete.")
    return model_path


def create_face_landmarker():
    options = face_landmarker.FaceLandmarkerOptions(
        base_options=base_options_module.BaseOptions(
            model_asset_path=ensure_model(FACE_MODEL_FILE, FACE_MODEL_URL, "face model")
        ),
        running_mode=running_mode_module.VisionTaskRunningMode.VIDEO,
        num_faces=3,
        min_face_detection_confidence=0.5,
        min_face_presence_confidence=0.5,
        min_tracking_confidence=0.7,
    )
    return face_landmarker.FaceLandmarker.create_from_options(options)


def create_pose_landmarker():
    options = pose_landmarker.PoseLandmarkerOptions(
        base_options=base_options_module.BaseOptions(
            model_asset_path=ensure_model(
                POSE_MODEL_FILE, POSE_MODEL_URL, "pose model"
            )
        ),
        running_mode=running_mode_module.VisionTaskRunningMode.VIDEO,
        num_poses=3,
        min_pose_detection_confidence=0.5,
        min_pose_presence_confidence=0.5,
        min_tracking_confidence=0.7,
    )
    return pose_landmarker.PoseLandmarker.create_from_options(options)


def open_camera():
    print("Initializing camera")
    video_capture = cv2.VideoCapture(0)
    if not video_capture.isOpened():
        raise SystemExit("no camera found")
    print("Camera Initialized")
    return video_capture


def create_analyzer(face_lm, pose_lm):
    return SceneAnalyzer(face_lm, pose_lm)


# ---------------------------------------------------------------------------
# Detection + profile helpers
# ---------------------------------------------------------------------------


class SceneAnalyzer:
    """Face + person detection with one shared monotonic timestamp."""

    def __init__(self, face_lm, pose_lm):
        self.face_lm = face_lm
        self.pose_lm = pose_lm
        self._timestamp_ms = 0

    def analyze(self, frame):
        self._timestamp_ms += 33
        return analyze_scene(frame, self.face_lm, self.pose_lm, self._timestamp_ms)


def landmarks_bbox(landmarks, width, height, padding=0.12):
    xs = [lm.x for lm in landmarks]
    ys = [lm.y for lm in landmarks]
    x_min = int(min(xs) * width)
    y_min = int(min(ys) * height)
    x_max = int(max(xs) * width)
    y_max = int(max(ys) * height)

    box_w = x_max - x_min
    box_h = y_max - y_min
    pad_x = int(box_w * padding)
    pad_y = int(box_h * padding)

    x = max(0, x_min - pad_x)
    y = max(0, y_min - pad_y)
    w = min(width - x, box_w + 2 * pad_x)
    h = min(height - y, box_h + 2 * pad_y)
    return x, y, w, h


def pose_landmarks_bbox(landmarks, width, height):
    visible = [
        lm for lm in landmarks if lm.visibility is None or lm.visibility > 0.5
    ]
    if len(visible) < MIN_POSE_LANDMARKS:
        return None
    return landmarks_bbox(visible, width, height, padding=0.08)


def face_to_embedding(landmarks, width, height):
    points = np.array(
        [[lm.x * width, lm.y * height, lm.z * width] for lm in landmarks],
        dtype=np.float32,
    )

    left_eye = points[33]
    right_eye = points[263]
    nose = points[1]
    chin = points[152]

    center = (left_eye + right_eye + nose + chin) / 4.0
    scale = np.linalg.norm(right_eye - left_eye)
    if scale < 1e-6:
        return None

    normalized = (points - center) / scale
    return normalized.reshape(-1)


def pose_to_embedding(landmarks, width, height):
    points = np.array(
        [[lm.x * width, lm.y * height, lm.z * width] for lm in landmarks],
        dtype=np.float32,
    )

    left_shoulder = points[11]
    right_shoulder = points[12]
    left_hip = points[23]
    right_hip = points[24]

    center = (left_shoulder + right_shoulder + left_hip + right_hip) / 4.0
    scale = np.linalg.norm(right_shoulder - left_shoulder)
    if scale < 1e-6:
        return None

    normalized = (points - center) / scale
    return normalized.reshape(-1)


def cosine_similarity(a, b):
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom < 1e-8:
        return 0.0
    return float(np.dot(a, b) / denom)


def analyze_scene(frame, face_lm, pose_lm, timestamp_ms):
    height, width, _ = frame.shape
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    mp_image = image_module.Image(
        image_format=image_module.ImageFormat.SRGB,
        data=rgb,
    )

    face_result = face_lm.detect_for_video(mp_image, timestamp_ms)
    pose_result = pose_lm.detect_for_video(mp_image, timestamp_ms)

    faces = []
    if face_result.face_landmarks:
        for landmarks in face_result.face_landmarks:
            embedding = face_to_embedding(landmarks, width, height)
            if embedding is None:
                continue
            faces.append(
                {
                    "bbox": landmarks_bbox(landmarks, width, height),
                    "embedding": embedding,
                }
            )

    persons = []
    if pose_result.pose_landmarks:
        for landmarks in pose_result.pose_landmarks:
            bbox = pose_landmarks_bbox(landmarks, width, height)
            embedding = pose_to_embedding(landmarks, width, height)
            if bbox is None or embedding is None:
                continue
            persons.append({"bbox": bbox, "embedding": embedding})

    return {"faces": faces, "persons": persons}


class EmbeddingProfile:
    def __init__(self, embeddings):
        self.embeddings = np.array(embeddings, dtype=np.float32)
        self.centroid = self._normalize(np.mean(self.embeddings, axis=0))
        self.threshold = USER_MATCH_THRESHOLD

    @staticmethod
    def _normalize(vector):
        norm = np.linalg.norm(vector)
        if norm < 1e-8:
            return vector
        return vector / norm

    def match_score(self, embedding):
        if embedding is None:
            return 0.0

        sample_scores = [
            cosine_similarity(embedding, sample) for sample in self.embeddings
        ]
        top_scores = sorted(sample_scores, reverse=True)[:5]
        centroid_score = cosine_similarity(embedding, self.centroid)
        return float(0.45 * np.mean(top_scores) + 0.55 * centroid_score)


class UserProfile:
    def __init__(self, face_embeddings, pose_embeddings):
        self.face_profile = EmbeddingProfile(face_embeddings)
        self.pose_profile = EmbeddingProfile(pose_embeddings)

    @property
    def sample_count(self):
        return len(self.face_profile.embeddings)

    def face_match_score(self, embedding):
        return self.face_profile.match_score(embedding)

    def pose_match_score(self, embedding):
        return self.pose_profile.match_score(embedding)

    def save_dict(self):
        return {
            "face_embeddings": self.face_profile.embeddings,
            "pose_embeddings": self.pose_profile.embeddings,
            "face_centroid": self.face_profile.centroid,
            "pose_centroid": self.pose_profile.centroid,
            "face_threshold": self.face_profile.threshold,
            "pose_threshold": self.pose_profile.threshold,
        }


def load_user_profile():
    if not os.path.isfile(EMBEDDINGS_FILE):
        return None

    data = np.load(EMBEDDINGS_FILE)
    if "pose_embeddings" not in data:
        print("Old face-only profile found. Please register again.")
        return None

    profile = UserProfile(list(data["face_embeddings"]), list(data["pose_embeddings"]))
    profile.face_profile.threshold = USER_MATCH_THRESHOLD
    profile.pose_profile.threshold = USER_MATCH_THRESHOLD
    return profile


def save_user_profile(samples):
    face_embeddings = [sample["face"] for sample in samples]
    pose_embeddings = [sample["pose"] for sample in samples]
    profile = UserProfile(face_embeddings, pose_embeddings)
    os.makedirs(FACE_DATA_DIR, exist_ok=True)
    np.savez(EMBEDDINGS_FILE, **profile.save_dict())
    print(f"Saved {len(samples)} paired face+pose samples to {EMBEDDINGS_FILE}")
    print(
        f"Face threshold {profile.face_profile.threshold:.3f}, "
        f"pose threshold {profile.pose_profile.threshold:.3f}"
    )
    return profile


def clear_saved_user_profile():
    """Delete the saved face+pose profile file. Returns (success, message).

    Only call from an explicit user action (Clear profile). Lesson/task end
    must never invoke this — stopping a scan only closes the camera.
    """
    if not os.path.isfile(EMBEDDINGS_FILE):
        return True, "No saved profile to remove."

    try:
        os.remove(EMBEDDINGS_FILE)
        print(f"[FACE_GUARD] Removed saved profile: {EMBEDDINGS_FILE}")
        return True, f"Removed saved profile: {EMBEDDINGS_FILE}"
    except Exception as exc:
        print(f"[FACE_GUARD] Failed to remove profile: {exc}")
        return False, f"Failed to remove profile: {exc}"


def bbox_overlap_ratio(a, b):
    """Share of the smaller box covered by intersection (high = duplicate detection)."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix1, iy1 = max(ax, bx), max(ay, by)
    ix2, iy2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    min_area = min(aw * ah, bw * bh)
    return inter / min_area if min_area > 0 else 0.0


def bbox_centre_distance(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    acx, acy = ax + aw / 2, ay + ah / 2
    bcx, bcy = bx + bw / 2, by + bh / 2
    return math.hypot(acx - bcx, acy - bcy)


def _classify_by_embedding_scores(items, scores, profile_threshold):
    """Relaxed user match with overlap merge (shared by face and pose channels)."""
    if not items:
        return []

    relax = max(USER_MATCH_RELAX, profile_threshold * 0.88)
    best_index = int(np.argmax(scores))

    user_indices = set()
    for index, score in enumerate(scores):
        if score >= relax:
            user_indices.add(index)

    if scores[best_index] >= relax * 0.92:
        user_indices.add(best_index)

    merged = set(user_indices)
    for i in list(user_indices):
        for j in list(user_indices):
            if i != j and bbox_overlap_ratio(
                items[i]["bbox"], items[j]["bbox"]
            ) > BBOX_OVERLAP_SAME_PERSON:
                merged.add(i)
                merged.add(j)

    classified = []
    for index, item in enumerate(items):
        classified.append(
            {**item, "score": scores[index], "is_user": index in merged}
        )
    return classified


def classify_faces(faces, profile):
    if not faces:
        return []

    scores = [profile.face_match_score(face["embedding"]) for face in faces]
    scored = _classify_by_embedding_scores(
        faces, scores, profile.face_profile.threshold
    )
    return [
        {"face": item, "score": item["score"], "is_user": item["is_user"]}
        for item in scored
    ]


def classify_poses(persons, profile):
    if not persons:
        return []

    scores = [profile.pose_match_score(person["embedding"]) for person in persons]
    scored = _classify_by_embedding_scores(
        persons, scores, profile.pose_profile.threshold
    )
    return [
        {"person": item, "score": item["score"], "is_user": item["is_user"]}
        for item in scored
    ]


def classify_persons(persons, profile, classified_faces):
    """User if pose matches OR upper body is linked to a user face — no face-over-pose priority."""
    user_face_bboxes = [
        item["face"]["bbox"] for item in classified_faces if item["is_user"]
    ]
    pose_classified = classify_poses(persons, profile)

    classified = []
    for item in pose_classified:
        person = item["person"]
        linked_to_user_face = any(
            face_in_person(face_bbox, person["bbox"])
            for face_bbox in user_face_bboxes
        )
        is_user_pose = item["is_user"]
        classified.append(
            {
                "person": person,
                "score": item["score"],
                "is_user": is_user_pose or linked_to_user_face,
                "used_pose": is_user_pose,
                "linked_face": linked_to_user_face and not is_user_pose,
            }
        )
    return classified


def frame_has_other_person(classified_faces, classified_persons, profile=None):
    """True only when a distinct second person is present (not the registered user / duplicates)."""
    user_face_bboxes = [item["face"]["bbox"] for item in classified_faces if item["is_user"]]
    user_person_bboxes = [
        item["person"]["bbox"] for item in classified_persons if item["is_user"]
    ]
    relax_face = USER_MATCH_RELAX
    relax_pose = USER_MATCH_RELAX
    if profile is not None:
        relax_face = max(USER_MATCH_RELAX, profile.face_profile.threshold * 0.88)
        relax_pose = max(USER_MATCH_RELAX, profile.pose_profile.threshold * 0.88)

    def overlaps_user(bbox, user_bboxes):
        return any(
            bbox_overlap_ratio(bbox, ub) > BBOX_OVERLAP_SAME_PERSON for ub in user_bboxes
        )

    def distinct_other_face(item):
        if item["is_user"]:
            return False
        fb = item["face"]["bbox"]
        if overlaps_user(fb, user_face_bboxes):
            return False
        if profile is not None:
            score = profile.face_match_score(item["face"]["embedding"])
            if score >= relax_face:
                return False
            return score <= OTHER_MATCH_MAX_SCORE
        return item.get("score", 0) <= OTHER_MATCH_MAX_SCORE

    def distinct_other_person(item):
        if item["is_user"]:
            return False
        pb = item["person"]["bbox"]
        if overlaps_user(pb, user_person_bboxes + user_face_bboxes):
            return False
        if profile is not None:
            score = profile.pose_match_score(item["person"]["embedding"])
            if score >= relax_pose:
                return False
            return score <= OTHER_MATCH_MAX_SCORE
        return True

    other_faces = [item for item in classified_faces if distinct_other_face(item)]
    other_persons = [item for item in classified_persons if distinct_other_person(item)]

    if user_face_bboxes or user_person_bboxes:
        return len(other_faces) > 0 or len(other_persons) > 0

    if profile is not None:
        for item in classified_faces:
            if profile.face_match_score(item["face"]["embedding"]) >= relax_face:
                return len(other_faces) > 0 or len(other_persons) > 0
        for item in classified_persons:
            if profile.pose_match_score(item["person"]["embedding"]) >= relax_pose:
                return len(other_faces) > 0 or len(other_persons) > 0

    if len(other_faces) >= 2:
        for i, a in enumerate(other_faces):
            for b in other_faces[i + 1 :]:
                if bbox_centre_distance(a["face"]["bbox"], b["face"]["bbox"]) > 80:
                    return True
        return False

    return len(other_faces) > 0 or len(other_persons) > 0


def face_in_person(face_bbox, person_bbox):
    fx, fy, fw, fh = face_bbox
    px, py, pw, ph = person_bbox
    face_center_x = fx + fw / 2
    face_center_y = fy + fh / 2
    return px <= face_center_x <= px + pw and py <= face_center_y <= py + ph


def pair_face_and_person(faces, persons):
    pairs = []
    for face in faces:
        for person in persons:
            if face_in_person(face["bbox"], person["bbox"]):
                pairs.append((face, person))
    return pairs


def current_pose_hint(sample_count):
    pose_index = min(sample_count // SAMPLES_PER_POSE, len(ENROLLMENT_POSES) - 1)
    return ENROLLMENT_POSES[pose_index]


def draw_label(frame, x, y, text, color):
    cv2.rectangle(frame, (x, y), (x + 280, y - 28), color, -1)
    cv2.putText(
        frame,
        text,
        (x + 6, y - 8),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        (255, 255, 255),
        2,
    )


def draw_banner(frame, text, color):
    cv2.rectangle(frame, (0, 0), (frame.shape[1], 45), color, -1)
    cv2.putText(
        frame,
        text,
        (12, 32),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (255, 255, 255),
        2,
    )


def draw_status_text(frame, text):
    cv2.putText(
        frame,
        text,
        (10, frame.shape[0] - 20),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (255, 200, 0),
        2,
    )


# ---------------------------------------------------------------------------
# Phase 1: Calibration / registration
# ---------------------------------------------------------------------------


def calibrate_user(
    video_capture,
    analyzer,
    stop_event=None,
    show_window=True,
    on_status=None,
    on_frame=None,
):
    """Register the user's face + pose profile."""
    samples = []
    last_capture_time = 0.0
    registration_start = time.monotonic()
    last_preview_emit = [0.0]
    notify = on_status or (lambda _msg: None)

    print("\n=== Calibration: face + pose registration ===")
    print(f"Capturing {SAMPLES_NEEDED} paired samples automatically.")
    print("Keep your face and upper body visible in the camera.")
    print("Look straight, then slowly turn left and right.")
    print("Press q to cancel.\n")

    while len(samples) < SAMPLES_NEEDED:
        if stop_event is not None and stop_event.is_set():
            return None

        ret, frame = video_capture.read()
        if not ret or frame is None:
            print("Could not read from camera.")
            notify("Camera read failed")
            return None

        elapsed = time.monotonic() - registration_start
        scene = analyzer.analyze(frame)
        faces = scene["faces"]
        persons = scene["persons"]
        pairs = pair_face_and_person(faces, persons)
        pose_name, pose_hint = current_pose_hint(len(samples))
        display = frame.copy()
        now = time.monotonic()

        if elapsed < READY_COUNTDOWN_SEC:
            status = f"Starting in {int(READY_COUNTDOWN_SEC - elapsed) + 1}..."
        elif len(faces) == 0:
            status = "No face detected - look at the camera"
        elif len(persons) == 0:
            status = "Step back - upper body must be visible"
        elif len(faces) > 1 or len(persons) > 1:
            status = "Only you should be visible"
        elif not pairs:
            status = "Center your face above your body"
        else:
            time_until_next = SAMPLE_INTERVAL_SEC - (now - last_capture_time)
            status = (
                "Capturing face + pose..."
                if time_until_next <= 0
                else f"Next capture in {time_until_next:.1f}s"
            )

        cv2.putText(
            display,
            f"Calibrating: {len(samples)}/{SAMPLES_NEEDED}",
            (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 255, 255),
            2,
        )
        cv2.putText(
            display,
            f"{pose_name}: {pose_hint}",
            (10, 65),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (0, 255, 255),
            2,
        )
        cv2.putText(
            display,
            status,
            (10, 95),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (0, 255, 255),
            2,
        )
        cv2.putText(
            display,
            "Press q to cancel",
            (10, 125),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (0, 255, 255),
            1,
        )

        for person in persons:
            x, y, w, h = person["bbox"]
            cv2.rectangle(display, (x, y), (x + w, y + h), (255, 200, 0), 2)

        for face in faces:
            x, y, w, h = face["bbox"]
            cv2.rectangle(display, (x, y), (x + w, y + h), (0, 255, 255), 2)

        notify(f"Calibrating {len(samples)}/{SAMPLES_NEEDED}: {status}")
        key = emit_preview_frame(
            display,
            show_window=show_window,
            on_frame=on_frame,
            last_emit_time=last_preview_emit,
        )
        if key == ord("q"):
            return None

        if (
            elapsed >= READY_COUNTDOWN_SEC
            and len(pairs) == 1
            and (now - last_capture_time) >= SAMPLE_INTERVAL_SEC
        ):
            face, person = pairs[0]
            samples.append({"face": face["embedding"], "pose": person["embedding"]})
            last_capture_time = now
            pose_name, _ = current_pose_hint(len(samples) - 1)
            print(f"Captured {pose_name} face+pose {len(samples)}/{SAMPLES_NEEDED}")

    print("Calibration complete.")
    return save_user_profile(samples)


def choose_startup_mode():
    has_saved_profile = load_user_profile() is not None

    print("\n=== Face + Person Detector ===")
    print("1. Calibrate (register face + pose)")
    if has_saved_profile:
        print("2. Start detection (use saved profile)")
        print("3. Quit")
    else:
        print("2. Quit")
    print()

    while True:
        choice = input("Choose an option: ").strip()

        if choice == "1":
            return "calibrate"

        if has_saved_profile and choice == "2":
            return "detect"

        if (has_saved_profile and choice == "3") or (
            not has_saved_profile and choice == "2"
        ):
            return "quit"

        print("Invalid choice. Try again.")


def load_or_calibrate(video_capture, analyzer, mode):
    if mode == "calibrate":
        profile = calibrate_user(video_capture, analyzer)
        if profile is None:
            raise SystemExit("Calibration cancelled. Exiting.")
        return profile

    profile = load_user_profile()
    if profile is None:
        raise SystemExit("No saved profile found. Calibrate first.")
    print(
        f"Using saved profile ({profile.sample_count} samples, "
        f"threshold {profile.face_profile.threshold:.3f})."
    )
    return profile


# ---------------------------------------------------------------------------
# Phase 2 + 3: Scanning and return to centre
# ---------------------------------------------------------------------------


class HeadScanner:
    """Step scan: centre → 45° right (hold) → centre → 45° left (hold) → repeat."""

    STATE_TO_RIGHT = "to_right"
    STATE_HOLD_RIGHT = "hold_right"
    STATE_TO_CENTER = "to_center"
    STATE_TO_LEFT = "to_left"
    STATE_HOLD_LEFT = "hold_left"

    def __init__(
        self,
        center_turn=HEAD_CENTER,
        scan_degrees=SCAN_DEGREES,
        hold_sec=SCAN_HOLD_SEC,
        arrive_delta=SCAN_ARRIVE_DELTA,
        warmup_sec=SCAN_WARMUP_SEC,
        head_half_range_degrees=SCAN_HEAD_HALF_RANGE_DEGREES,
        max_rounds=SCAN_MAX_ROUNDS,
    ):
        self.center_turn = float(center_turn)
        self.scan_degrees = float(scan_degrees)
        self.max_rounds = max(1, int(max_rounds))
        self.rounds_completed = 0
        self.scan_finished = False
        self.finish_after_this_round = False
        delta = _head_turn_delta_units(
            self.scan_degrees, half_span_degrees=head_half_range_degrees
        )
        self.right_turn = max(HEAD_RIGHT, self.center_turn - delta)
        self.left_turn = min(HEAD_LEFT, self.center_turn + delta)
        self.hold_sec = float(hold_sec)
        self.arrive_delta = float(arrive_delta)
        self.warmup_sec = float(warmup_sec)
        self.state = self.STATE_TO_RIGHT
        self.state_entered = time.monotonic()
        self.start_time = self.state_entered
        self.current_target = self.center_turn
        self._after_center_state = self.STATE_TO_RIGHT

    def reset(self):
        self.start_time = time.monotonic()
        self.state = self.STATE_TO_RIGHT
        self.state_entered = self.start_time
        self.current_target = self.center_turn
        self._after_center_state = self.STATE_TO_RIGHT
        self.rounds_completed = 0
        self.scan_finished = False
        self.finish_after_this_round = False

    def request_finish_after_round(self):
        """Finish the current scan round, then stop at centre (e.g. other person seen)."""
        self.finish_after_this_round = True

    def _at_target(self, actual_turn):
        return abs(float(actual_turn) - self.current_target) <= self.arrive_delta

    def _at_center(self, actual_turn):
        return abs(float(actual_turn) - self.center_turn) <= self.arrive_delta

    def update(self, actual_turn):
        now = time.monotonic()
        if now - self.start_time < self.warmup_sec:
            self.current_target = self.center_turn
            return

        if self.state == self.STATE_TO_RIGHT:
            self.current_target = self.right_turn
            if self._at_target(actual_turn):
                self.state = self.STATE_HOLD_RIGHT
                self.state_entered = now
        elif self.state == self.STATE_HOLD_RIGHT:
            self.current_target = self.right_turn
            if now - self.state_entered >= self.hold_sec:
                self.state = self.STATE_TO_CENTER
                self._after_center_state = self.STATE_TO_LEFT
                self.state_entered = now
        elif self.state == self.STATE_TO_CENTER:
            self.current_target = self.center_turn
            if self._at_center(actual_turn):
                if self._after_center_state == self.STATE_TO_RIGHT:
                    self.rounds_completed += 1
                    if (
                        self.finish_after_this_round
                        or self.rounds_completed >= self.max_rounds
                    ):
                        self.scan_finished = True
                        self.current_target = self.center_turn
                        return
                self.state = self._after_center_state
                self.state_entered = now
        elif self.state == self.STATE_TO_LEFT:
            self.current_target = self.left_turn
            if self._at_target(actual_turn):
                self.state = self.STATE_HOLD_LEFT
                self.state_entered = now
        elif self.state == self.STATE_HOLD_LEFT:
            self.current_target = self.left_turn
            if now - self.state_entered >= self.hold_sec:
                self.state = self.STATE_TO_CENTER
                self._after_center_state = self.STATE_TO_RIGHT
                self.state_entered = now

    def target_turn(self):
        if self.scan_finished:
            return self.center_turn
        return self.current_target

    def is_holding(self):
        return self.state in (self.STATE_HOLD_RIGHT, self.STATE_HOLD_LEFT)

    def rounds_exhausted(self):
        return self.scan_finished

    def status_text(self):
        deg = int(round(self.scan_degrees))
        finishing = " (finishing round)" if self.finish_after_this_round else ""
        if self.state == self.STATE_HOLD_RIGHT:
            return f"Holding {deg}° right — checking...{finishing}"
        if self.state == self.STATE_HOLD_LEFT:
            return f"Holding {deg}° left — checking...{finishing}"
        if self.state == self.STATE_TO_CENTER:
            return f"Returning to centre...{finishing}"
        if self.state == self.STATE_TO_RIGHT:
            return f"Turning {deg}° right...{finishing}"
        return f"Turning {deg}° left...{finishing}"


def _head_turn_delta_units(degrees, half_span_degrees=90.0):
    """Motor-units delta from centre for a yaw angle.

    ``half_span_degrees`` = physical yaw from centre to motor stop (0 or 10).
    On Ohbot that is ~90°, so a 45° scan is ~2.5 motor units (not 5).
    """
    span = float(half_span_degrees) if half_span_degrees else 90.0
    if span <= 0:
        span = 90.0
    return min(5.0, max(0.0, float(degrees) * (5.0 / span)))


class HeadMotionController:
    """Smooth head commands and avoid sending tiny jittery moves to Ohbot."""

    def __init__(self):
        self.turn = HEAD_CENTER
        self.nod = HEAD_CENTER
        self.last_sent_turn = HEAD_CENTER
        self.last_sent_nod = HEAD_CENTER

    def sync_from_ohbot(self):
        """Match internal state to actual Ohbot head motors."""
        try:
            turn = float(ohbot.motorPos[ohbot.HEADTURN])
            nod = float(ohbot.motorPos[ohbot.HEADNOD])
            if 0.0 <= turn <= 10.0:
                self.turn = turn
                self.last_sent_turn = turn
            if 0.0 <= nod <= 10.0:
                self.nod = nod
                self.last_sent_nod = nod
        except Exception:
            pass

    def begin_return_to_rest(self, rest_turn, rest_nod):
        """Sync actual pose and command rest — internal state tracks the real motors."""
        self.sync_from_ohbot()
        ohbot.move(ohbot.HEADTURN, float(rest_turn), RETURN_MOVE_SPEED)
        ohbot.move(ohbot.HEADNOD, float(rest_nod), RETURN_MOVE_SPEED)

    def rate_limit_toward(self, target_turn, target_nod, dt, max_rate):
        max_step = max(0.0, float(max_rate)) * max(0.0, float(dt))
        for attr, target in (("turn", target_turn), ("nod", target_nod)):
            current = getattr(self, attr)
            delta = target - current
            if abs(delta) <= max_step:
                setattr(self, attr, target)
            else:
                setattr(self, attr, current + (max_step if delta > 0 else -max_step))

    def advance_motion(
        self,
        dt,
        target_turn,
        target_nod,
        scanning=False,
        returning=False,
        target_turn_fn=None,
    ):
        """Integrate head motion in fixed sub-steps (smooth even when vision is slow)."""
        if dt <= 0:
            return
        remaining = float(dt)
        tick = SCAN_MOTION_TICK_S
        while remaining > 0:
            step_dt = min(remaining, tick)
            if scanning:
                turn_target = target_turn_fn() if target_turn_fn is not None else target_turn
                self.rate_limit_toward(
                    turn_target, target_nod, step_dt, SCAN_MAX_TURN_RATE
                )
            elif returning:
                self.rate_limit_toward(
                    target_turn, target_nod, step_dt, RETURN_MAX_TURN_RATE
                )
            self.move_if_needed(scanning=scanning, returning=returning)
            remaining -= step_dt

    def move_if_needed(self, scanning=False, returning=False):
        if scanning:
            min_delta = SCAN_MIN_HEAD_DELTA
            speed = SCAN_MOVE_SPEED
        elif returning:
            min_delta = REST_MIN_HEAD_DELTA
            speed = RETURN_MOVE_SPEED
        else:
            return
        if (
            abs(self.turn - self.last_sent_turn) < min_delta
            and abs(self.nod - self.last_sent_nod) < min_delta
        ):
            return

        ohbot.move(ohbot.HEADTURN, self.turn, speed)
        ohbot.move(ohbot.HEADNOD, self.nod, speed)
        self.last_sent_turn = self.turn
        self.last_sent_nod = self.nod


class OtherPersonConfirmer:
    """Require multiple consecutive frames before confirming another person."""

    def __init__(self, confirm_frames=OTHER_CONFIRM_FRAMES):
        self.confirm_frames = confirm_frames
        self.other_streak = 0
        self.confirmed = False
        self.alert_printed = False

    def reset(self):
        self.other_streak = 0
        self.confirmed = False
        self.alert_printed = False

    def update(self, saw_other):
        if saw_other:
            self.other_streak += 1
            if not self.confirmed and self.other_streak >= self.confirm_frames:
                self.confirmed = True
        else:
            self.other_streak = 0

        if self.confirmed and not self.alert_printed:
            self.alert_printed = True

        return self.confirmed


def analyze_and_classify(frame, analyzer, profile):
    scene = analyzer.analyze(frame)
    classified_faces = classify_faces(scene["faces"], profile)
    classified_persons = classify_persons(
        scene["persons"], profile, classified_faces
    )
    return classified_faces, classified_persons


def draw_detection_overlay(frame, classified_faces, classified_persons):
    for item in classified_persons:
        person = item["person"]
        x, y, w, h = person["bbox"]
        score = item["score"]
        if item["is_user"]:
            color = (0, 180, 0)
            if item.get("used_pose") and item.get("linked_face"):
                label = f"You (face+pose) {score:.2f}"
            elif item.get("used_pose"):
                label = f"You (pose) {score:.2f}"
            else:
                label = f"You (linked) {score:.2f}"
        else:
            color = (0, 0, 220)
            label = f"Other person {score:.2f}"
        cv2.rectangle(frame, (x, y), (x + w, y + h), color, 3)
        draw_label(frame, x, y + h, label, color)

    for item in classified_faces:
        face = item["face"]
        x, y, w, h = face["bbox"]
        score = item["score"]
        if item["is_user"]:
            color = (0, 255, 0)
            label = f"You (face) {score:.2f}"
        else:
            color = (0, 0, 255)
            label = f"Other face {score:.2f}"
        cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
        draw_label(frame, x, y, label, color)


def finalize_rest_head(head_motion, target_turn, target_nod):
    """Send one final move so Ohbot ends on the rest / centre pose."""
    head_motion.turn = target_turn
    head_motion.nod = target_nod
    ohbot.move(ohbot.HEADTURN, target_turn, RETURN_MOVE_SPEED)
    ohbot.move(ohbot.HEADNOD, target_nod, RETURN_MOVE_SPEED)
    head_motion.last_sent_turn = target_turn
    head_motion.last_sent_nod = target_nod


def run_scanning_step(frame, head_scanner, startup_done):
    """Phase 2: step scan right/left with pauses to check for other people."""
    status = (
        head_scanner.status_text()
        if startup_done
        else "Starting scan..."
    )
    draw_status_text(frame, status)


def run_return_rest_step(frame, banner=None):
    """Phase 3 (Task 2): stop scanning and return head to neutral rest."""
    draw_banner(
        frame,
        banner or "Another person detected - returning head to rest",
        (0, 0, 200),
    )


def detect_other_person(classified_faces, classified_persons, confirmer, startup_done, profile=None):
    """Check if another person is present and confirmed."""
    if not startup_done:
        return False
    saw_other = frame_has_other_person(classified_faces, classified_persons, profile)
    return confirmer.update(saw_other)


def ohbot_head_at_rest(rest_turn, rest_nod, delta):
    """True when actual Ohbot head motors are near the rest pose."""
    try:
        turn = float(ohbot.motorPos[ohbot.HEADTURN])
        nod = float(ohbot.motorPos[ohbot.HEADNOD])
        if not (0.0 <= turn <= 10.0 and 0.0 <= nod <= 10.0):
            return False
        return abs(turn - float(rest_turn)) < delta and abs(nod - float(rest_nod)) < delta
    except Exception:
        return False


def _stop_requested(stop_event, extra_stop_event=None):
    if stop_event is not None and stop_event.is_set():
        return True
    if extra_stop_event is not None and extra_stop_event.is_set():
        return True
    return False


def run_detection_session(
    video_capture,
    analyzer,
    profile,
    stop_event=None,
    show_window=True,
    on_status=None,
    on_frame=None,
    task2_one_shot=False,
    extra_stop_event=None,
    task2_facing_hold_s=2.0,
    task2_facing_settled_frames=12,
    task2_facing_center_tolerance=None,
    task2_facing_head_delta=None,
    task2_facing_max_s=None,
    rest_turn=None,
    rest_nod=None,
):
    """
    Main runtime:
      1. Step-scan left/right for another person
      2. Return head to centre/rest when done (detected or max rounds)
    """
    notify = on_status or (lambda _msg: None)
    last_preview_emit = [0.0]
    rest_turn_pos = float(rest_turn if rest_turn is not None else HEAD_CENTER)
    rest_nod_pos = float(rest_nod if rest_nod is not None else HEAD_CENTER)
    head_motion = HeadMotionController()
    head_motion.sync_from_ohbot()
    head_scanner = HeadScanner(center_turn=rest_turn_pos)
    confirmer = OtherPersonConfirmer()
    phase = PHASE_SCANNING
    session_start = time.monotonic()
    post_detect_since = None
    post_detect_settled_streak = 0
    last_motion_time = time.monotonic()
    other_person_latched = False
    consecutive_no_frame = 0
    no_frame_since = None
    exit_reason = "complete"

    print("Starting detection session...")
    notify("Scanning right and left for other people...")

    while True:
        if _stop_requested(stop_event, extra_stop_event):
            exit_reason = "stopped"
            break

        ret, frame = video_capture.read()
        if not ret or frame is None:
            consecutive_no_frame += 1
            now_nf = time.monotonic()
            if no_frame_since is None:
                no_frame_since = now_nf
                print("[FACE_SCAN] Camera read failed — waiting briefly...")
            elif consecutive_no_frame % 20 == 0:
                print("[FACE_SCAN] Still no camera frame...")
            if (
                consecutive_no_frame >= MAX_CONSECUTIVE_NO_FRAME
                or (now_nf - no_frame_since) >= NO_FRAME_ABORT_SEC
            ):
                print("[FACE_SCAN] No camera frames — skipping face scan")
                notify("Camera unavailable — skipping scan, continuing lesson")
                finalize_rest_head(head_motion, rest_turn_pos, rest_nod_pos)
                exit_reason = "no_camera"
                break
            time.sleep(0.03)
            continue

        consecutive_no_frame = 0
        no_frame_since = None

        classified_faces, classified_persons = analyze_and_classify(
            frame, analyzer, profile
        )
        draw_detection_overlay(frame, classified_faces, classified_persons)

        startup_done = (time.monotonic() - session_start) >= SCAN_STARTUP_SEC

        if phase == PHASE_SCANNING:
            head_scanner.update(head_motion.turn)
            holding = head_scanner.is_holding() and startup_done
            if holding:
                other_confirmed = detect_other_person(
                    classified_faces, classified_persons, confirmer, True, profile
                )
            elif other_person_latched:
                other_confirmed = True
            else:
                confirmer.other_streak = 0
                other_confirmed = False

            if other_confirmed and not other_person_latched:
                other_person_latched = True
                head_scanner.request_finish_after_round()
                round_num = head_scanner.rounds_completed + 1
                print(
                    "[FACE_SCAN] Another person detected — waiting for scan round "
                    f"{round_num}/{head_scanner.max_rounds} to complete before "
                    "returning to centre."
                )
                print(
                    f"[FACE_SCAN] Current scan step: {head_scanner.state} "
                    f"(finishing round, then return to rest)"
                )
                notify("Another person detected — finishing scan round...")

            if startup_done and head_scanner.rounds_exhausted():
                phase = PHASE_RETURN_REST
                head_motion.begin_return_to_rest(rest_turn_pos, rest_nod_pos)
                post_detect_since = time.monotonic()
                post_detect_settled_streak = 0
                if other_person_latched:
                    print(
                        "[FACE_SCAN] Scan round complete — other person was detected; "
                        f"returning to centre (turn={rest_turn_pos:.1f}, nod={rest_nod_pos:.1f})."
                    )
                    notify(
                        "Scan round complete — other person detected, returning to centre"
                    )
                    run_return_rest_step(
                        frame, "Other person detected — returning to centre"
                    )
                else:
                    print(
                        f"[FACE_SCAN] Scan rounds complete ({SCAN_MAX_ROUNDS}) — "
                        f"no other person; returning to centre (turn={rest_turn_pos:.1f})."
                    )
                    notify(
                        f"No other person after {SCAN_MAX_ROUNDS} scan rounds — "
                        "returning to centre"
                    )
                    run_return_rest_step(
                        frame, "Scan complete — no other person, returning to centre"
                    )
            else:
                run_scanning_step(frame, head_scanner, startup_done)
        else:
            run_return_rest_step(frame)

        motion_now = time.monotonic()
        motion_dt = motion_now - last_motion_time
        if phase == PHASE_SCANNING:
            motion_dt = min(motion_dt, SCAN_MOTION_DT_CAP)
        last_motion_time = motion_now
        if phase == PHASE_SCANNING:
            head_motion.advance_motion(
                motion_dt,
                HEAD_CENTER,
                HEAD_CENTER,
                scanning=True,
                target_turn_fn=head_scanner.target_turn,
            )
        elif phase == PHASE_RETURN_REST:
            head_motion.advance_motion(
                motion_dt,
                rest_turn_pos,
                rest_nod_pos,
                returning=True,
            )

        if phase == PHASE_RETURN_REST:
            if post_detect_since is None:
                post_detect_since = time.monotonic()
            head_delta = (
                float(task2_facing_head_delta)
                if task2_facing_head_delta is not None
                else REST_HEAD_DELTA
            )
            max_post_s = (
                float(task2_facing_max_s)
                if task2_facing_max_s is not None
                else REST_SETTLE_MAX_S
            )
            at_rest = ohbot_head_at_rest(rest_turn_pos, rest_nod_pos, head_delta)
            if not at_rest:
                at_rest = (
                    abs(head_motion.turn - rest_turn_pos) < head_delta
                    and abs(head_motion.nod - rest_nod_pos) < head_delta
                )
            if at_rest:
                post_detect_settled_streak += 1
            else:
                post_detect_settled_streak = 0
            hold_s = max(0.0, float(task2_facing_hold_s or 0.0))
            need_frames = max(1, int(task2_facing_settled_frames or 1))
            post_elapsed = time.monotonic() - post_detect_since
            timed_out = max_post_s > 0 and post_elapsed >= max_post_s
            if (
                (post_detect_settled_streak >= need_frames and post_elapsed >= hold_s)
                or timed_out
            ):
                finalize_rest_head(head_motion, rest_turn_pos, rest_nod_pos)
                print(
                    f"[FACE_SCAN] Scan complete — head at centre "
                    f"(turn={rest_turn_pos:.1f}, nod={rest_nod_pos:.1f})"
                )
                notify("Scan complete — head at centre")
                break
        if show_window or on_frame is not None:
            key = emit_preview_frame(
                frame,
                show_window=show_window,
                on_frame=on_frame,
                last_emit_time=last_preview_emit,
            )
            if key == ord("q"):
                exit_reason = "stopped"
                break
            if key == ord("r") and show_window:
                print("Re-calibrating...")
                notify("Re-calibrating...")
                new_profile = calibrate_user(
                    video_capture,
                    analyzer,
                    stop_event=stop_event,
                    show_window=show_window,
                    on_status=notify,
                    on_frame=on_frame,
                )
                if new_profile is not None:
                    profile = new_profile
                    phase = PHASE_SCANNING
                    confirmer = OtherPersonConfirmer()
                    head_scanner.reset()
                    other_person_latched = False
                    session_start = time.monotonic()
                    notify("Calibration saved — resuming scan")
                else:
                    print("Calibration cancelled. Keeping previous profile.")
                    notify("Calibration cancelled — keeping previous profile")

    return profile, exit_reason, bool(other_person_latched)


class FaceGuardService:
    """Calibrate, scan, and detect other people — for use from Ohbot Control Panel."""

    def __init__(self, stop_event=None, on_status=None, on_frame=None, ready_event=None):
        self.stop_event = stop_event or threading.Event()
        self.on_status = on_status or (lambda _msg: None)
        self.on_frame = on_frame
        self.ready_event = ready_event
        self._lock = threading.Lock()
        self._video_capture = None
        self._face_lm = None
        self._pose_lm = None
        self._analyzer = None
        self._profile = None
        self.last_other_person_detected = False

    @staticmethod
    def has_saved_profile():
        return load_user_profile() is not None

    @staticmethod
    def profile_path():
        return EMBEDDINGS_FILE

    @staticmethod
    def clear_saved_profile():
        """Remove calibrated user data from disk. Returns (success, message)."""
        return clear_saved_user_profile()

    def forget_profile(self):
        """Clear in-memory profile and delete saved file."""
        ok, message = clear_saved_user_profile()
        self._profile = None
        return ok, message

    def _notify(self, message):
        try:
            self.on_status(str(message))
        except Exception:
            pass

    def _ensure_open(self):
        with self._lock:
            if self._video_capture is None or not self._video_capture.isOpened():
                self._notify("Opening camera...")
                self._video_capture = open_camera()
            if self._face_lm is None:
                self._face_lm = create_face_landmarker()
            if self._pose_lm is None:
                self._pose_lm = create_pose_landmarker()
            if self._analyzer is None:
                self._analyzer = create_analyzer(self._face_lm, self._pose_lm)

    def close(self, destroy_windows=True):
        with self._lock:
            if self._video_capture is not None:
                try:
                    self._video_capture.release()
                except Exception:
                    pass
                self._video_capture = None
            for landmarker in (self._face_lm, self._pose_lm):
                if landmarker is not None:
                    try:
                        landmarker.close()
                    except Exception:
                        pass
            self._face_lm = None
            self._pose_lm = None
            self._analyzer = None
            if destroy_windows:
                try:
                    cv2.destroyAllWindows()
                except Exception:
                    pass

    def calibrate(self, show_window=True):
        """Register the user's face + pose (100 paired samples)."""
        self._ensure_open()
        if show_window:
            reset_opencv_preview()
        self._notify("Calibrating — keep face and upper body visible")
        profile = calibrate_user(
            self._video_capture,
            self._analyzer,
            stop_event=self.stop_event,
            show_window=show_window,
            on_status=self._notify,
            on_frame=self.on_frame,
        )
        if profile is None:
            self._notify("Calibration cancelled")
            return False
        self._profile = profile
        self._notify(f"Calibration complete ({profile.sample_count} samples saved)")
        return True

    def run_detection(
        self,
        profile=None,
        show_window=True,
        task2_one_shot=False,
        extra_stop_event=None,
        task2_facing_hold_s=2.0,
        task2_facing_settled_frames=12,
        task2_facing_center_tolerance=None,
        task2_facing_head_delta=None,
        task2_facing_max_s=None,
        rest_turn=None,
        rest_nod=None,
    ):
        """Step-scan for other people; always return head to centre/rest when done."""
        try:
            self._ensure_open()
            if show_window:
                reset_opencv_preview()
            profile = profile or self._profile or load_user_profile()
            if profile is None:
                self._notify("No saved profile — calibrate first")
                self.last_other_person_detected = False
                return False, False
            self._profile = profile
            self._notify(
                f"Using profile ({profile.sample_count} samples) — scanning..."
            )
            if self.ready_event is not None:
                self.ready_event.set()
            _profile, exit_reason, other_detected = run_detection_session(
                self._video_capture,
                self._analyzer,
                profile,
                stop_event=self.stop_event,
                show_window=show_window,
                on_status=self._notify,
                on_frame=self.on_frame,
                task2_one_shot=task2_one_shot,
                extra_stop_event=extra_stop_event,
                task2_facing_hold_s=task2_facing_hold_s,
                task2_facing_settled_frames=task2_facing_settled_frames,
                task2_facing_center_tolerance=task2_facing_center_tolerance,
                task2_facing_head_delta=task2_facing_head_delta,
                task2_facing_max_s=task2_facing_max_s,
                rest_turn=rest_turn,
                rest_nod=rest_nod,
            )
            self.last_other_person_detected = bool(other_detected)
            if exit_reason == "no_camera":
                self._notify("Camera unavailable — scan skipped")
                self.last_other_person_detected = False
                return True, False
            if _stop_requested(self.stop_event, extra_stop_event):
                self._notify("Face guard stopped")
                return False, False
            if task2_one_shot:
                if other_detected:
                    self._notify("Scan complete — bystander detected")
                else:
                    self._notify("Scan complete — no bystander detected")
            else:
                self._notify("Detection session ended — head at centre")
            return True, bool(other_detected)
        finally:
            if self.ready_event is not None:
                self.ready_event.clear()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    video_capture = open_camera()
    face_lm = create_face_landmarker()
    pose_lm = create_pose_landmarker()
    analyzer = create_analyzer(face_lm, pose_lm)

    print("Using MediaPipe face + pose detection.")

    try:
        if len(sys.argv) > 1 and sys.argv[1] in ("enroll", "calibrate"):
            mode = "calibrate"
        elif len(sys.argv) > 1 and sys.argv[1] in ("track", "detect"):
            mode = "detect"
        else:
            mode = choose_startup_mode()

        if mode == "quit":
            print("Exiting.")
            return

        profile = load_or_calibrate(video_capture, analyzer, mode)
        _profile, _exit_reason, _other = run_detection_session(video_capture, analyzer, profile)
    finally:
        video_capture.release()
        cv2.destroyAllWindows()
        face_lm.close()
        pose_lm.close()


if __name__ == "__main__":
    main()
