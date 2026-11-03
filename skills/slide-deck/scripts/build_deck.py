#!/usr/bin/env python3
"""把投影片大綱 JSON 排成 Apple 風格的 .pptx，可選擇再轉成 PDF。

用法：
  python3 build_deck.py OUTLINE.json --out DECK.pptx [--pdf] [--font NAME]

大綱格式見 references/outline-schema.md。排版全部由這支腳本決定，
大綱只放內容；格式錯誤會以 exit code 2 回報，並列出每一條錯在哪。
"""
import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

# ---------------------------------------------------------------- 設計 token

THEMES = {
    "light": {
        "bg": "FFFFFF", "text": "1D1D1F", "secondary": "6E6E73",
        "surface": "F5F5F7", "hairline": "D2D2D7", "accent": "0071E3",
    },
    "dark": {
        "bg": "000000", "text": "F5F5F7", "secondary": "A1A1A6",
        "surface": "1C1C1E", "hairline": "38383A", "accent": "2997FF",
    },
}

# Windows / Office for Mac 都有微軟正黑體，檔案拿去改不會跑字型；
# 容器裡轉 PDF 時再用 fontconfig 把它對應到 Noto Sans CJK TC。
DEFAULT_FONT = "Microsoft JhengHei"
LINUX_FALLBACK_FONT = "Noto Sans CJK TC"

SLIDE_W, SLIDE_H = Inches(13.333), Inches(7.5)
MARGIN_X = Inches(0.95)
CONTENT_W = SLIDE_W - 2 * MARGIN_X
TITLE_TOP = Inches(0.7)

# 大字要收緊字距、小字維持 0；單位是 1/100 pt。
def tracking_for(size_pt):
    if size_pt >= 48:
        return int(-size_pt * 2)      # 約 -2%
    if size_pt >= 28:
        return int(-size_pt * 1)      # 約 -1%
    return 0


# ---------------------------------------------------------------- 大綱檢查

LAYOUTS = {
    "title":      {"required": ["title"]},
    "section":    {"required": ["title"]},
    "bullets":    {"required": ["title", "bullets"]},
    "statement":  {"required": ["text"]},
    "two-column": {"required": ["title", "left", "right"]},
    "stats":      {"required": ["stats"]},
    "steps":      {"required": ["title", "steps"]},
    "quote":      {"required": ["quote"]},
    "image":      {"required": ["image"]},
    "image-text": {"required": ["title", "image", "bullets"]},
    "table":      {"required": ["title", "columns", "rows"]},
    "end":        {"required": ["title"]},
}


