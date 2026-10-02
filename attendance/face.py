import base64
import contextlib
import importlib
import io
import warnings
from pathlib import Path

FACE_LIB_AVAILABLE = None
face_recognition = None
np = None
Image = None
cv2 = None
FACE_BACKEND = None
FACE_CASCADE = None


def _load_face_lib():
    global FACE_LIB_AVAILABLE, face_recognition, np, Image, cv2, FACE_BACKEND, FACE_CASCADE
    if FACE_LIB_AVAILABLE is not None:
        return FACE_LIB_AVAILABLE

    try:
        warnings.filterwarnings(
            "ignore",
            message=r".*pkg_resources is deprecated as an API.*",
            category=UserWarning,
        )
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            face_recognition = importlib.import_module("face_recognition")
        np = importlib.import_module("numpy")
        Image = importlib.import_module("PIL.Image")
        FACE_LIB_AVAILABLE = True
        FACE_BACKEND = "face_recognition"
    except BaseException:
        try:
            cv2 = importlib.import_module("cv2")
            np = importlib.import_module("numpy")
            Image = importlib.import_module("PIL.Image")
            cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
            if cascade_path.exists():
                FACE_CASCADE = cv2.CascadeClassifier(str(cascade_path))
                FACE_LIB_AVAILABLE = True
                FACE_BACKEND = "opencv"
            else:
                FACE_LIB_AVAILABLE = False
                FACE_BACKEND = None
        except BaseException:
            FACE_LIB_AVAILABLE = False
            FACE_BACKEND = None
    return FACE_LIB_AVAILABLE


def is_face_available():
    return _load_face_lib()


def _image_to_array(image_bytes: bytes):
    if not _load_face_lib():
        return None
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    return np.array(image)


def extract_face_encoding(image_bytes: bytes):
    if not _load_face_lib():
        return None
    image = _image_to_array(image_bytes)
    if image is None:
        return None
    if FACE_BACKEND == "face_recognition":
        encodings = face_recognition.face_encodings(image)
        if not encodings:
            return None
        return encodings[0].tolist()

    # OpenCV fallback: use largest detected face and build normalized vector.
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    faces = FACE_CASCADE.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(50, 50))
    if len(faces) == 0:
        return None
    x, y, w, h = max(faces, key=lambda box: box[2] * box[3])
    face_crop = gray[y : y + h, x : x + w]
    face_crop = cv2.resize(face_crop, (64, 64), interpolation=cv2.INTER_AREA)
    face_crop = cv2.equalizeHist(face_crop)
    vector = face_crop.astype("float32").flatten() / 255.0
    return vector.tolist()


def decode_data_url(data_url: str):
    if "," not in data_url:
        return base64.b64decode(data_url)
    _, b64 = data_url.split(",", 1)
    return base64.b64decode(b64)


def match_face(captured_encoding, known_encodings, tolerance=0.6):
    if not _load_face_lib():
        return None
    if not captured_encoding or not known_encodings:
        return None

    try:
        candidate = np.asarray(captured_encoding, dtype="float32").reshape(-1)
    except (TypeError, ValueError):
        return None

    compatible = []
    compatible_indices = []
    for index, encoding in enumerate(known_encodings):
        try:
            known_encoding = np.asarray(encoding, dtype="float32").reshape(-1)
        except (TypeError, ValueError):
            continue
        if known_encoding.size != candidate.size or not np.isfinite(known_encoding).all():
            continue
        compatible.append(known_encoding)
        compatible_indices.append(index)

    if not compatible:
        return None

    known = np.stack(compatible)
    if FACE_BACKEND == "face_recognition":
        distances = face_recognition.face_distance(known, candidate)
        if len(distances) == 0:
            return None
        best_idx = int(np.argmin(distances))
        best_distance = float(distances[best_idx])
        if best_distance <= tolerance:
            return compatible_indices[best_idx]
        # Real camera captures vary more than a stored reference image. For live attendance,
        # a few extra points of difference are still valid if the match is clearly the best candidate.
        if len(compatible) == 1 and best_distance <= 0.75:
            return compatible_indices[best_idx]
        if len(compatible) > 1 and best_distance <= 0.72:
            return compatible_indices[best_idx]
        return None

    candidate_norm = np.linalg.norm(candidate)
    known_norm = np.linalg.norm(known, axis=1)
    denom = known_norm * candidate_norm + 1e-8
    similarities = (known @ candidate) / denom
    best_idx = int(np.argmax(similarities))
    if similarities[best_idx] >= 0.68:
        return compatible_indices[best_idx]
    return None
