"""Build the local demo video for the Variables and Data Types lesson.

This intentionally has no network or paid-service dependency.  It produces a
small, standards-compatible H.264 MP4 from readable instructional slides.
Run it from the project root when the checked-in demo asset needs rebuilding.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "uploads" / "videos" / "python_variables_data_types.mp4"
SIZE = (1280, 720)
SLIDE_SECONDS = 30

SLIDES = [
    ("Variables and Data Types", "Python Fundamentals · Getting Started", "In this 6-minute lesson, learn how Python stores and labels values."),
    ("What is a variable?", "A variable is a name that refers to a value.", "Think of it as a clear label you can use again later."),
    ("Assignment", 'name = "Riya"\nage = 20', "Use = to assign a value to a name. Python works out each value's type."),
    ("Strings", 'greeting = "Hello"\ncity = \'Mumbai\'', "Strings hold text. Put text inside single or double quotes."),
    ("Numbers", "age = 20        # int\nprice = 3.14    # float", "int means whole number; float means a number with a decimal part."),
    ("Booleans", "is_enrolled = True\nhas_paid = False", "Booleans are exactly True or False. They help programs make decisions."),
    ("Check a type", 'value = "Riya"\nprint(type(value))', "type() tells you what kind of value Python is working with."),
    ("Good names", "user_name = \"Riya\"\nscore2 = 95", "Use letters, digits, and underscores. A name cannot start with a digit."),
    ("Reassignment", "streak = 3\nstreak = streak + 1", "A variable can be assigned a new value as your program changes."),
    ("Simple operations", "age = 20\nnext_year = age + 1\nprint(next_year)", "Values in variables can be combined to solve small problems."),
    ("Beginner practice", 'name = "Riya"\nage = 20\nprint(name)\nprint(age)', "Try this exact code in Python Practice below the video."),
    ("Recap", "variables · str · int · float · bool", "Use meaningful names, check types with type(), and practise with the Quick Check."),
]


def _font(size: int, mono: bool = False) -> ImageFont.FreeTypeFont:
    candidates = (["C:/Windows/Fonts/consola.ttf", "C:/Windows/Fonts/arial.ttf"]
                  if mono else ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf"])
    for candidate in candidates:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def _draw_wrapped(draw, text, xy, font, fill, max_width, line_gap=12):
    words, line, y = text.split(), "", xy[1]
    for word in words:
        next_line = (line + " " + word).strip()
        if draw.textbbox((0, 0), next_line, font=font)[2] > max_width and line:
            draw.text((xy[0], y), line, font=font, fill=fill)
            y += font.size + line_gap
            line = word
        else:
            line = next_line
    if line:
        draw.text((xy[0], y), line, font=font, fill=fill)


def make_slide(index: int, title: str, code: str, explanation: str) -> Image.Image:
    image = Image.new("RGB", SIZE, "#101827")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, SIZE[0], 12), fill="#48bb78")
    draw.text((85, 62), "PYTHON FUNDAMENTALS  /  VARIABLES AND DATA TYPES",
              font=_font(24), fill="#9fb3c8")
    _draw_wrapped(draw, title, (85, 130), _font(54), "#f7fafc", 1090)
    draw.rounded_rectangle((85, 250, 1195, 500), radius=20, fill="#1f2937")
    code_font = _font(37, mono=True)
    y = 295
    for line in code.splitlines():
        draw.text((130, y), line, font=code_font, fill="#a7f3d0")
        y += 50
    _draw_wrapped(draw, explanation, (85, 555), _font(29), "#d9e2ec", 1080, 10)
    draw.text((85, 670), f"{index + 1:02d} / {len(SLIDES):02d}", font=_font(20), fill="#829ab1")
    return image


def main() -> None:
    ffmpeg = shutil.which("ffmpeg") or r"C:\Program Files\BlueStacks_nxt\ffmpeg.exe"
    if not Path(ffmpeg).is_file():
        raise SystemExit("ffmpeg is required to rebuild the local video asset.")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen([
        ffmpeg, "-y", "-f", "rawvideo", "-pixel_format", "rgb24",
        "-video_size", f"{SIZE[0]}x{SIZE[1]}", "-framerate", "1", "-i", "-",
        "-pix_fmt", "yuv420p", "-c:v", "libopenh264", "-movflags", "+faststart",
        str(OUTPUT),
    ], stdin=subprocess.PIPE)
    try:
        for index, slide in enumerate(SLIDES):
            frame = make_slide(index, *slide).tobytes()
            for _ in range(SLIDE_SECONDS):
                process.stdin.write(frame)
        process.stdin.close()
        if process.wait() != 0:
            raise SystemExit("ffmpeg could not encode the local video asset.")
    finally:
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
    print(OUTPUT)


if __name__ == "__main__":
    main()
