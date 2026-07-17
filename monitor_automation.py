"""
Employer Monitoring System - Screen Recording Automation
==========================================================

Implements the 12-step loop you described:

 1. Read new connection entries from the log screen
 2. Buffer the first IP address
 3. Switch to the connection list section
 4. Find the row matching that IP, right-click it
 5. Click "Monitor" in the context menu
 6. Move mouse to the bottom-right resize corner of the new Monitor dialog
 7. Resize the dialog to 1.8x its original size (Windows API)
 8. Click the "Autosave" button
 9. A new File Explorer window opens (from autosave) -> close it, sleep 5s
10. Minimize the Monitor dialog
11. If there's a next IP in the buffer, process it (back to step 3);
    otherwise fall through
12. Wait, then go back to step 1 (poll log for new connections)

REQUIREMENTS
------------
pip install pyautogui pytesseract pillow opencv-python pygetwindow pywin32

You also need Tesseract-OCR installed and on PATH (Windows build:
https://github.com/UB-Mannheim/tesseract/wiki), since step 1 reads log
text off the screen (most of these dashboards don't expose an API).

SETUP YOU MUST DO BEFORE RUNNING
---------------------------------
1. Take small, tight screenshots of just these UI elements and save
   them into ./assets/ with these exact names:
     - monitor_menu_item.png   (the "Monitor" row in the right-click menu)
     - autosave_button.png     (the Autosave button inside the Monitor dialog)
   (Right-click menu + Autosave button are matched by image since they
   move around; the log region and resize corner are matched by a fixed
   screen rectangle you calibrate once below.)

2. Run the included `calibrate.py` helper (bottom of this file, or
   run this script with `--calibrate`) to print your mouse position.
   Hover over each spot and note the coordinates, then fill in the
   CONFIG block below:
     - LOG_REGION: the rectangle around the log list text
     - CONNECTION_LIST_REGION: rectangle of the connection list panel
     - (resize corner is auto-detected from the dialog's own window
       geometry, see resize_monitor_dialog())

3. Adjust IP_LOG_PATTERN / CONNECTED_TEXT if your log format differs.

SAFETY NOTES
------------
- pyautogui.FAILSAFE is left ON: slam your mouse to a screen corner
  (0,0) at any time to abort immediately if something goes wrong.
- This script only automates clicks/drags within your own monitoring
  application; it does not touch other processes.
- It closes newly-opened Explorer *windows* (not the explorer.exe
  shell process), so your taskbar/desktop stay alive.
"""

import os
import re
import sys
import time
import threading
import logging
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass, field

import pyautogui
import pygetwindow as gw
import pytesseract
from PIL import Image, ImageDraw
import pystray

# If Tesseract-OCR isn't on your Windows PATH, point pytesseract at the
# exe directly. Default install location shown below -- adjust if you
# installed elsewhere, or comment this out if it's already on PATH.
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


def _resource_root() -> str:
    """Base dir for bundled assets (PyInstaller) or the script directory."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _app_dir() -> str:
    """Writable dir next to the exe (frozen) or the script directory."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# ------------------------------------------------------------------
# CONFIG - fill these in for your machine / layout
# ------------------------------------------------------------------

# Left-panel tab buttons. Log and Connection are separate tabs, so
# switching between them requires an actual click, not just reading
# a different screen region.
# Fill in either a rectangle (left, top, width, height) or just use
# the center point directly below -- whichever you give me.
LOG_TAB_CENTER = (432, 432)          # center of the Log tab button
CONNECTION_TAB_CENTER = (432, 388)   # center of the Connection tab button

TAB_SWITCH_WAIT_SECS = 0.5  # let the panel repaint after switching tabs

# Rectangle (left, top, width, height) around the log text panel.
LOG_REGION = (481, 377, 315, 459)

# Rectangle (left, top, width, height) around the connection list panel.
CONNECTION_LIST_REGION = (481, 377, 315, 459)

