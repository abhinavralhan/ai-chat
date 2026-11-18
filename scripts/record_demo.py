"""Record the README demo: drive OpenWebUI with Playwright, then cut the waits and make docs/demo.gif.

Needs the stack running (ollama serve; docker compose up -d webui). Run from the repo root:
  docker run --rm --network ai-chat_default -v "$PWD":/work -w /work mcr.microsoft.com/playwright/python:v1.58.0-noble \
    sh -c "pip install -q playwright==1.58.0 && python scripts/record_demo.py"
  python3 scripts/record_demo.py --gif     # on the host, needs ffmpeg
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

URL = "http://webui:8080/?models=marketing-semantic"
TURNS = ["How are we doing?", "Net revenue per month"]  # vague first, so the agent asks back
OUT = Path("docs/demo_raw")
ANSWERS = "#response-content-container"


FAST = 25  # how much the model waits are sped up


def record() -> None:
    from playwright.sync_api import sync_playwright  # only in the Playwright container

    OUT.mkdir(parents=True, exist_ok=True)
    marks = []  # (start, end) seconds of each model wait, to fast-forward in the edit
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1100, "height": 720}, record_video_dir=str(OUT),
                                  record_video_size={"width": 1100, "height": 720})
        page = ctx.new_page()
        t0 = time.monotonic()
        page.goto(URL)
        page.wait_for_selector("#chat-input", timeout=60_000)
        page.keyboard.press("Escape")  # closes the "what's new" dialog if it shows
        page.wait_for_timeout(1500)
        for i, text in enumerate(TURNS, 1):
            page.click("#chat-input")
            page.keyboard.type(text, delay=60)
            page.keyboard.press("Enter")
            start = time.monotonic() - t0
            # the api sends the whole answer in one chunk, so any text means the answer is complete
            page.wait_for_function(f"[...document.querySelectorAll('{ANSWERS}')].filter(e => e.innerText.trim()).length >= {i}",
                                   timeout=600_000, polling=1000)
            marks.append((round(start + 2, 1), round(time.monotonic() - t0 - 1, 1)))
            page.wait_for_timeout(4000)  # let the viewer read it
        page.screenshot(path="docs/demo_last.png")
        video = page.video.path()
        ctx.close()
        browser.close()
    Path(video).rename(OUT / "raw.webm")
    (OUT / "marks.json").write_text(json.dumps(marks))
    print("waits (s):", marks)


def segments(marks: list, end: float) -> list[tuple[float, float, int]]:
    """Split the video into (start, end, speed): normal speed around the waits, FAST during them."""
    out, t = [], 0.0
    for a, b in marks:
        out += [(t, a, 1), (a, b, FAST)]
        t = b
    return out + [(t, end, 1)]


def gif() -> None:
    raw, marks = OUT / "raw.webm", json.loads((OUT / "marks.json").read_text())
    end = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                         "-of", "csv=p=0", str(raw)]))
    parts = [f"[0:v]trim={a}:{b},setpts=(PTS-STARTPTS)/{k}[v{i}]" for i, (a, b, k) in enumerate(segments(marks, end))]
    joined = "".join(f"[v{i}]" for i in range(len(parts)))
    graph = (";".join(parts) + f";{joined}concat=n={len(parts)}:v=1:a=0,fps=10,scale=900:-1:flags=lanczos,"
             "split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-filter_complex", graph, "docs/demo.gif"], check=True)
    print("wrote docs/demo.gif")


if __name__ == "__main__":
    assert segments([(5, 50)], 60) == [(0.0, 5, 1), (5, 50, FAST), (50, 60, 1)]
    gif() if "--gif" in sys.argv else record()