def validate(outline, base_dir):
    errors, warnings = [], []
    if not isinstance(outline, dict):
        return ["最外層必須是 JSON 物件"], warnings
    if outline.get("theme", "light") not in THEMES:
        errors.append(f"theme 只能是 {list(THEMES)}")
    accent = outline.get("accent")
    if accent is not None and not _is_hex(accent):
        errors.append("accent 必須是 6 碼 hex 色碼，例如 \"0071E3\"")
    slides = outline.get("slides")
    if not isinstance(slides, list) or not slides:
        errors.append("slides 必須是非空陣列")
        return errors, warnings

    for i, s in enumerate(slides, 1):
        where = f"第 {i} 頁"
        if not isinstance(s, dict):
            errors.append(f"{where}：必須是物件")
            continue
        layout = s.get("layout")
        if layout not in LAYOUTS:
            errors.append(f"{where}：layout {layout!r} 不存在，可用：{', '.join(LAYOUTS)}")
            continue
        where = f"第 {i} 頁（{layout}）"
        for key in LAYOUTS[layout]["required"]:
            if not s.get(key):
                errors.append(f"{where}：缺少 {key}")
        if "bullets" in s and s["bullets"]:
            errors += _check_bullets(s["bullets"], where)
            if len(s["bullets"]) > 6:
                warnings.append(f"{where}：{len(s['bullets'])} 個重點太多，建議拆成兩頁（最多 6 個）")
        if layout == "two-column":
            for side in ("left", "right"):
                col = s.get(side)
                if col and not isinstance(col, dict):
                    errors.append(f"{where}：{side} 必須是物件 {{heading, bullets}}")
                elif col:
                    errors += _check_bullets(col.get("bullets", []), f"{where} {side}")
        if layout == "stats":
            stats = s.get("stats") or []
            if not 1 <= len(stats) <= 4:
                errors.append(f"{where}：stats 要 1–4 個")
            for st in stats:
                if not isinstance(st, dict) or not st.get("value"):
                    errors.append(f"{where}：每個 stat 都要有 value")
        if layout == "steps":
            steps = s.get("steps") or []
            if not 2 <= len(steps) <= 5:
                errors.append(f"{where}：steps 要 2–5 個")
            for st in steps:
                if not isinstance(st, dict) or not st.get("title"):
                    errors.append(f"{where}：每個 step 都要有 title")
        if layout == "table":
            cols = s.get("columns") or []
            if len(cols) > 6:
                errors.append(f"{where}：表格最多 6 欄")
            for r, row in enumerate(s.get("rows") or [], 1):
                if not isinstance(row, list) or len(row) != len(cols):
                    errors.append(f"{where}：第 {r} 列欄數跟 columns 不一致")
            if len(s.get("rows") or []) > 8:
                warnings.append(f"{where}：表格超過 8 列，字會很小，建議拆頁")
        if layout in ("image", "image-text") and s.get("image"):
            path = _resolve(s["image"], base_dir)
            if not os.path.isfile(path):
                errors.append(f"{where}：找不到圖片 {path}")
    return errors, warnings


def _check_bullets(bullets, where):
    errs = []
    if not isinstance(bullets, list):
        return [f"{where}：bullets 必須是陣列"]
    for b in bullets:
        if isinstance(b, str):
            continue
        if not isinstance(b, dict) or not b.get("text"):
            errs.append(f"{where}：bullet 必須是字串或 {{text, sub}}")
        elif "sub" in b and not all(isinstance(x, str) for x in b["sub"]):
            errs.append(f"{where}：sub 只能放字串")
    return errs


def _is_hex(v):
    v = str(v).lstrip("#")
    return len(v) == 6 and all(c in "0123456789abcdefABCDEF" for c in v)


def _resolve(path, base_dir):
    return path if os.path.isabs(path) else os.path.join(base_dir, path)


# ---------------------------------------------------------------- 文字量估算

def _char_em(ch):
    o = ord(ch)
    if o >= 0x2E80 or 0xFF00 <= o <= 0xFFEF:
        return 1.0
    if ch == " ":
        return 0.28
    if ch.isupper():
        return 0.64
    return 0.53


def text_lines(text, size_pt, width_emu):
    width_pt = width_emu / 12700
    total = 0
    for para in str(text).split("\n"):
        w = sum(_char_em(c) for c in para) * size_pt
        total += max(1, math.ceil(w / width_pt * 1.04))
    return total


def fit_size(texts, sizes, width_emu, height_emu, spacing):
    """從大到小找第一個塞得進框的字級；全部塞不下就回傳最小的並標記 overflow。"""
    height_pt = height_emu / 12700
    for size in sizes:
        lines = sum(text_lines(t, size, width_emu) for t in texts)
        if lines * size * spacing * 1.2 <= height_pt:
            return size, False
    return sizes[-1], True


# ---------------------------------------------------------------- 繪製工具

