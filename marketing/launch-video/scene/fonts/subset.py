"""Make the video's font: a subset of Pretendard Variable (SIL OFL 1.1) with only the glyphs the scene uses.

OFL: a subset is a Modified Version, and "Pretendard" is a Reserved Font Name, so the subset is renamed
("VR Launch Sans"). It stays under the OFL (see OFL.txt next to it).

Needs the unmodified font once (network at setup time only):
  curl -L -o /tmp/pretendard.zip https://github.com/orioncactus/pretendard/releases/download/v1.3.9/Pretendard-1.3.9.zip
  unzip -o /tmp/pretendard.zip public/variable/PretendardVariable.ttf -d .cache/fonts
Run after changing any on-screen copy:  python3 scene/fonts/subset.py
"""

from __future__ import annotations

import re
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont

PKG = Path(__file__).resolve().parents[2]
SRC = PKG / ".cache/fonts/public/variable/PretendardVariable.ttf"
OUT = PKG / "scene/fonts/vr-launch-sans.woff2"
NAME = "VR Launch Sans"


def main() -> None:
    text = "".join(p.read_text(encoding="utf-8") for p in (PKG / "scene").glob("*.[hj]*") if p.suffix in (".html", ".js"))
    hangul = set(re.findall(r"[가-힣ㄱ-ㆎ]", text))
    unicodes = set(range(0x20, 0x7F)) | set(range(0xA0, 0x100)) | {ord(c) for c in hangul}
    unicodes |= {0x2013, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D, 0x2022, 0x2026, 0x2192, 0x2190, 0x00B7, 0x2713, 0x2715}
    font = TTFont(SRC)
    opts = subset.Options()
    opts.flavor = "woff2"
    opts.layout_features = ["kern", "liga", "calt", "tnum", "ss01", "ss02", "case"]
    opts.name_IDs = ["*"]
    opts.notdef_outline = True
    sub = subset.Subsetter(opts)
    sub.populate(unicodes=unicodes)
    sub.subset(font)
    names = font["name"]
    for rec in list(names.names):
        if rec.nameID in (1, 4, 16, 21):
            rec.string = NAME
        elif rec.nameID in (6, 20):
            rec.string = NAME.replace(" ", "")
        elif rec.nameID == 3:
            rec.string = f"{NAME};subset-of-Pretendard-1.3.9"
    font.flavor = "woff2"
    font.save(OUT)
    print(f"{OUT.name}: {len(unicodes)} code points ({len(hangul)} Hangul), {OUT.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
