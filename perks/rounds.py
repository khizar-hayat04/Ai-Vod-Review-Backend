"""
Valorant round extractor (detection-only)
-----------------------------------------
Samples a gameplay video, OCR-reads the HUD round scores, and prints a
summary of rounds / wins. Clip cutting is disabled for faster testing.

Edit the CONFIG section below, then run:
    py rounds.py
"""

import cv2
import pytesseract
from pytesseract import Output
import os
import json
import time
import numpy as np
from collections import Counter

# =============================================================================
# CONFIG — set your paths here
# =============================================================================

# Path to the Valorant gameplay video
VIDEO_PATH = r"D:\test_Deaths.mp4"

# Where summary.json / summary.txt are saved
OUTPUT_DIR = r"D:\VOD-Project-FE\perks"

# Optional: debug crops from the latest sample (team/enemy raw + bw)
FEATURES_DIR = r"D:\VOD-Project-FE"

# Sample grid (seconds)
SAMPLE_EVERY_SEC = 30

# Set True to also write team_rounds_raw.jpg / enemy_rounds_bw.jpg each sample
SAVE_DEBUG_CROPS = False

# Reject OCR garbage (fake jumps like 1→12). Clip export is OFF for now.
MAX_SCORE = 25
# 30s sampling can miss rounds (especially 10→13); allow larger catch-up
MAX_ROUND_DELTA_PER_SAMPLE = 5

# OCR confidence gates (prefer blank over a wrong digit)
MIN_OCR_CONF = 75
MIN_AGREE_CONF = 62

# Optional Tesseract path if not on PATH:
# pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# HUD crop ratios — slightly wider so 10+ (two digits) still fit
TEAM_X = (0.415, 0.448)
ENEMY_X = (0.555, 0.588)
SCORE_Y = (0.025, 0.065)

# After 12 rounds Valorant switches sides; left/right HUD may swap meaning
HALF_TIME_ROUNDS = 12
# How many identical OCR samples before accepting a catch-up reading
CATCHUP_CONFIRM_SAMPLES = 2

# =============================================================================

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(FEATURES_DIR, exist_ok=True)


def _to_black_text_on_white(binary: np.ndarray) -> np.ndarray:
    border = np.concatenate([
        binary[0, :], binary[-1, :], binary[:, 0], binary[:, -1],
    ])
    if border.mean() < 127:
        return cv2.bitwise_not(binary)
    return binary


def _pick_digit_components(white_on_black: np.ndarray) -> np.ndarray:
    """Keep main digit blob(s). Allows a second blob for two-digit scores (10+)."""
    num, labels, stats, centroids = cv2.connectedComponentsWithStats(
        white_on_black, connectivity=8
    )
    if num <= 1:
        return white_on_black

    H, W = white_on_black.shape[:2]
    cx, cy = W / 2.0, H / 2.0
    scored = []

    for lab in range(1, num):
        area = int(stats[lab, cv2.CC_STAT_AREA])
        if area < 18:
            continue
        x = int(stats[lab, cv2.CC_STAT_LEFT])
        bw = int(stats[lab, cv2.CC_STAT_WIDTH])
        bh = int(stats[lab, cv2.CC_STAT_HEIGHT])
        if bw < 2 or bh < 2:
            continue
        aspect = bw / float(bh)
        if aspect > 2.8 or aspect < 0.08:
            continue
        touches_left = x <= 1
        touches_right = x + bw >= W - 2
        if (touches_left or touches_right) and bw < 0.30 * W:
            continue
        if bh < 0.25 * H:
            continue
        mx, my = centroids[lab]
        dist = float(np.hypot(mx - cx, my - cy))
        score = -dist * 4.0 + (bh / H) * 25.0 + min(area, 800) * 0.02
        if abs(mx - cx) < 0.22 * W:
            score += 35.0
        scored.append((score, lab, mx))

    if not scored:
        return np.zeros_like(white_on_black)

    scored.sort(reverse=True)
    keep = [scored[0][1]]
    if len(scored) > 1:
        best_mx = scored[0][2]
        for score, lab, mx in scored[1:3]:
            if abs(mx - best_mx) < 0.55 * W and score > scored[0][0] * 0.35:
                keep.append(lab)
                break

    out = np.zeros_like(white_on_black)
    for lab in keep:
        out[labels == lab] = 255
    return out


