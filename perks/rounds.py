import re
from dataclasses import dataclass
from pathlib import Path

import cv2

try:
    import pytesseract
except ImportError:
    pytesseract = None

OCR_WHITELIST = 'ACFHILNORSTUWY'  # letters for Won/Lost/Thrifty/Clutch
OCR_PSMS = (8, 7, 6)  # single word, single line, uniform block
SAMPLE_INTERVAL_SEC = 5
KNOWN_LABELS = ('Won', 'Lost', 'Thrifty', 'Clutch')
EXACT_LABELS = frozenset(label.upper() for label in KNOWN_LABELS) | {'THRIFY'}


@dataclass
class CropCoords:
    x_start: int
    y_start: int
    x_end: int
    y_end: int


def _preprocess_variants(gray):
    """Build OCR-ready images; white-on-dark UI text needs inversion."""
    scaled = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    blurred = cv2.GaussianBlur(scaled, (3, 3), 0)

    variants = []
    for inverted in (True, False):
        base = cv2.bitwise_not(blurred) if inverted else blurred
        _, otsu = cv2.threshold(base, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        variants.append(otsu)

        adapt = cv2.adaptiveThreshold(
            base,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            31,
            5,
        )
        variants.append(adapt)

    return variants


def _ocr_score(text: str, avg_conf: float) -> float:
    score = avg_conf
    if detect_round_labels(text):
        score += 200
    letters = re.sub(r'[^A-Za-z]', '', text)
    if letters.upper() in EXACT_LABELS:
        score += 150
    return score


def extract_text_from_region(image) -> str:
    """Run OCR tuned for bold white round-result UI text on dark backgrounds."""
    if pytesseract is None:
        raise RuntimeError(
            'pytesseract is not installed. Run: pip install pytesseract'
        )

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    best_text = ''
    best_score = -1.0

    for variant in _preprocess_variants(gray):
        for psm in OCR_PSMS:
            config = (
                f'--oem 3 --psm {psm} '
                f'-c tessedit_char_whitelist={OCR_WHITELIST}'
            )
            data = pytesseract.image_to_data(
                variant,
                config=config,
                output_type=pytesseract.Output.DICT,
            )

            words: list[str] = []
            confidences: list[int] = []
            for i, word in enumerate(data['text']):
                cleaned = word.strip()
                if not cleaned:
                    continue
                words.append(cleaned)
                conf = int(data['conf'][i])
                if conf >= 0:
                    confidences.append(conf)

            combined = ' '.join(words)
            avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
            score = _ocr_score(combined, avg_conf)

            if score > best_score:
                best_score = score
                best_text = combined

    if best_text:
        return best_text

    fallback = _preprocess_variants(gray)[0]
    config = f'--oem 3 --psm 8 -c tessedit_char_whitelist={OCR_WHITELIST}'
    return pytesseract.image_to_string(fallback, config=config).strip()


def detect_round_labels(text: str) -> list[str]:
    """Return detected round labels from OCR output (order: Won/Lost/Thrifty/Clutch)."""
    if not text:
        return []

    compact = re.sub(r'[^A-Za-z]', '', text).upper()
    found: list[str] = []

    # Exact / substring matches on compact text (handles OCR glue like "WONTHRIFTY")
    if 'WON' in compact or re.search(r'\bwon\b', text, re.IGNORECASE):
        found.append('Won')
    if 'LOST' in compact or re.search(r'\blost\b', text, re.IGNORECASE):
        found.append('Lost')
    if (
        'THRIFTY' in compact
        or 'THRIFY' in compact
        or re.search(r'\bthrifty\b', text, re.IGNORECASE)
        or re.search(r'\bthrify\b', text, re.IGNORECASE)
    ):
        found.append('Thrifty')
    if 'CLUTCH' in compact or re.search(r'\bclutch\b', text, re.IGNORECASE):
        found.append('Clutch')

    return found


def crop_frame_region(frame, coords: CropCoords):
    """Crop a BGR frame using pixel coordinates. Returns None if invalid."""
    if frame is None:
        return None

    img_height, img_width = frame.shape[:2]
    x_start, y_start, x_end, y_end = (
        coords.x_start,
        coords.y_start,
        coords.x_end,
        coords.y_end,
    )

    if x_start < 0 or y_start < 0 or x_end > img_width or y_end > img_height:
        print(
            f'Error: Coordinates out of bounds for frame '
            f'({img_width}x{img_height}).'
        )
        return None

    if x_start >= x_end or y_start >= y_end:
        print('Error: Start coordinates must be smaller than end coordinates.')
        return None

    return frame[y_start:y_end, x_start:x_end]


def analyze_cropped_region(cropped_image) -> tuple[list[str], str]:
    """OCR a cropped region and detect round labels."""
    try:
        ocr_text = extract_text_from_region(cropped_image)
    except RuntimeError as exc:
        print(f'OCR skipped: {exc}')
        return [], ''
    except pytesseract.TesseractNotFoundError:
        print(
            'OCR skipped: Tesseract is not installed or not on PATH.\n'
            'Install from https://github.com/UB-Mannheim/tesseract/wiki'
        )
        return [], ''

    return detect_round_labels(ocr_text), ocr_text


def _format_timestamp(seconds: float) -> str:
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f'{minutes:02d}:{secs:02d}'


VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v'}
DEBUG_CROPS_DIR = Path(__file__).resolve().parent / 'debug_crops'


def process_video(
    video_path: str,
    coords: CropCoords,
    interval_sec: float = SAMPLE_INTERVAL_SEC,
    save_crops: bool = True,
    crops_dir: Path | None = None,
) -> dict[str, int]:
    """
    Sample a video every `interval_sec`, crop each frame, OCR for Won/Lost.
    Saves every cropped frame and prints OCR for each sample (debug).
    """
    path = Path(video_path)
    if not path.is_file():
        print(f'Error: File not found: {video_path}')
        return {label: 0 for label in KNOWN_LABELS}

    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        print(
            f'Error: Expected a video file ({", ".join(sorted(VIDEO_EXTENSIONS))}), '
            f'got {path.suffix or "(no extension)"}: {video_path}\n'
            'Set video_file in __main__ to a real .mp4 / .mkv / etc. path.'
        )
        return {label: 0 for label in KNOWN_LABELS}

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        print(f'Error: Could not open video: {video_path}')
        return {label: 0 for label in KNOWN_LABELS}

    out_dir = crops_dir or DEBUG_CROPS_DIR
    if save_crops:
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f'Saving cropped frames to: {out_dir}')

    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    duration_sec = frame_count / fps if fps and fps > 0 else 0

    print(f'Video: {video_path}')
    if duration_sec > 0:
        print(
            f'Duration: {_format_timestamp(duration_sec)} '
            f'({duration_sec:.1f}s) — sampling every {interval_sec}s'
        )
    else:
        print(f'Sampling every {interval_sec}s')

    totals = {label: 0 for label in KNOWN_LABELS}
    sample_index = 0
    time_sec = 0.0

    while True:
        if duration_sec > 0 and time_sec > duration_sec:
            break

        cap.set(cv2.CAP_PROP_POS_MSEC, time_sec * 1000)
        ok, frame = cap.read()
        if not ok or frame is None:
            if sample_index == 0:
                print('Error: Could not read any frames from video.')
            break

        cropped = crop_frame_region(frame, coords)
        if cropped is None:
            break

        label = _format_timestamp(time_sec)
        safe_label = label.replace(':', '-')

        if save_crops:
            crop_path = out_dir / f'crop_{sample_index:04d}_{safe_label}.png'
            cv2.imwrite(str(crop_path), cropped)
            print(f'[{label}] saved: {crop_path.name}')

        outcomes, ocr_text = analyze_cropped_region(cropped)
        print(f'[{label}] OCR: {ocr_text!r}')

        if outcomes:
            for outcome in outcomes:
                totals[outcome] += 1
                print(f'[{label}] Round result: {outcome}')
        else:
            print(f'[{label}] Round result: (none)')

        sample_index += 1
        time_sec += interval_sec

    cap.release()

    print('---')
    for name in KNOWN_LABELS:
        print(f'Total {name}: {totals[name]}')
    print(f'Frames sampled: {sample_index}')
    if save_crops:
        print(f'Cropped frames folder: {out_dir}')

    return totals