ASSETS_DIR = os.path.join(_resource_root(), "assets")
MONITOR_MENU_IMAGE = os.path.join(ASSETS_DIR, "monitor_menu_item.png")
AUTOSAVE_BUTTON_IMAGE = os.path.join(ASSETS_DIR, "autosave_button.png")

IMAGE_MATCH_CONFIDENCE = 0.85   # lower this if matches keep failing (needs opencv)
MONITOR_MENU_MATCH_CONFIDENCE = 0.75
# Context menu search box around the right-click point.
CONTEXT_MENU_REGION_PAD_X = 40
CONTEXT_MENU_REGION_PAD_Y = 10
CONTEXT_MENU_REGION_WIDTH = 260
CONTEXT_MENU_REGION_HEIGHT = 220
RESIZE_FACTOR = 1.8  # Monitor dialog target = current size * this factor

SIZE_TOLERANCE_PX = 10  # if within this many px of target, skip resizing
POLL_INTERVAL_SECS = 8          # step 12 "waiting" before re-checking log
EXPLORER_CLOSE_WAIT_SECS = 5    # step 9 sleep after killing the new explorer window

# Log line format example: "9:48:14 PM      192.168.1.23   Connected"
# Allow OCR junk (quotes, pipes, etc.) between the time and the IP.
LOG_LINE_PATTERN = re.compile(
    r"(?P<time>\d{1,2}:\d{2}:\d{2}\s*[AP]M)\s*[^\d]*?"
    r"(?P<ip>\d{1,3}(?:\.\d{1,3}){3})\s*Connected",
    re.IGNORECASE,
)
IP_TOKEN_PATTERN = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
# How many OCR character mistakes to allow when matching an IP in the list.
IP_OCR_MAX_DISTANCE = 2

# ------------------------------------------------------------------
# ACTIVITY LOG - written to a rotating file next to the exe/script,
# and echoed to the console.
# ------------------------------------------------------------------
ACTIVITY_LOG_PATH = os.path.join(_app_dir(), "activity.log")