class Deck:
    def __init__(self, outline, font, base_dir):
        self.o = outline
        self.base_dir = base_dir
        self.font = font
        self.t = dict(THEMES[outline.get("theme", "light")])
        if outline.get("accent"):
            self.t["accent"] = outline["accent"].lstrip("#").upper()
        self.prs = Presentation()
        self.prs.slide_width, self.prs.slide_height = SLIDE_W, SLIDE_H
        self.blank = self.prs.slide_layouts[6]
        self.warnings = []
        self.page = 0

    def color(self, key):
        return RGBColor.from_string(self.t.get(key, key))

    # 一頁的底
    def new_slide(self, s):
        self.page += 1
        slide = self.prs.slides.add_slide(self.blank)
        bg = slide.background.fill
        bg.solid()
        bg.fore_color.rgb = self.color("bg")
        if s.get("notes"):
            slide.notes_slide.notes_text_frame.text = s["notes"]
        return slide

    def textbox(self, slide, x, y, w, h, anchor=MSO_ANCHOR.TOP):
        box = slide.shapes.add_textbox(x, y, w, h)
        tf = box.text_frame
        tf.word_wrap = True
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        tf.vertical_anchor = anchor
        return tf

    def style_run(self, run, size, color="text", bold=False):
        f = run.font
        f.size = Pt(size)
        f.bold = bold
        f.name = self.font
        f.color.rgb = self.color(color)
        rpr = run._r.get_or_add_rPr()
        for tag in ("a:ea", "a:cs"):
            el = rpr.find(qn(tag))
            if el is None:
                el = rpr.makeelement(qn(tag), {})
                rpr.append(el)
            el.set("typeface", self.font)
        spc = tracking_for(size)
        if spc:
            rpr.set("spc", str(spc))

    def para(self, tf, text, size, color="text", bold=False, spacing=1.15,
             align=PP_ALIGN.LEFT, space_after=0, first=False):
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        p.alignment = align
        p.line_spacing = spacing
        p.space_after = Pt(space_after)
        run = p.add_run()
        run.text = str(text)
        self.style_run(run, size, color, bold)
        return p

    def text(self, slide, x, y, w, h, text, sizes, color="text", bold=False,
             spacing=1.1, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, label=""):
        sizes = sizes if isinstance(sizes, (list, tuple)) else [sizes]
        size, overflow = fit_size([text], sizes, w, h, spacing)
        if overflow:
            self.warn(f"{label or '文字'}太長，最小字級仍可能超出版面：{str(text)[:20]}…")
        tf = self.textbox(slide, x, y, w, h, anchor)
        self.para(tf, text, size, color, bold, spacing, align, first=True)
        return size

    def bullets(self, slide, x, y, w, h, items, sizes=(28, 26, 24, 22, 20, 18)):
        flat = []
        for b in items:
            if isinstance(b, str):
                flat.append(b)
            else:
                flat.append(b["text"])
                flat += b.get("sub", [])
        size, overflow = fit_size(flat, sizes, w - Inches(0.35), h, 1.55)
        if overflow:
            self.warn("重點內容太多，建議拆頁或縮短句子")
        tf = self.textbox(slide, x, y, w, h)
        first = True
        for b in items:
            text, subs = (b, []) if isinstance(b, str) else (b["text"], b.get("sub", []))
            p = self.para(tf, text, size, spacing=1.25, space_after=size * 0.55, first=first)
            self._bullet(p, "●", "accent", Inches(0.4), size * 0.6)
            first = False
            for sub in subs:
                sp = self.para(tf, sub, size - 4, "secondary", spacing=1.25,
                               space_after=size * 0.45)
                sp.level = 1
                self._bullet(sp, "–", "secondary", Inches(0.3), size - 4, indent_left=Inches(0.7))

    def _bullet(self, p, char, color, hang, size, indent_left=None):
        ppr = p._p.get_or_add_pPr()
        left = indent_left if indent_left is not None else hang
        ppr.set("marL", str(int(left)))
        ppr.set("indent", str(-int(hang)))
        for tag in ("a:buClr", "a:buSzPts", "a:buFont", "a:buChar"):
            old = ppr.find(qn(tag))
            if old is not None:
                ppr.remove(old)
        clr = ppr.makeelement(qn("a:buClr"), {})
        srgb = clr.makeelement(qn("a:srgbClr"), {"val": self.t[color]})
        clr.append(srgb)
        sz = ppr.makeelement(qn("a:buSzPts"), {"val": str(int(size * 100))})
        font = ppr.makeelement(qn("a:buFont"), {"typeface": "Arial"})
        bu = ppr.makeelement(qn("a:buChar"), {"char": char})
        for el in (clr, sz, font, bu):
            ppr.append(el)

    def rect(self, slide, x, y, w, h, fill="surface", radius=0.06):
        shp = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h)
        shp.adjustments[0] = radius
        shp.fill.solid()
        shp.fill.fore_color.rgb = self.color(fill)
        shp.line.fill.background()
        shp.shadow.inherit = False
        return shp

    def line(self, slide, x, y, w, color="hairline"):
        ln = slide.shapes.add_connector(1, x, y, x + w, y)
        ln.line.color.rgb = self.color(color)
        ln.line.width = Pt(0.75)

    def picture(self, slide, path, x, y, w, h):
        from PIL import Image
        with Image.open(path) as im:
            iw, ih = im.size
        scale = min(w / iw, h / ih)
        pw, ph = int(iw * scale), int(ih * scale)
        slide.shapes.add_picture(path, x + (w - pw) // 2, y + (h - ph) // 2, pw, ph)

    def kicker(self, slide, s, y=TITLE_TOP):
        """標題上方的小字分類標籤，Apple 發表會常用的 eyebrow。"""
        if s.get("kicker"):
            self.text(slide, MARGIN_X, y, CONTENT_W, Inches(0.4), s["kicker"], 16,
                      "accent", bold=True)
            return y + Inches(0.5)
        return y

    def title(self, slide, s, y=None):
        y = self.kicker(slide, s, TITLE_TOP if y is None else y)
        h = Inches(1.1)
        self.text(slide, MARGIN_X, y, CONTENT_W, h, s["title"], [40, 36, 32, 28],
                  bold=True, spacing=1.05, anchor=MSO_ANCHOR.TOP, label="標題")
        return y + h + Inches(0.35)

    def footer(self, slide):
        tf = self.textbox(slide, SLIDE_W - MARGIN_X - Inches(1), SLIDE_H - Inches(0.55),
                          Inches(1), Inches(0.3))
        self.para(tf, str(self.page), 11, "secondary", align=PP_ALIGN.RIGHT, first=True)

    def warn(self, msg):
        self.warnings.append(f"第 {self.page} 頁：{msg}")

    # ------------------------------------------------------------ 版型

    def build(self):
        for s in self.o["slides"]:
            getattr(self, "layout_" + s["layout"].replace("-", "_"))(s)
        return self.prs

    def layout_title(self, s):
        slide = self.new_slide(s)
        y = Inches(2.35)
        if s.get("kicker"):
            self.text(slide, MARGIN_X, y - Inches(0.6), CONTENT_W, Inches(0.45),
                      s["kicker"], 18, "accent", bold=True)
        self.text(slide, MARGIN_X, y, CONTENT_W, Inches(2.0), s["title"],
                  [66, 60, 54, 48, 40], bold=True, spacing=1.02,
                  anchor=MSO_ANCHOR.BOTTOM, label="標題")
        if s.get("subtitle"):
            self.text(slide, MARGIN_X, y + Inches(2.2), CONTENT_W, Inches(1.0),
                      s["subtitle"], [26, 24, 22], "secondary", spacing=1.25)
        if s.get("byline"):
            self.text(slide, MARGIN_X, SLIDE_H - Inches(1.2), CONTENT_W, Inches(0.4),
                      s["byline"], 16, "secondary")

    def layout_section(self, s):
        slide = self.new_slide(s)
        if s.get("number"):
            self.text(slide, MARGIN_X, Inches(1.7), CONTENT_W, Inches(1.4),
                      s["number"], 72, "accent", bold=True)
        self.text(slide, MARGIN_X, Inches(3.1), CONTENT_W, Inches(1.6), s["title"],
                  [54, 48, 42, 36], bold=True, spacing=1.05, label="章節標題")
        if s.get("subtitle"):
            self.text(slide, MARGIN_X, Inches(4.8), CONTENT_W, Inches(1.0),
                      s["subtitle"], [24, 22, 20], "secondary", spacing=1.25)

    def layout_bullets(self, s):
        slide = self.new_slide(s)
        y = self.title(slide, s)
        self.bullets(slide, MARGIN_X, y, CONTENT_W * 0.85, SLIDE_H - y - Inches(0.9), s["bullets"])
        self.footer(slide)

    def layout_statement(self, s):
        slide = self.new_slide(s)
        y = self.kicker(slide, s, Inches(1.6))
        self.text(slide, MARGIN_X, y, CONTENT_W * 0.92, Inches(3.2), s["text"],
                  [60, 54, 48, 42, 36, 32], bold=True, spacing=1.08,
                  anchor=MSO_ANCHOR.MIDDLE, label="主張句")
        if s.get("caption"):
            self.text(slide, MARGIN_X, y + Inches(3.5), CONTENT_W * 0.8, Inches(1.0),
                      s["caption"], [22, 20], "secondary", spacing=1.3)
        self.footer(slide)

    def layout_two_column(self, s):
        slide = self.new_slide(s)
        y = self.title(slide, s)
        gap = Inches(0.6)
        col_w = (CONTENT_W - gap) // 2
        h = SLIDE_H - y - Inches(0.9)
        for i, side in enumerate(("left", "right")):
            col = s[side]
            x = MARGIN_X + i * (col_w + gap)
            card = self.rect(slide, x, y, col_w, h)
            pad = Inches(0.45)
            inner_y = y + pad
            if col.get("heading"):
                self.text(slide, x + pad, inner_y, col_w - 2 * pad, Inches(0.6),
                          col["heading"], [26, 24, 22], bold=True, label="欄位標題")
                inner_y += Inches(0.85)
            self.bullets(slide, x + pad, inner_y, col_w - 2 * pad,
                         y + h - inner_y - pad, col.get("bullets", []), sizes=(24, 22, 20, 18))
        self.footer(slide)

    def layout_stats(self, s):
        slide = self.new_slide(s)
        y = self.title(slide, s) if s.get("title") else Inches(1.6)
        stats = s["stats"]
        gap = Inches(0.35)
        n = len(stats)
        card_w = (CONTENT_W - gap * (n - 1)) // n
        card_h = min(Inches(3.6), SLIDE_H - y - Inches(1.0))
        value_sizes = {1: [120, 96], 2: [96, 80, 72], 3: [80, 72, 60], 4: [64, 56, 48]}[n]
        for i, st in enumerate(stats):
            x = MARGIN_X + i * (card_w + gap)
            self.rect(slide, x, y, card_w, card_h)
            pad = Inches(0.4)
            self.text(slide, x + pad, y + pad, card_w - 2 * pad, card_h * 0.5,
                      st["value"], value_sizes, "accent", bold=True, spacing=1.0,
                      anchor=MSO_ANCHOR.BOTTOM, label="數字")
            if st.get("label"):
                self.text(slide, x + pad, y + pad + card_h * 0.55, card_w - 2 * pad,
                          card_h * 0.45 - 2 * pad, st["label"], [20, 18, 16, 14],
                          "secondary", spacing=1.25)
        self.footer(slide)

    def layout_steps(self, s):
        slide = self.new_slide(s)
        y = self.title(slide, s) + Inches(0.2)
        steps = s["steps"]
        n = len(steps)
        gap = Inches(0.4)
        w = (CONTENT_W - gap * (n - 1)) // n
        for i, st in enumerate(steps):
            x = MARGIN_X + i * (w + gap)
            self.text(slide, x, y, w, Inches(0.8), f"{i + 1:02d}", 40, "accent", bold=True)
            self.line(slide, x, y + Inches(0.95), w)
            self.text(slide, x, y + Inches(1.2), w, Inches(0.9), st["title"],
                      [26, 24, 22, 20], bold=True, spacing=1.15, label="步驟標題")
            if st.get("text"):
                self.text(slide, x, y + Inches(2.15), w, SLIDE_H - y - Inches(3.1),
                          st["text"], [18, 17, 16, 14], "secondary", spacing=1.35,
                          label="步驟說明")
        self.footer(slide)

    def layout_quote(self, s):
        slide = self.new_slide(s)
        self.text(slide, MARGIN_X, Inches(1.4), CONTENT_W * 0.9, Inches(3.6),
                  f"「{s['quote']}」", [48, 44, 40, 36, 32, 28], bold=True,
                  spacing=1.15, anchor=MSO_ANCHOR.BOTTOM, label="引言")
        if s.get("author"):
            self.text(slide, MARGIN_X, Inches(5.3), CONTENT_W, Inches(0.6),
                      f"— {s['author']}", 22, "secondary")
        self.footer(slide)

    def layout_image(self, s):
        slide = self.new_slide(s)
        y = self.title(slide, s) if s.get("title") else Inches(0.6)
        cap_h = Inches(0.6) if s.get("caption") else 0
        h = SLIDE_H - y - Inches(0.8) - cap_h
        self.picture(slide, _resolve(s["image"], self.base_dir), MARGIN_X, y, CONTENT_W, h)
        if cap_h:
            self.text(slide, MARGIN_X, y + h + Inches(0.15), CONTENT_W, Inches(0.45),
                      s["caption"], [16, 14], "secondary", align=PP_ALIGN.CENTER)
        self.footer(slide)

    def layout_image_text(self, s):
        slide = self.new_slide(s)
        y = self.title(slide, s)
        h = SLIDE_H - y - Inches(0.9)
        text_w = CONTENT_W * 0.46
        self.bullets(slide, MARGIN_X, y, text_w, h, s["bullets"], sizes=(26, 24, 22, 20, 18))
        img_x = MARGIN_X + text_w + Inches(0.5)
        self.picture(slide, _resolve(s["image"], self.base_dir), img_x, y,
                     MARGIN_X + CONTENT_W - img_x, h)
        self.footer(slide)

    def layout_table(self, s):
        slide = self.new_slide(s)
        y = self.title(slide, s)
        cols, rows = s["columns"], s["rows"]
        n_rows = len(rows) + 1
        avail_h = SLIDE_H - y - Inches(0.9)
        row_h = min(Inches(0.7), avail_h // n_rows)
        size = 18 if row_h >= Inches(0.6) else 15 if row_h >= Inches(0.48) else 13
        shape = slide.shapes.add_table(n_rows, len(cols), MARGIN_X, y, CONTENT_W, row_h * n_rows)
        table = shape.table
        tbl_pr = shape._element.graphic.graphicData.tbl.tblPr
        style = tbl_pr.find(qn("a:tableStyleId"))
        if style is None:
            style = tbl_pr.makeelement(qn("a:tableStyleId"), {})
            tbl_pr.append(style)
        style.text = "{2D5ABB26-0587-4C30-8999-92F81FD0307C}"  # No Style, No Grid
        tbl_pr.set("firstRow", "0")
        tbl_pr.set("bandRow", "0")
        for r in range(n_rows):
            table.rows[r].height = row_h
            values = cols if r == 0 else rows[r - 1]
            for c, val in enumerate(values):
                cell = table.cell(r, c)
                cell.fill.background()
                cell.margin_left = Inches(0.1) if c else 0
                cell.margin_right = Inches(0.1)
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                tf = cell.text_frame
                tf.word_wrap = True
                tf.paragraphs[0].text = ""
                run = tf.paragraphs[0].add_run()
                run.text = str(val)
                self.style_run(run, size if r else size - 2,
                               "secondary" if r == 0 else "text", bold=(r == 0))
                self._cell_bottom_border(cell, "text" if r == 0 else "hairline")
        self.footer(slide)

    def _cell_bottom_border(self, cell, color):
        # tcPr 的子元素有固定順序：框線必須排在 fill 前面，PowerPoint 才不會報檔案損毀
        tc_pr = cell._tc.get_or_add_tcPr()
        for i, tag in enumerate(("a:lnL", "a:lnR", "a:lnT", "a:lnB")):
            ln = tc_pr.makeelement(qn(tag), {"w": "9525" if tag == "a:lnB" else "0"})
            if tag == "a:lnB":
                fill = ln.makeelement(qn("a:solidFill"), {})
                fill.append(fill.makeelement(qn("a:srgbClr"), {"val": self.t[color]}))
            else:
                fill = ln.makeelement(qn("a:noFill"), {})
            ln.append(fill)
            tc_pr.insert(i, ln)

    def layout_end(self, s):
        slide = self.new_slide(s)
        self.text(slide, MARGIN_X, Inches(2.4), CONTENT_W, Inches(1.6), s["title"],
                  [66, 60, 54, 48], bold=True, align=PP_ALIGN.CENTER,
                  anchor=MSO_ANCHOR.BOTTOM, label="結尾標題")
        if s.get("subtitle"):
            self.text(slide, MARGIN_X, Inches(4.2), CONTENT_W, Inches(1.0),
                      s["subtitle"], [24, 22, 20], "secondary", align=PP_ALIGN.CENTER,
                      spacing=1.3)


# ---------------------------------------------------------------- PDF

def find_soffice():
    for name in ("soffice", "libreoffice"):
        path = shutil.which(name)
        if path:
            return path
    mac = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    return mac if os.path.exists(mac) else None


def to_pdf(pptx_path, font):
    soffice = find_soffice()
    if not soffice:
        raise RuntimeError("找不到 LibreOffice（soffice），無法轉 PDF；.pptx 已產出")
    out_dir = os.path.dirname(os.path.abspath(pptx_path))
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, HOME=tmp)
        if sys.platform.startswith("linux"):
            conf = os.path.join(tmp, "fonts.conf")
            with open(conf, "w") as f:
                f.write(
                    '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig>'
                    '<include ignore_missing="yes">/etc/fonts/fonts.conf</include>'
                    f'<alias binding="same"><family>{font}</family>'
                    f'<prefer><family>{LINUX_FALLBACK_FONT}</family></prefer></alias>'
                    "</fontconfig>"
                )
            env["FONTCONFIG_FILE"] = conf
        cmd = [soffice, f"-env:UserInstallation=file://{tmp}/profile", "--headless",
               "--convert-to", "pdf", "--outdir", out_dir, pptx_path]
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=180)
    pdf = os.path.splitext(os.path.abspath(pptx_path))[0] + ".pdf"
    if proc.returncode != 0 or not os.path.exists(pdf):
        raise RuntimeError(f"PDF 轉檔失敗：{proc.stderr.strip() or proc.stdout.strip()}")
    return pdf


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("outline", help="大綱 JSON 檔")
    ap.add_argument("--out", required=True, help="輸出的 .pptx 路徑")
    ap.add_argument("--pdf", action="store_true", help="同時轉出 PDF（需要 LibreOffice）")
    ap.add_argument("--font", default=DEFAULT_FONT, help=f"字型，預設 {DEFAULT_FONT}")
    args = ap.parse_args()

    try:
        with open(args.outline, encoding="utf-8") as f:
            outline = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"❌ 讀不到大綱：{e}")
        return 2

    base_dir = os.path.dirname(os.path.abspath(args.outline))
    errors, warnings = validate(outline, base_dir)
    if errors:
        print("❌ 大綱有錯，請修正後再跑：")
        for e in errors:
            print(f"  - {e}")
        return 2

    deck = Deck(outline, args.font, base_dir)
    prs = deck.build()
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    prs.save(args.out)
    print(f"✅ PPTX：{os.path.abspath(args.out)}（{len(outline['slides'])} 頁）")

    status = 0
    if args.pdf:
        try:
            print(f"✅ PDF：{to_pdf(args.out, args.font)}")
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            print(f"⚠️ {e}")
            status = 3

    for w in warnings + deck.warnings:
        print(f"⚠️ {w}")
    return status


if __name__ == "__main__":
    sys.exit(main())