def _has_digit_hole(black_on_white: np.ndarray) -> bool:
    ink = cv2.bitwise_not(black_on_white)
    contours, hierarchy = cv2.findContours(ink, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return False
    H, W = ink.shape[:2]
    for i, links in enumerate(hierarchy[0]):
        child = int(links[2])
        if child < 0:
            continue
        outer = float(cv2.contourArea(contours[i]))
        hole = float(cv2.contourArea(contours[child]))
        if outer < 50 or hole < 10:
            continue
        x, y, bw, bh = cv2.boundingRect(contours[i])
        if abs((x + bw / 2) - W / 2) > 0.32 * W:
            continue
        if bh < 0.30 * H:
            continue
        ratio = hole / outer
        if 0.04 <= ratio <= 0.75:
            return True
    return False


def _looks_like_zero(black_on_white: np.ndarray) -> bool:
    if not _has_digit_hole(black_on_white):
        return False
    ink = cv2.bitwise_not(black_on_white)
    ys, xs = np.where(ink > 0)
    if len(xs) < 40:
        return False
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    bw, bh = x1 - x0 + 1, y1 - y0 + 1
    aspect = bw / max(bh, 1)
    if not (0.35 <= aspect <= 1.15):
        return False
    col_fill = ink[y0:y1 + 1, x0:x1 + 1].mean(axis=0) / 255.0
    if col_fill.size < 3:
        return False
    left_band = float(col_fill[: max(1, len(col_fill) // 4)].mean())
    right_band = float(col_fill[-max(1, len(col_fill) // 4):].mean())
    mid_band = float(col_fill[len(col_fill) // 3: 2 * len(col_fill) // 3].mean())
    if left_band < 0.15 or right_band < 0.15:
        return False
    if mid_band > 0.85:
        return False
    return True


def _mask_looks_valid(black_on_white: np.ndarray) -> bool:
    ink = cv2.bitwise_not(black_on_white)
    ys, xs = np.where(ink > 0)
    if len(xs) < 25:
        return False
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    bw, bh = x1 - x0 + 1, y1 - y0 + 1
    H, W = ink.shape[:2]
    if bh < 0.30 * H or bw < 0.12 * W:
        return False
    touches_left = x0 <= 2
    touches_right = x1 >= W - 3
    if (touches_left or touches_right) and bw < 0.40 * W:
        return False
    if abs(xs.mean() - W / 2) > 0.32 * W:
        return False
    aspect = bw / max(bh, 1)
    if aspect > 2.3 or aspect < 0.12:
        return False
    return True


def _preprocess_white_digits(bgr: np.ndarray) -> list:
    """
    Isolate bright-white HUD digits. CLAHE/Otsu often destroys 10–13 against
    teal/gray panels and leaves only the trailing digit (13→3, 12→1).
    """
    up = cv2.resize(bgr, None, fx=6, fy=6, interpolation=cv2.INTER_CUBIC)
    mn = up.min(axis=2).astype(np.uint8)
    open_k = np.ones((2, 2), np.uint8)
    close_k = np.ones((3, 3), np.uint8)
    variants = []
    seen = set()

    p85 = float(np.percentile(mn, 85))
    for floor in (170, 185, 200, 210):
        thr = max(int(p85), floor)
        _, bright = cv2.threshold(mn, thr, 255, cv2.THRESH_BINARY)
        m = cv2.morphologyEx(bright, cv2.MORPH_OPEN, open_k, iterations=1)
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, close_k, iterations=1)
        # Do NOT collapse to a single connected component — that turns "13" into "3"
        if cv2.countNonZero(m) < 25:
            continue
        bw = _to_black_text_on_white(m)
        ink = cv2.bitwise_not(bw)
        if cv2.countNonZero(ink) < 25:
            continue
        key = hash(bw.tobytes())
        if key in seen:
            continue
        seen.add(key)
        variants.append(bw)
    return variants


def _is_plausible_round(digits: str) -> bool:
    if not digits or not digits.isdigit() or len(digits) > 2:
        return False
    return 0 <= int(digits) <= MAX_SCORE


def _ocr_with_confidence(image: np.ndarray) -> tuple:
    best_digits, best_conf = "", -1.0
    # Prefer multi-character PSMs so "13" / "12" are not forced to one digit
    for psm in (7, 8, 6, 13, 10):
        config = f"--oem 3 --psm {psm} -c tessedit_char_whitelist=0123456789"
        try:
            text = pytesseract.image_to_string(image, config=config)
        except pytesseract.TesseractError:
            continue
        digits = "".join(ch for ch in (text or "") if ch.isdigit())
        if not _is_plausible_round(digits):
            continue
        # image_to_string has no conf — use length bias (prefer 2-digit when present)
        conf = 80.0 + (10.0 if len(digits) == 2 else 0.0)
        try:
            data = pytesseract.image_to_data(image, config=config, output_type=Output.DICT)
            for i, txt in enumerate(data.get("text", [])):
                d2 = "".join(ch for ch in (txt or "") if ch.isdigit())
                if d2 == digits:
                    conf = max(conf, float(data["conf"][i]))
        except Exception:
            pass
        if conf > best_conf:
            best_conf = conf
            best_digits = digits
    return best_digits, best_conf


def _choose_confident_digits(candidates: list) -> str:
    usable = [(d, c) for d, c in candidates if _is_plausible_round(d) and c >= 50]
    if not usable:
        return ""

    # Prefer two-digit readings when available (fixes 13→3 / 12→1)
    two = [(d, c) for d, c in usable if len(d) == 2]
    pool = two if two else usable

    best_d, best_c = max(pool, key=lambda x: (len(x[0]), x[1]))
    if best_c >= MIN_AGREE_CONF or len(best_d) == 2:
        return best_d

    counts = Counter(d for d, _ in pool)
    digit, n = counts.most_common(1)[0]
    if n >= 2:
        return digit
    return ""


def extract_number_from_crop(cropped, label="Score", filename="score", quiet=False):
    """OCR a HUD score crop. Returns digit string, or '' if unclear."""
    if SAVE_DEBUG_CROPS:
        cv2.imwrite(os.path.join(FEATURES_DIR, f"{filename}_raw.jpg"), cropped)

    if cropped is None or cropped.size == 0 or min(cropped.shape[:2]) < 3:
        if not quiet:
            print(f"  {label} -> [Not Detected]")
        return ""

    variants = _preprocess_white_digits(cropped)
    bw_path = os.path.join(FEATURES_DIR, f"{filename}_bw.jpg")

    if not variants:
        if SAVE_DEBUG_CROPS:
            blank = np.full((80, 80), 255, np.uint8)
            cv2.imwrite(bw_path, blank)
        if not quiet:
            print(f"  {label} -> [Not Detected]")
        return ""

    candidates = []
    best_debug, best_debug_score = variants[0], -1.0
    for bw in variants:
        padded = cv2.copyMakeBorder(bw, 28, 28, 28, 28, cv2.BORDER_CONSTANT, value=255)
        digits, conf = _ocr_with_confidence(padded)
        if digits == "4" and conf < 88 and len(digits) == 1:
            digits, conf = "", -1.0
        candidates.append((digits, conf))
        if conf > best_debug_score:
            best_debug_score, best_debug = conf, padded

    if SAVE_DEBUG_CROPS:
        cv2.imwrite(bw_path, best_debug)

    digits = _choose_confident_digits(candidates)
    if not quiet:
        print(f"  {label} -> {digits if digits else '[Not Detected]'}")
    return digits


def crop_scores(frame, pad_x=0.0):
    h, w = frame.shape[:2]
    y1, y2 = int(h * SCORE_Y[0]), int(h * SCORE_Y[1])
    tx1 = max(0.0, TEAM_X[0] - pad_x)
    tx2 = min(1.0, TEAM_X[1] + pad_x)
    ex1 = max(0.0, ENEMY_X[0] - pad_x)
    ex2 = min(1.0, ENEMY_X[1] + pad_x)
    team = frame[y1:y2, int(w * tx1):int(w * tx2)]
    enemy = frame[y1:y2, int(w * ex1):int(w * ex2)]
    return team, enemy


def _parse_score(text):
    return int(text) if text and text.isdigit() else None


def read_scores_at(cap, t_sec, quiet=False, prev_team=0, prev_enemy=0):
    """Seek to t_sec, OCR both scores. Returns (team:int|None, enemy:int|None)."""
    cap.set(cv2.CAP_PROP_POS_MSEC, float(t_sec) * 1000.0)
    ok, frame = cap.read()
    if not ok or frame is None:
        return None, None

    team_crop, enemy_crop = crop_scores(frame)
    team = _parse_score(extract_number_from_crop(team_crop, "Team", "team_rounds", quiet=quiet))
    enemy = _parse_score(extract_number_from_crop(enemy_crop, "Enemy", "enemy_rounds", quiet=quiet))

    # If a mid/high score collapses to 0/None, retry with a wider crop (helps 7→0 and 10+)
    need_team_retry = (team is None or (team == 0 and prev_team >= 5))
    need_enemy_retry = (enemy is None or (enemy == 0 and prev_enemy >= 5))
    if need_team_retry or need_enemy_retry:
        wide_team, wide_enemy = crop_scores(frame, pad_x=0.012)
        if need_team_retry:
            retry = _parse_score(
                extract_number_from_crop(wide_team, "Team", "team_rounds", quiet=True)
            )
            if retry is not None and retry >= max(prev_team, team or 0):
                team = retry
        if need_enemy_retry:
            retry = _parse_score(
                extract_number_from_crop(wide_enemy, "Enemy", "enemy_rounds", quiet=True)
            )
            if retry is not None and retry >= max(prev_enemy, enemy or 0):
                enemy = retry

    return team, enemy


def score_events(prev_team, prev_enemy, team, enemy):
    """
    Build an ordered list of round winners for this sample.
    A jump of +2 on one side => two round endings for that side.
    """
    events = []
    if team is not None and team > prev_team:
        events.extend(["team"] * (team - prev_team))
    if enemy is not None and enemy > prev_enemy:
        events.extend(["enemy"] * (enemy - prev_enemy))
    return events


def is_valid_score_pair(team, enemy):
    if team is None or enemy is None:
        return False
    return 0 <= team <= MAX_SCORE and 0 <= enemy <= MAX_SCORE


def is_valid_transition(prev_team, prev_enemy, team, enemy, max_delta=None):
    """Block OCR garbage: no drops, no absurd jumps."""
    if not is_valid_score_pair(team, enemy):
        return False
    if team < prev_team or enemy < prev_enemy:
        return False
    dt = team - prev_team
    de = enemy - prev_enemy
    if dt == 0 and de == 0:
        return False
    limit = MAX_ROUND_DELTA_PER_SAMPLE if max_delta is None else max_delta
    if dt > limit or de > limit:
        return False
    if not (1 <= dt + de <= limit):
        return False
    return True


def _expand_score_guesses(prev: int, raw):
    """
    If OCR only caught the trailing digit of 10–13 (13→3, 12→1), reconstruct.
    """
    if raw is None:
        return []
    opts = [raw]
    if prev >= 9 and 0 <= raw <= 9:
        tens = 10 + raw
        if tens >= prev and tens <= MAX_SCORE:
            opts.append(tens)
    return opts


def interpret_score_reading(prev_team, prev_enemy, raw_team, raw_enemy):
    """
    Map raw OCR into a usable scoreboard update.

    Handles:
    - team 7/8/9 misread as 0
    - one side OCR miss
    - left/right swap after halftime
    - two-digit scores misread as trailing digit only (13→3)
    """
    candidates = []

    def consider(team, enemy, tag, bonus=0, max_delta=None):
        if team is None or enemy is None:
            return
        if not (0 <= team <= MAX_SCORE and 0 <= enemy <= MAX_SCORE):
            return
        if team == prev_team and enemy == prev_enemy:
            candidates.append((0, bonus, team, enemy, tag))  # stable
            return
        if is_valid_transition(prev_team, prev_enemy, team, enemy, max_delta=max_delta):
            delta = (team - prev_team) + (enemy - prev_enemy)
            candidates.append((delta, bonus, team, enemy, tag))

    team_opts = _expand_score_guesses(prev_team, raw_team) or ([raw_team] if raw_team is not None else [])
    enemy_opts = _expand_score_guesses(prev_enemy, raw_enemy) or ([raw_enemy] if raw_enemy is not None else [])

    # Late game (9+) often skips samples — allow larger catch-up to 13-12
    late = prev_team >= 9 or prev_enemy >= 9
    catchup_limit = 8 if late else MAX_ROUND_DELTA_PER_SAMPLE

    # Normal orientation (+ trailing-digit expansions)
    for t in team_opts:
        for e in enemy_opts:
            tag = "normal"
            if t != raw_team or e != raw_enemy:
                tag = "tens_repair"
            delta = 0
            if t is not None and e is not None:
                delta = (t - prev_team) + (e - prev_enemy)
            md = catchup_limit if (late or tag == "tens_repair") else None
            bonus = 5 if tag == "normal" else 3
            if delta > MAX_ROUND_DELTA_PER_SAMPLE:
                tag = f"{tag}_catchup"
                bonus = 2
            consider(t, e, tag, bonus=bonus, max_delta=md)

    # One-sided updates when the other crop fails
    for t in team_opts:
        if raw_enemy is None:
            consider(t, prev_enemy, "team_only", bonus=1, max_delta=catchup_limit)
    for e in enemy_opts:
        if raw_team is None:
            consider(prev_team, e, "enemy_only", bonus=1, max_delta=catchup_limit)

    # Classic glitch: mid/high digit misread as 0 (especially 7→0 after halftime)
    if raw_team == 0 and prev_team >= 5:
        if raw_enemy is None or raw_enemy == prev_enemy:
            consider(prev_team, prev_enemy, "fix_team_zero_hold", bonus=4)
        elif raw_enemy > prev_enemy:
            # Enemy moved; keep last good team score
            consider(prev_team, raw_enemy, "fix_team_zero_enemy_up", bonus=3)
        elif raw_enemy < prev_enemy and prev_team + prev_enemy >= HALF_TIME_ROUNDS:
            # Likely side-swap: right value is actually team progress
            consider(raw_enemy, prev_enemy, "swap_right_as_team", bonus=1)
    if raw_enemy == 0 and prev_enemy >= 5:
        if raw_team is None or raw_team == prev_team:
            consider(prev_team, prev_enemy, "fix_enemy_zero_hold", bonus=4)
        elif raw_team > prev_team:
            consider(raw_team, prev_enemy, "fix_enemy_zero_team_up", bonus=3)
    if raw_team == 0 and raw_enemy == 0 and prev_team + prev_enemy >= 5:
        consider(prev_team, prev_enemy, "fix_both_zero", bonus=5)

    # After halftime, try swapped left/right meaning
    if prev_team + prev_enemy >= HALF_TIME_ROUNDS:
        consider(raw_enemy, raw_team, "swapped", bonus=2)
        if raw_enemy == 0 and raw_team is not None and prev_team >= 5:
            consider(prev_team, raw_team, "swapped_fix_team0", bonus=2)
        if raw_team == 0 and raw_enemy is not None and prev_enemy >= 5:
            consider(raw_enemy, prev_enemy, "swapped_fix_enemy0", bonus=2)
        if raw_team is not None and raw_enemy is None:
            consider(prev_team, raw_team, "swapped_team_as_enemy", bonus=0)
        if raw_enemy is not None and raw_team is None:
            consider(raw_enemy, prev_enemy, "swapped_enemy_as_team", bonus=0)

    if not candidates:
        return None, None, "none"

    # Prefer stable holds, then higher bonus, then smaller deltas
    candidates.sort(key=lambda x: (0 if x[0] == 0 else 1, -x[1], x[0]))
    delta, bonus, team, enemy, tag = candidates[0]
    return team, enemy, tag


def split_window(start_sec, end_sec, n, step=SAMPLE_EVERY_SEC):
    """Split [start_sec, end_sec] into n segments on the sample grid."""
    start_sec = int(start_sec)
    end_sec = int(end_sec)
    if n <= 0:
        return []
    if end_sec <= start_sec:
        end_sec = start_sec + step
    if n == 1 or (end_sec - start_sec) < step * n:
        return [(start_sec, end_sec)] * n

    bounds = [start_sec]
    duration = end_sec - start_sec
    for i in range(1, n):
        raw = start_sec + duration * i / float(n)
        snapped = int(round(raw / step) * step)
        snapped = max(bounds[-1] + step, min(snapped, end_sec - step))
        bounds.append(snapped)
    bounds.append(end_sec)

    segments = []
    for i in range(len(bounds) - 1):
        a, b = bounds[i], bounds[i + 1]
        if b <= a:
            b = a + step
        segments.append((a, b))
    return segments


def format_ts(sec):
    sec = int(sec)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def process_video(video_path=VIDEO_PATH, output_dir=OUTPUT_DIR):
    if not os.path.isfile(video_path):
        raise FileNotFoundError(f"Video not found: {video_path}")

    os.makedirs(output_dir, exist_ok=True)
    t0 = time.perf_counter()

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    duration = frame_count / fps if fps > 0 else 0
    last_sample = int(duration // SAMPLE_EVERY_SEC) * SAMPLE_EVERY_SEC

    print("=" * 60)
    print("Valorant Round Extractor (NO CLIPS — detection only)")
    print(f"Video   : {video_path}")
    print(f"Duration: {format_ts(duration)} ({duration:.1f}s)")
    print(f"Sample  : every {SAMPLE_EVERY_SEC}s  (0 .. {last_sample})")
    print(f"Summary : {output_dir}")
    print("=" * 60)

    prev_team, prev_enemy = 0, 0
    window_start = 0
    rounds = []
    # Catch-up: require the same interpreted score twice before accepting a jump
    pending_catchup = None  # ((team, enemy), count)

    t = 0
    sample_i = 0
    total_samples = (last_sample // SAMPLE_EVERY_SEC) + 1

    while t <= last_sample:
        sample_i += 1
        raw_team, raw_enemy = read_scores_at(
            cap, t, quiet=True, prev_team=prev_team, prev_enemy=prev_enemy
        )
        team, enemy, how = interpret_score_reading(
            prev_team, prev_enemy, raw_team, raw_enemy
        )

        if team is None or enemy is None:
            pending_catchup = None
            print(
                f"[{format_ts(t)}] {sample_i}/{total_samples}  "
                f"held {prev_team}-{prev_enemy} "
                f"(OCR raw={raw_team}-{raw_enemy})"
            )
            t += SAMPLE_EVERY_SEC
            continue

        if team == prev_team and enemy == prev_enemy:
            pending_catchup = None
            note = f" via {how}" if how not in ("normal", "none") else ""
            print(
                f"[{format_ts(t)}] {sample_i}/{total_samples}  "
                f"score {team}-{enemy}{note}"
            )
            t += SAMPLE_EVERY_SEC
            continue

        if not is_valid_transition(prev_team, prev_enemy, team, enemy):
            pending_catchup = None
            print(
                f"[{format_ts(t)}] {sample_i}/{total_samples}  "
                f"held {prev_team}-{prev_enemy} "
                f"(rejected {team}-{enemy} from raw {raw_team}-{raw_enemy})"
            )
            t += SAMPLE_EVERY_SEC
            continue

        # For repaired/glitch interpretations, confirm on a second sample
        needs_confirm = (
            how.startswith("fix_")
            or "swap" in how
            or "tens_repair" in how
            or "catchup" in how
        )
        if needs_confirm:
            key = (team, enemy)
            if pending_catchup and pending_catchup[0] == key:
                pending_catchup = (key, pending_catchup[1] + 1)
            else:
                pending_catchup = (key, 1)
            if pending_catchup[1] < CATCHUP_CONFIRM_SAMPLES:
                print(
                    f"[{format_ts(t)}] {sample_i}/{total_samples}  "
                    f"candidate {prev_team}-{prev_enemy} -> {team}-{enemy} "
                    f"({how}, confirm {pending_catchup[1]}/{CATCHUP_CONFIRM_SAMPLES})"
                )
                t += SAMPLE_EVERY_SEC
                continue
        pending_catchup = None

        events = score_events(prev_team, prev_enemy, team, enemy)
        print(
            f"[{format_ts(t)}] {sample_i}/{total_samples}  "
            f"score {prev_team}-{prev_enemy} -> {team}-{enemy}  "
            f"<< {len(events)} round(s) ended ({how})"
        )

        segments = split_window(window_start, t, len(events))
        for winner, (seg_start, seg_end) in zip(events, segments):
            round_no = len(rounds) + 1
            rounds.append({
                "round": round_no,
                "winner": winner,
                "start_sec": seg_start,
                "end_sec": seg_end,
                "start": format_ts(seg_start),
                "end": format_ts(seg_end),
                "score_after": {"team": team, "enemy": enemy},
            })
            print(
                f"  >> Round {round_no}: {winner} win  "
                f"{format_ts(seg_start)} -> {format_ts(seg_end)}"
            )

        prev_team, prev_enemy = team, enemy
        window_start = t
        t += SAMPLE_EVERY_SEC

    cap.release()

    team_wins = sum(1 for r in rounds if r["winner"] == "team")
    enemy_wins = sum(1 for r in rounds if r["winner"] == "enemy")
    total_rounds = len(rounds)
    elapsed = time.perf_counter() - t0

    summary = {
        "video": video_path,
        "total_rounds": total_rounds,
        "team_wins": team_wins,
        "enemy_wins": enemy_wins,
        "final_score": {"team": prev_team, "enemy": prev_enemy},
        "timing_sec": round(elapsed, 2),
        "rounds": rounds,
    }

    summary_json = os.path.join(output_dir, "summary.json")
    summary_txt = os.path.join(output_dir, "summary.txt")
    with open(summary_json, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    with open(summary_txt, "w", encoding="utf-8") as f:
        f.write("Valorant Round Extraction Summary (detection only — no clips)\n")
        f.write("=============================================================\n")
        f.write(f"Video        : {video_path}\n")
        f.write(f"Total rounds : {total_rounds}\n")
        f.write(f"Team wins    : {team_wins}\n")
        f.write(f"Enemy wins   : {enemy_wins}\n")
        f.write(f"Final score  : {prev_team} - {prev_enemy}\n")
        f.write(f"Time         : {elapsed:.1f}s\n\n")
        for r in rounds:
            f.write(
                f"Round {r['round']:02d}: {r['winner']:5s}  "
                f"{r['start']} -> {r['end']}\n"
            )

    print("\n" + "=" * 60)
    print("SUMMARY (no clips saved)")
    print(f"  Total rounds : {total_rounds}")
    print(f"  Team wins    : {team_wins}")
    print(f"  Enemy wins   : {enemy_wins}")
    print(f"  Final score  : {prev_team} - {prev_enemy}")
    print(f"  Time         : {elapsed:.1f}s")
    print(f"  Summary file : {summary_txt}")
    print("=" * 60)

    return summary


if __name__ == "__main__":
    process_video()