log = logging.getLogger("monitor_automation")
log.setLevel(logging.INFO)
if not log.handlers:
    _fmt = logging.Formatter("%(asctime)s  %(levelname)s  %(message)s")

    _file_handler = RotatingFileHandler(
        ACTIVITY_LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    _file_handler.setFormatter(_fmt)
    log.addHandler(_file_handler)

    _console_handler = logging.StreamHandler()
    _console_handler.setFormatter(_fmt)
    log.addHandler(_console_handler)

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.3  # small delay after every pyautogui call


def _text_preview(text: str, max_len: int = 160) -> str:
    preview = " ".join(text.split())
    if len(preview) > max_len:
        return preview[:max_len] + "..."
    return preview or "<empty>"


def locate_image_center(image_path: str, confidence: float, label: str, region=None):
    try:
        return pyautogui.locateCenterOnScreen(
            image_path, confidence=confidence, region=region
        )
    except pyautogui.ImageNotFoundException as e:
        log.warning(f"Could not locate {label}: {e}")
    except Exception:
        log.exception(f"Unexpected error while locating {label}")
    return None


def context_menu_region(anchor: tuple[int, int] | None):
    """Screen region where the right-click context menu should appear."""
    if anchor is None:
        return None
    x, y = anchor
    left = max(0, x - CONTEXT_MENU_REGION_PAD_X)
    top = max(0, y - CONTEXT_MENU_REGION_PAD_Y)
    return (left, top, CONTEXT_MENU_REGION_WIDTH, CONTEXT_MENU_REGION_HEIGHT)


def find_monitor_menu_by_ocr(region) -> tuple[int, int] | None:
    """
    OCR the context-menu region and click the word Monitor,
    never Information.
    """
    if region is None:
        return None
    screenshot = pyautogui.screenshot(region=region)
    data = pytesseract.image_to_data(screenshot, output_type=pytesseract.Output.DICT)
    words = [word.strip() for word in data["text"] if word.strip()]
    log.info(f"Context menu OCR words: {_text_preview(' '.join(words))}")

    for i, word in enumerate(data["text"]):
        cleaned = word.strip().strip("[]").strip()
        if cleaned.lower() == "monitor":
            x = region[0] + data["left"][i] + data["width"][i] // 2
            y = region[1] + data["top"][i] + data["height"][i] // 2
            log.info(f"OCR found Monitor menu text at ({x}, {y})")
            return (x, y)
    return None


def _ip_distance(a: str, b: str) -> int:
    """Simple Levenshtein distance for OCR-tolerant IP matching."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > IP_OCR_MAX_DISTANCE:
        return IP_OCR_MAX_DISTANCE + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr.append(min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost))
        prev = curr
    return prev[-1]


def _extract_ip_candidates(words: list[str]) -> list[tuple[str, int, int]]:
    """
    Pull IP-like tokens from OCR words.
    Returns (ip_text, word_index, priority) where priority 0 means the
    word itself contains the IP, and 1 means it came from joining neighbors.
    """
    candidates = []
    for i, word in enumerate(words):
        for match in IP_TOKEN_PATTERN.finditer(word):
            candidates.append((match.group(0), i, 0))
        if i + 1 < len(words):
            joined = words[i] + words[i + 1]
            for match in IP_TOKEN_PATTERN.finditer(joined):
                # Prefer the neighbor that looks more like an IP token.
                next_word = words[i + 1]
                prefer_next = bool(IP_TOKEN_PATTERN.search(next_word))
                candidates.append((match.group(0), i + 1 if prefer_next else i, 1))
    return candidates


def find_best_ip_match(target_ip: str, words: list[str]):
    """
    Exact match first, then closest OCR-tolerant IP match.
    Returns (matched_ip, word_index) or (None, None).
    """
    for i, word in enumerate(words):
        if word == target_ip:
            return target_ip, i

    best = None
    best_distance = IP_OCR_MAX_DISTANCE + 1
    best_priority = 99  # lower is better: 0=token itself, 1=joined neighbors
    for candidate, index, priority in _extract_ip_candidates(words):
        distance = _ip_distance(target_ip, candidate)
        if distance < best_distance or (
            distance == best_distance and priority < best_priority
        ):
            best = (candidate, index)
            best_distance = distance
            best_priority = priority

    if best is not None and best_distance <= IP_OCR_MAX_DISTANCE:
        return best
    return None, None


def release_ip_for_retry(ip: str):
    """Allow a failed IP to be picked up again on a later poll."""
    if ip in state.skipped_ips:
        log.info(f"Not releasing skipped IP: {ip}")
        return
    state.seen_ips.discard(ip)
    log.info(f"Released {ip} for retry on next poll")


def skip_ip(ip: str, matched_ip: str | None = None, was_fuzzy: bool = False):
    """
    Permanently skip an IP after a successful run (especially fuzzy matches),
    so later polls do not reprocess it.
    """
    state.seen_ips.add(ip)
    state.skipped_ips.add(ip)
    if matched_ip and matched_ip != ip:
        state.seen_ips.add(matched_ip)
        state.skipped_ips.add(matched_ip)
        log.info(f"Skipped IP: {ip} (fuzzy OCR match was {matched_ip})")
    elif was_fuzzy:
        log.info(f"Skipped IP: {ip} (fuzzy)")
    else:
        log.info(f"Skipped IP: {ip}")


# ------------------------------------------------------------------
# STATE
# ------------------------------------------------------------------

@dataclass
class State:
    seen_ips: set = field(default_factory=set)   # already-processed, avoid re-triggering
    skipped_ips: set = field(default_factory=set)  # permanently skipped after success
    pending_ips: list = field(default_factory=list)
    last_right_click: tuple[int, int] | None = None


state = State()


# ------------------------------------------------------------------
# TAB SWITCHING - left panel has separate Log / Connection tabs
# ------------------------------------------------------------------

def switch_to_log_tab():
    pyautogui.click(LOG_TAB_CENTER)
    time.sleep(TAB_SWITCH_WAIT_SECS)
    log.info("Switched to Log tab")


def switch_to_connection_tab():
    pyautogui.click(CONNECTION_TAB_CENTER)
    time.sleep(TAB_SWITCH_WAIT_SECS)
    log.info("Switched to Connection tab")


# ------------------------------------------------------------------
# STEP 1-2: read log, buffer new IPs
# ------------------------------------------------------------------

def read_new_connections() -> list:
    """OCR the log region and return newly-seen 'Connected' IPs, in order."""
    log.info(f"Reading log region: {LOG_REGION}")
    switch_to_log_tab()

    log.info("Capturing log screenshot")
    screenshot = pyautogui.screenshot(region=LOG_REGION)
    log.info("Running OCR on log screenshot")
    text = pytesseract.image_to_string(screenshot)
    log.info(f"OCR log text preview: {_text_preview(text)}")

    new_ips = []
    for match in LOG_LINE_PATTERN.finditer(text):
        ip = match.group("ip")
        if ip in state.skipped_ips:
            log.info(f"Skipping permanently skipped IP: {ip}")
            continue
        if ip not in state.seen_ips:
            state.seen_ips.add(ip)
            new_ips.append(ip)
        else:
            log.info(f"Skipping already-seen IP: {ip}")

    if new_ips:
        log.info(f"New connections found: {new_ips}")
    else:
        log.info("No new connected IPs found in log text")
    return new_ips


# ------------------------------------------------------------------
# STEP 3-4: switch to connection list, find + right-click the IP row
# ------------------------------------------------------------------

def find_and_right_click_ip(ip: str) -> tuple[bool, bool, str | None]:
    """
    OCR the connection list region to locate the row containing `ip`,
    then right-click at that row's vertical position.
    Returns (found, was_fuzzy, matched_ip).
    """
    log.info(f"Looking for IP in connection list: {ip}")
    switch_to_connection_tab()

    log.info(f"Capturing connection list screenshot: {CONNECTION_LIST_REGION}")
    screenshot = pyautogui.screenshot(region=CONNECTION_LIST_REGION)
    log.info("Running OCR on connection list screenshot")
    data = pytesseract.image_to_data(screenshot, output_type=pytesseract.Output.DICT)
    words = [word.strip() for word in data["text"] if word.strip()]
    log.info(f"Connection list OCR words preview: {_text_preview(' '.join(words))}")

    # Map cleaned words back to original OCR indices for click coordinates.
    cleaned_to_raw = []
    for i, word in enumerate(data["text"]):
        cleaned = word.strip()
        if cleaned:
            cleaned_to_raw.append(i)

    matched_ip, cleaned_index = find_best_ip_match(ip, words)
    if matched_ip is None or cleaned_index is None:
        log.warning(f"IP {ip} not found in connection list")
        return False, False, None

    raw_index = cleaned_to_raw[cleaned_index]
    was_fuzzy = matched_ip != ip
    if was_fuzzy:
        log.info(
            f"Fuzzy-matched OCR IP '{matched_ip}' to target '{ip}' "
            f"(distance={_ip_distance(ip, matched_ip)})"
        )

    x = CONNECTION_LIST_REGION[0] + data["left"][raw_index] + data["width"][raw_index] // 2
    y = CONNECTION_LIST_REGION[1] + data["top"][raw_index] + data["height"][raw_index] // 2
    pyautogui.rightClick(x, y)
    state.last_right_click = (x, y)
    log.info(f"Right-clicked row for {ip} at ({x}, {y})")
    time.sleep(0.4)  # let the context menu appear
    return True, was_fuzzy, matched_ip


# ------------------------------------------------------------------
# STEP 5: click "Monitor" in the context menu
# ------------------------------------------------------------------

def click_monitor_menu_item() -> bool:
    """
    Select Monitor from the right-click menu via keyboard.
    Menu order is fixed:
      1. Tab Notify
      2. Information
      3. Monitor  <-- target
      4. Run File
      5. WebCam
    """
    if state.last_right_click is None:
        log.warning("No right-click anchor; cannot open Monitor via keyboard")
        return False

    log.info(
        "Selecting Monitor menu item with keyboard "
        "(Down x3 + Enter) after right-click at "
        f"{state.last_right_click}"
    )
    time.sleep(0.35)  # let the context menu fully appear and take focus
    pyautogui.press("down")  # Tab Notify
    time.sleep(0.08)
    pyautogui.press("down")  # Information
    time.sleep(0.08)
    pyautogui.press("down")  # Monitor
    time.sleep(0.08)
    pyautogui.press("enter")
    log.info("Sent Down x3 + Enter for Monitor menu item")
    time.sleep(1)  # let the dialog open
    return True


# ------------------------------------------------------------------
# STEP 6-7: resize Monitor dialog via Windows API (pygetwindow)
# ------------------------------------------------------------------

def get_monitor_window():
    """
    Return the Monitor dialog window.
    Prefer a title containing 'Monitor'; fall back to the active window.
    """
    time.sleep(0.8)  # let the dialog finish opening
    candidates = []
    for win in gw.getAllWindows():
        title = (win.title or "").strip()
        if not title:
            continue
        if "monitor" in title.lower():
            candidates.append(win)

    if candidates:
        # Prefer the most recently focused / largest candidate
        win = max(candidates, key=lambda w: (w.width * w.height))
        log.info(f"Found Monitor window by title: '{win.title}' ({win.width}x{win.height})")
        return win

    win = gw.getActiveWindow()
    if win is not None:
        log.info(
            f"No title match for Monitor; using active window: "
            f"'{win.title}' ({win.width}x{win.height})"
        )
    return win


def resize_monitor_dialog() -> bool:
    """
    Resize the Monitor dialog to RESIZE_FACTOR x its current width/height
    using pygetwindow.resizeTo (Windows API), then verify the result.
    """
    win = get_monitor_window()
    if win is None:
        log.warning("No Monitor window found for resize")
        return False

    try:
        win.activate()
    except Exception as e:
        log.warning(f"Could not activate Monitor window: {e}")

    time.sleep(0.2)
    orig_width, orig_height = win.width, win.height
    target_width = int(round(orig_width * RESIZE_FACTOR))
    target_height = int(round(orig_height * RESIZE_FACTOR))

    width_diff = abs(orig_width - target_width)
    height_diff = abs(orig_height - target_height)
    if width_diff <= SIZE_TOLERANCE_PX and height_diff <= SIZE_TOLERANCE_PX:
        log.info(
            f"Dialog already at target size ({orig_width}x{orig_height}), "
            f"skipping resize"
        )
        return True

    log.info(
        f"Resizing Monitor dialog via API from {orig_width}x{orig_height} "
        f"to {target_width}x{target_height} (x{RESIZE_FACTOR})"
    )
    try:
        win.resizeTo(target_width, target_height)
    except Exception:
        log.exception("win.resizeTo failed")
        return False

    time.sleep(0.3)
    # Re-read size after the API call
    new_width, new_height = win.width, win.height
    if (
        abs(new_width - target_width) > SIZE_TOLERANCE_PX
        or abs(new_height - target_height) > SIZE_TOLERANCE_PX
    ):
        log.warning(
            f"Resize verification failed: wanted {target_width}x{target_height}, "
            f"got {new_width}x{new_height}"
        )
        return False

    log.info(f"Resized Monitor dialog confirmed at {new_width}x{new_height}")
    return True


# ------------------------------------------------------------------
# STEP 8: click Autosave (OCR inside Monitor window)
# ------------------------------------------------------------------

def _normalize_ocr_token(word: str) -> str:
    return word.strip().strip("[](){}").strip().lower()


def find_autosave_by_ocr(region) -> tuple[int, int] | None:
    """
    OCR the Monitor window region and return the center of the AutoSave text.
    Accepts AutoSave / Auto-Save / Auto Save (split tokens).
    """
    if region is None:
        return None

    screenshot = pyautogui.screenshot(region=region)
    data = pytesseract.image_to_data(screenshot, output_type=pytesseract.Output.DICT)
    words = [word.strip() for word in data["text"] if word.strip()]
    log.info(f"Monitor window OCR words: {_text_preview(' '.join(words))}")

    # Single-token match: AutoSave, Auto-Save, [AutoSave], etc.
    for i, word in enumerate(data["text"]):
        cleaned = _normalize_ocr_token(word).replace("-", "").replace("_", "")
        if cleaned == "autosave":
            x = region[0] + data["left"][i] + data["width"][i] // 2
            y = region[1] + data["top"][i] + data["height"][i] // 2
            log.info(f"OCR found AutoSave text at ({x}, {y})")
            return (x, y)

    # Split-token match: "Auto" + "Save"
    cleaned_words = []
    for i, word in enumerate(data["text"]):
        cleaned = _normalize_ocr_token(word)
        if cleaned:
            cleaned_words.append((i, cleaned))

    for idx in range(len(cleaned_words) - 1):
        i1, w1 = cleaned_words[idx]
        i2, w2 = cleaned_words[idx + 1]
        if w1 == "auto" and w2.replace("-", "") == "save":
            x1 = region[0] + data["left"][i1] + data["width"][i1] // 2
            y1 = region[1] + data["top"][i1] + data["height"][i1] // 2
            x2 = region[0] + data["left"][i2] + data["width"][i2] // 2
            y2 = region[1] + data["top"][i2] + data["height"][i2] // 2
            x, y = (x1 + x2) // 2, (y1 + y2) // 2
            log.info(f"OCR found Auto + Save text at ({x}, {y})")
            return (x, y)

    return None


def click_autosave_button() -> bool:
    """Click AutoSave by OCR inside the Monitor dialog bounds."""
    win = get_monitor_window()
    if win is None:
        log.warning("No Monitor window found for Autosave OCR")
        return False

    left = max(0, win.left)
    top = max(0, win.top)
    width = max(1, win.width)
    height = max(1, win.height)
    # AutoSave is typically in the top toolbar — OCR the upper portion first
    # for speed/accuracy, then fall back to the full window.
    top_region = (left, top, width, max(80, height // 4))
    full_region = (left, top, width, height)

    log.info(f"Searching for AutoSave via OCR in Monitor top region: {top_region}")
    location = find_autosave_by_ocr(top_region)
    if location is None:
        log.info(f"AutoSave not in top region; OCR full Monitor window: {full_region}")
        location = find_autosave_by_ocr(full_region)

    if location is None:
        log.warning("Could not find AutoSave text in Monitor window")
        return False

    pyautogui.click(location)
    log.info(f"Clicked AutoSave at {location}")
    time.sleep(0.5)
    return True


# ------------------------------------------------------------------
# STEP 9: close the newly-opened Explorer window, wait
# ------------------------------------------------------------------

def close_new_explorer_window():
    """
    Close only the newly-spawned File Explorer *window*
    (not the explorer.exe shell process).
    """
    time.sleep(1.5)  # give the window time to actually appear
    explorer_windows = [w for w in gw.getAllWindows() if "File Explorer" in w.title]
    if not explorer_windows:
        log.info("No new File Explorer window found to close")
    for w in explorer_windows:
        try:
            w.close()
            log.info(f"Closed Explorer window: {w.title}")
        except Exception as e:
            log.warning(f"Could not close window '{w.title}': {e}")

    time.sleep(EXPLORER_CLOSE_WAIT_SECS)


# ------------------------------------------------------------------
# STEP 10: minimize the Monitor dialog
# ------------------------------------------------------------------

def minimize_monitor_dialog():
    win = gw.getActiveWindow()
    if win is not None:
        win.minimize()
        log.info("Minimized Monitor dialog")


# ------------------------------------------------------------------
# MAIN LOOP - steps 1 through 12
# ------------------------------------------------------------------

def process_single_ip(ip: str):
    log.info(f"--- Processing {ip} ---")

    # 3. switch to Connection tab (handled inside find_and_right_click_ip)
    # 4. find + right click
    found, was_fuzzy, matched_ip = find_and_right_click_ip(ip)
    if not found:
        release_ip_for_retry(ip)
        return

    # 5. click Monitor
    if not click_monitor_menu_item():
        release_ip_for_retry(ip)
        return

    # 6-7. move to corner + resize (skipped if already at target size)
    if not resize_monitor_dialog():
        release_ip_for_retry(ip)
        return

    # 8. autosave
    if not click_autosave_button():
        release_ip_for_retry(ip)
        return

    # 9. kill new explorer window, sleep
    close_new_explorer_window()

    # 10. minimize
    minimize_monitor_dialog()

    # 11. permanently skip this IP (important for fuzzy OCR matches)
    skip_ip(ip, matched_ip=matched_ip, was_fuzzy=was_fuzzy)

    log.info(f"--- Finished {ip} ---")


def main_loop(stop_event: threading.Event):
    """Run steps 1-12 until stop_event is set."""
    log.info("Monitoring automation loop started.")
    try:
        while not stop_event.is_set():
            log.info("Polling for new connections")
            # 1-2. read log, buffer new ips
            new_ips = read_new_connections()
            state.pending_ips.extend(new_ips)
            log.info(f"Pending IP queue size: {len(state.pending_ips)}")

            # 3-11. process each buffered ip in turn
            while state.pending_ips and not stop_event.is_set():
                ip = state.pending_ips.pop(0)
                process_single_ip(ip)

            # 12. wait, then re-poll (interruptible)
            log.info(f"Waiting {POLL_INTERVAL_SECS} seconds before next poll")
            stop_event.wait(POLL_INTERVAL_SECS)
    except Exception:
        log.exception("Automation loop crashed")
    finally:
        log.info("Monitoring automation loop stopped.")


# ------------------------------------------------------------------
# START / STOP CONTROLLER
# ------------------------------------------------------------------

class AutomationController:
    """Runs the automation loop in a background thread with start/stop."""

    def __init__(self):
        self._thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        with self._lock:
            if self.running:
                log.info("Start requested, but automation is already running.")
                return
            log.info(f"Activity log path: {ACTIVITY_LOG_PATH}")
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=main_loop, args=(self._stop_event,), daemon=True
            )
            self._thread.start()
            log.info("Automation started.")

    def stop(self):
        with self._lock:
            if not self.running:
                log.info("Stop requested, but automation is not running.")
                return
            log.info("Stopping automation...")
            self._stop_event.set()
        self._thread.join(timeout=15)
        log.info("Automation stopped.")


# ------------------------------------------------------------------
# SYSTEM TRAY ICON
# ------------------------------------------------------------------

def _make_tray_image(running: bool) -> Image.Image:
    """Simple circular status icon: green when running, gray when idle."""
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    color = (46, 204, 113, 255) if running else (127, 140, 141, 255)
    draw.ellipse((6, 6, size - 6, size - 6), fill=color, outline=(44, 62, 80, 255), width=3)
    return image


def open_activity_log():
    try:
        os.startfile(ACTIVITY_LOG_PATH)  # type: ignore[attr-defined]
    except Exception as e:
        log.warning(f"Could not open activity log: {e}")


def run_tray():
    controller = AutomationController()

    def refresh(icon):
        icon.icon = _make_tray_image(controller.running)
        icon.title = (
            "Monitor Automation - Running" if controller.running
            else "Monitor Automation - Stopped"
        )
        icon.update_menu()

    def on_start(icon, _item):
        controller.start()
        refresh(icon)

    def on_stop(icon, _item):
        controller.stop()
        refresh(icon)

    def on_quit(icon, _item):
        controller.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(
            "Start", on_start, enabled=lambda _i: not controller.running
        ),
        pystray.MenuItem(
            "Stop", on_stop, enabled=lambda _i: controller.running
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open Activity Log", lambda _i, _it: open_activity_log()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )

    icon = pystray.Icon(
        "monitor_automation",
        icon=_make_tray_image(False),
        title="Monitor Automation - Stopped",
        menu=menu,
    )
    log.info("Tray icon started. Right-click the tray icon to Start/Stop.")
    icon.run()


if __name__ == "__main__":
    run_tray()