def crop_frame_by_coords(image_path, output_path, x_start, y_start, x_end, y_end):
    """Single-image helper: crop, save, and check OCR for round labels."""
    image = cv2.imread(image_path)
    if image is None:
        print(f'Error: Could not load image from {image_path}')
        return None

    coords = CropCoords(x_start, y_start, x_end, y_end)
    cropped_image = crop_frame_region(image, coords)
    if cropped_image is None:
        return None

    cv2.imwrite(output_path, cropped_image)
    print(f'Successfully cropped and saved to: {output_path}')

    outcomes, ocr_text = analyze_cropped_region(cropped_image)
    print(f'OCR text: {ocr_text!r}')

    if outcomes:
        for outcome in outcomes:
            print(f'Round result: {outcome}')
    else:
        print('Round result: none of Won/Lost/Thrifty/Clutch detected.')

    return outcomes


if __name__ == '__main__':
    # MUST be a video (.mp4 / .mkv / …), not a screenshot .png
    video_file = r"D:\setups\videoplayback (4).mp4"

    # Same crop box as before (round result overlay region)
    CROP = CropCoords(
        x_start=820,
        y_start=130,
        x_end=1100,
        y_end=270,
    )

    process_video(video_file, CROP, interval_sec=SAMPLE_INTERVAL_SEC)
