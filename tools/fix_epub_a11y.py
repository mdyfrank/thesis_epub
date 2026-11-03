#!/usr/bin/env python3
r"""Post-process LaTeXML output to clear the ACE (DAISY) accessibility failures
that cannot be expressed in the LaTeX source.

LaTeXML gives us no hook for these, so they have to be patched in the generated
XHTML / OPF:

  * html-has-lang / epub-lang        -> lang + xml:lang on <html> and <package>
  * metadata-accessmode              -> schema:accessMode
  * metadata-accessmodesufficient    -> schema:accessModeSufficient
  * metadata-accessibilityfeature    -> schema:accessibilityFeature
  * metadata-accessibilityhazard     -> schema:accessibilityHazard
  * metadata-accessibilitysummary    -> schema:accessibilitySummary
  * epub-type-has-matching-role      -> role="doc-toc" on the TOC nav; the
                                        List of Figures / List of Tables navs
                                        get epub:type="loi" / "lot" (LaTeXML
                                        wrongly labels all three as "toc")
  * landmark-unique                  -> distinct aria-label on each <nav>
  * heading-order                    -> the "Contents"/"List of ..." headings
                                        are emitted as <h6> after an <h2>
  * epub-toc-order                   -> two separate causes: LaTeXML omits
                                        unnumbered (\chapter*) chapters and the
                                        bibliography from its TOC, and ACE
                                        cannot resolve same-document fragment
                                        links ("#Ch1"). The missing entries are
                                        re-inserted in document order and the
                                        TOC links are qualified with the file
                                        name.

It also repairs two layout faults that are not ACE checks. LaTeXML renders
\resizebox as a fixed-size box with the table absolutely positioned and scaled
inside it, so in a reading system a scaled table overflows upward over the text
before it (see unscale_tables). It draws a tcolorbox as a fixed-size SVG whose
content overflows onto the text after it (see unbox_svg_pictures). Content wider
than a narrow or paginated page spills onto neighbouring text, so wide blocks
get horizontal scrollers and LaTeXML.css gains a few layout rules (see
wrap_wide_blocks and LAYOUT_CSS). These fixes are applied to the EPUB only.

Usage:  python3 tools/fix_epub_a11y.py docs/thesis.epub [docs/index.html ...]
"""

import bisect
import os
import re
import shutil
import sys
import tempfile
import zipfile

LANG = "en"

# NOTE: keep this honest. "alternativeText" must NOT be claimed until every
# \includegraphics has an alt=... description, otherwise the metadata lies to
# the reader. Likewise accessModeSufficient stays "textual,visual" while the
# figures still carry no text alternative.
ACCESS_METADATA = """    <meta property="schema:accessMode">textual</meta>
    <meta property="schema:accessMode">visual</meta>
    <meta property="schema:accessModeSufficient">textual,visual</meta>
    <meta property="schema:accessibilityFeature">structuralNavigation</meta>
    <meta property="schema:accessibilityFeature">tableOfContents</meta>
    <meta property="schema:accessibilityFeature">MathML</meta>
    <meta property="schema:accessibilityHazard">none</meta>
    <meta property="schema:accessibilitySummary">This publication conforms to \
WCAG 2.1 Level AA except that figures are supplied as images without text \
alternatives. It has a full table of contents, a list of figures and a list of \
tables, semantic heading structure, real (non-image) tables with header cells, \
and mathematics marked up as MathML.</meta>
"""

NAVS = [
    ("ltx_list_toc", "toc", "Table of Contents", ' role="doc-toc"'),
    ("ltx_list_lof", "loi", "List of Figures", ""),
    ("ltx_list_lot", "lot", "List of Tables", ""),
]


SECTION_RE = re.compile(
    r'<section[^>]*\bid="([^"]+)"[^>]*\bclass="(ltx_(?:chapter|appendix|bibliography)\b[^"]*)"'
)
TOC_ENTRY = (
    '<li class="ltx_tocentry ltx_tocentry_chapter">'
    '<a href="#%s" class="ltx_ref"><span class="ltx_text ltx_ref_title">%s</span></a></li>\n'
)


def _document_order(text):
    """Top-level (chapter/appendix/bibliography) ids in reading order."""
    order = []
    for m in SECTION_RE.finditer(text):
        sid = m.group(1)
        tail = text[m.end():m.end() + 800]
        title = re.search(r"<h[1-6][^>]*>(.*?)</h[1-6]>", tail, re.S)
        title = re.sub(r"<[^>]+>", "", title.group(1)).strip() if title else sid
        title = re.sub(r"\s+", " ", title)
        order.append((sid, title))
    return order


def fix_toc_order(text):
    """Re-insert the entries LaTeXML dropped, keeping document order."""
    start = text.find("ltx_list_toc")
    if start < 0:
        return text
    ol_start = text.find('<ol class="ltx_toclist">', start)
    ol_end = text.find("</nav>", start)
    if ol_start < 0 or ol_end < 0:
        return text
    ol_start += len('<ol class="ltx_toclist">')
    body = text[ol_start:ol_end]

    # The region up to </nav> ends with the list's own </ol>. Split it off so
    # that appended entries land *inside* the <ol>; otherwise they become <li>
    # elements with no list parent (ACE "listitem" violation). Anything that
    # already leaked out past </ol> is pulled back in, so re-running this on an
    # already-patched file repairs it instead of duplicating entries.
    close = body.rfind("</ol>")
    if close < 0:
        return text
    escaped = body[close + len("</ol>"):]
    body = body[:close] + escaped
    closing = "</ol>"

    # hrefs may already be document-qualified ("thesis.xhtml#Ch1") from a
    # previous run, so match an optional file part before the fragment.
    entry_re = r'<li class="ltx_tocentry[^"]*"><a href="[^"#]*#([^"]+)"'
    present = set(re.findall(entry_re, body))
    # Split the existing list into top-level <li> blocks keyed by their target.
    pieces = re.split(r'(?=<li class="ltx_tocentry ltx_tocentry_(?:chapter|appendix)")', body)
    lead = pieces[0] if pieces and not pieces[0].lstrip().startswith("<li") else ""
    blocks = []
    for p in pieces[1:] if lead else pieces:
        m = re.match(entry_re, p.lstrip())
        if m:
            blocks.append((m.group(1), p))

    by_id = dict(blocks)
    rebuilt = []
    added = []
    for sid, title in _document_order(text):
        if sid in by_id:
            rebuilt.append(by_id[sid])
        elif sid not in present:
            rebuilt.append(TOC_ENTRY % (sid, title))
            added.append(title)
    if not added and not escaped.strip():
        return text
    if added:
        print("  toc-order: added %s" % ", ".join(added))
    if escaped.strip():
        print("  toc-order: moved %d stray entry/entries back inside <ol>"
              % len(re.findall(r"<li\b", escaped)))
    return text[:ol_start] + lead + "".join(rebuilt) + closing + text[ol_end:]


def qualify_toc_hrefs(text, self_name):
    """Turn same-document TOC fragments into document-qualified references.

    ACE resolves a TOC link as path.join(dirname(navDoc), hrefBeforeHash), so a
    bare "#Ch1" collapses to the *directory* and never matches the harvested
    "<dir>/thesis.xhtml#Ch1". Every entry then reports as an unresolvable id and
    epub-toc-order fails on the first one. Writing the file name into the href
    is equivalent for a reading system and lets the check resolve.
    """
    if not self_name:
        return text
    start = text.find("ltx_list_toc")
    if start < 0:
        return text
    end = text.find("</nav>", start)
    if end < 0:
        return text
    head, nav, tail = text[:start], text[start:end], text[end:]
    nav, n = re.subn(r'(<a\s[^>]*href=")#', r"\1" + self_name + "#", nav)
    if n:
        print("  toc-href: qualified %d links with %s" % (n, self_name))
    return head + nav + tail


# LaTeXML renders \resizebox as an outer box with a fixed height in pt and the
# content inside it position:absolute from its bottom edge, scaled by a
# transform. The box is sized for LaTeXML's own row height, so with a reading
# system's font a scaled table grows upward over the text before it. Each such
# wrapper is returned to normal flow at the table's natural size, and scrolls
# sideways when the screen is narrower than the table. Only plain translate and
# scale wrappers inside table floats are matched, so a rotation or a scaled
# figure keeps LaTeXML's rendering.
TABLE_FLOAT = re.compile(r'<figure id="[^"]*" class="ltx_table\b[^"]*">.*?</figure>', re.S)
SCALED_WRAPPER = re.compile(
    r'(<div class="[^"]*\bltx_transformed_outer" style=")[^"]*'
    r'("><span class="ltx_transformed_inner" style=")'
    r'transform:translate\([^)]*\) scale\([^)]*\) ;'
)
FLOW_OUTER = "display:block;max-width:100%;overflow-x:auto;"
FLOW_INNER = "position:static;"


def unscale_tables(text):
    """Return every scaled table wrapper to normal document flow."""
    count = 0

    def fix_float(m):
        nonlocal count
        new, n = SCALED_WRAPPER.subn(
            r"\g<1>" + FLOW_OUTER + r"\g<2>" + FLOW_INNER, m.group(0))
        count += n
        return new

    text = TABLE_FLOAT.sub(fix_float, text)
    if count:
        print("  unscale: returned %d scaled tables to document flow" % count)
    return text


# LaTeXML draws a tcolorbox (the thesis's promptbox) as an inline SVG of fixed
# width and height: filled background paths plus one foreignObject that holds
# the content with overflow visible. With a reading system's font the content
# outgrows that box and runs over the captions and text that follow. Each such
# box becomes an ordinary block with the same background colour, which grows
# with its content. Only pictures made of unstroked background paths and a
# single foreignObject are matched, so a real drawing keeps LaTeXML's SVG.
SVG_BOX = re.compile(
    r'<span class="ltx_inline-block">\s*(<svg\b[^>]*\bclass="ltx_picture"[^>]*>.*?</svg>)\s*</span>', re.S)
FO_CONTENT = re.compile(
    r'<span[^>]*\bclass="ltx_foreignobject_content">(.*)</span>\s*</span>\s*</foreignObject>', re.S)
BOX_STYLE = ("display:block;background-color:%s;border-radius:0.3em;"
             "padding:0.4em 0.6em;margin:0.5em 0;overflow-x:auto;")


def unbox_svg_pictures(text):
    """Replace fixed-size SVG content boxes with blocks in normal flow."""
    count = 0

    def fix(m):
        nonlocal count
        svg = m.group(1)
        paths = re.findall(r"<path\b[^>]*>", svg)
        fills = set(re.findall(r'fill="(#[0-9A-Fa-f]{6})"', svg)) - {"#000000"}
        content = FO_CONTENT.search(svg)
        if (svg.count("<foreignObject") != 1 or len(fills) != 1 or not content
                or not all("stroke:none" in p for p in paths)):
            return m.group(0)
        inner = re.sub(r'<table class="(ltx_tabular[^"]*)">',
                       r'<table class="\1" style="width:100%">', content.group(1), count=1)
        # a div is only valid where the box sits directly in a block container
        opened = text.rfind("<", 0, m.start())
        tag = "div" if text.startswith("<div", opened) else "span"
        # keep the picture's id so no link target disappears
        pid = re.search(r'\bid="([^"]+)"', svg[:svg.find(">")])
        idattr = ' id="%s"' % pid.group(1) if pid else ""
        count += 1
        return '<%s%s class="ltx_promptbox" style="%s">%s</%s>' % (
            tag, idattr, BOX_STYLE % fills.pop(), inner, tag)

    text = SVG_BOX.sub(fix, text)
    if count:
        print("  unbox: returned %d fixed-size SVG boxes to document flow" % count)
    return text


# Content wider than the text column (a long display equation, a wide table)
# runs past the column edge. Paginated reading systems lay pages out as CSS
# columns, so that overflow lands on the text of the neighbouring page. Display
# equations and any table not already inside a scrolling wrapper get one, so
# they scroll sideways within the column instead.
SCROLL_STYLE = "display:block;max-width:100%;overflow-x:auto;"
# Formula ink can reach a few px past the equation table; padding (offset by
# equal negative margins, so the layout does not move) keeps it inside the
# scroller, which then needs no vertical scrollbar.
EQN_SCROLL_STYLE = SCROLL_STYLE + "overflow-y:hidden;padding:0.35em 0;margin:-0.35em 0;"
EQN_TABLE = re.compile(r'<table\b[^>]*\bclass="[^"]*\bltx_eqn_table\b[^"]*"[^>]*>.*?</table>', re.S)
TABULAR_OPEN = re.compile(r'<table\b[^>]*\bclass="ltx_tabular\b')


def wrap_wide_blocks(text):
    """Give display equations and unwrapped tables a horizontal scroller."""
    n_eqn = 0

    def eqn(m):
        nonlocal n_eqn
        if text[:m.start()].endswith('<div class="ltx_eqn_scroll" style="%s">' % EQN_SCROLL_STYLE):
            return m.group(0)
        n_eqn += 1
        return '<div class="ltx_eqn_scroll" style="%s">%s</div>' % (EQN_SCROLL_STYLE, m.group(0))

    text = EQN_TABLE.sub(eqn, text)

    out, pos, n_tab = [], 0, 0
    for m in TABULAR_OPEN.finditer(text):
        if m.start() < pos:
            continue                                   # inside a table already copied
        if text.count("<table", 0, m.start()) != text.count("</table>", 0, m.start()):
            continue                                   # nested in another table
        before = text[max(0, m.start() - 300):m.start()]
        if re.search(r'ltx_transformed_inner" style="[^"]*">\s*$|ltx_promptbox"[^>]*>\s*$'
                     r'|class="ltx_table_scroll"[^>]*>\s*$', before):
            continue                                   # already scrolls (or is a prompt box)
        depth, i = 0, m.start()
        while True:                                    # find the matching </table>
            o, c = text.find("<table", i), text.find("</table>", i)
            if o != -1 and o < c:
                depth, i = depth + 1, o + 6
            else:
                depth, i = depth - 1, c + 8
                if depth == 0:
                    break
        out.append(text[pos:m.start()])
        out.append('<div class="ltx_table_scroll" style="%s">%s</div>' % (SCROLL_STYLE, text[m.start():i]))
        pos = i
        n_tab += 1
    text = "".join(out) + text[pos:]
    if n_eqn or n_tab:
        print("  scroll: wrapped %d display equations and %d tables" % (n_eqn, n_tab))
    return text


# Inline MathML cannot line-break, so a long formula in running text overflows
# a phone-width page. Formulas of 15 or more tokens get an inline scroller
# (styled in LAYOUT_CSS to keep the text baseline). Formulas inside tables are
# skipped: their tables already scroll.
INLINE_MATH = re.compile(r'<math\b[^>]*\bdisplay="inline"[^>]*>.*?</math>', re.S)
MATH_TOKEN = re.compile(r"<m(?:i|n|o|text|s)\b")
LONG_MATH_TOKENS = 15


def wrap_long_inline_math(text):
    """Let long inline formulas scroll instead of running off the page."""
    opens = [m.start() for m in re.finditer(r"<table\b", text)]
    closes = [m.start() for m in re.finditer(r"</table>", text)]
    count = 0

    def fix(m):
        nonlocal count
        depth = bisect.bisect(opens, m.start()) - bisect.bisect(closes, m.start())
        if depth or len(MATH_TOKEN.findall(m.group(0))) < LONG_MATH_TOKENS:
            return m.group(0)
        if text[:m.start()].endswith('<span class="ltx_math_scroll">'):
            return m.group(0)
        count += 1
        return '<span class="ltx_math_scroll">%s</span>' % m.group(0)

    text = INLINE_MATH.sub(fix, text)
    if count:
        print("  scroll: wrapped %d long inline formulas" % count)
    return text


# A list label wider than a bullet ("2021-2026", "RQ1.", "(4)") sits in the
# 1.5em LaTeXML reserves, and the item text, an inline-block as wide as the
# whole item, is pushed past the right edge. Such items are marked so that
# LAYOUT_CSS lays label and text side by side with the text taking only the
# remaining width. Bullet items, and items holding more than one block, are
# left as they are.
LIST_ITEM = re.compile(r'<li\b[^>]*\bclass="ltx_item"[^>]*>')


def mark_wide_labels(text):
    """Mark list items whose label is wider than a bullet."""
    out, pos, count = [], 0, 0
    for m in LIST_ITEM.finditer(text):
        depth, i = 1, m.end()
        while depth:                                   # the matching </li>
            o, c = text.find("<li", i), text.find("</li>", i)
            if o != -1 and o < c:
                depth, i = depth + 1, o + 3
            else:
                depth, i = depth - 1, c + 5
        body = text[m.end():i - 5]
        tag = re.match(r'\s*<span class="ltx_tag ltx_tag_item">(.*?)</span>\s*(<div\b[^>]*\bclass="ltx_para\b)', body, re.S)
        if not tag:
            continue
        label = re.sub(r"<[^>]+>", "", tag.group(1)).strip()
        if label in ("", "•"):
            continue
        d, j = 0, tag.start(2)                         # the para must be the last child
        while True:
            o, c = body.find("<div", j), body.find("</div>", j)
            if o != -1 and o < c:
                d, j = d + 1, o + 4
            else:
                d, j = d - 1, c + 6
                if d == 0:
                    break
        if body[j:].strip() or "ltx_item_widelabel" in m.group(0):
            continue
        out.append(text[pos:m.start()])
        out.append(m.group(0).replace('class="ltx_item"', 'class="ltx_item ltx_item_widelabel"', 1))
        pos = m.end()
        count += 1
    text = "".join(out) + text[pos:]
    if count:
        print("  lists: %d items with wide labels laid out side by side" % count)
    return text


# Appended to the EPUB's LaTeXML.css. Images are limited to the column (with
# height:auto so they keep their aspect ratio; LaTeXML caps sub-figure images
# at a fixed width without it, which distorts them and makes three panels
# wider than a phone page). Equation numbers get their own width instead of a
# zero-width cell that lets them sit on top of the formula. Words and URLs too
# long for the column may break rather than run onto the next page.
LAYOUT_CSS_MARK = "/* fix_epub_a11y: keep wide content inside the page */"
LAYOUT_CSS = LAYOUT_CSS_MARK + """
body { overflow-wrap: break-word; }
img.ltx_graphics, .ltx_flex_figure img.ltx_graphics { max-width: 100%; height: auto; }
.ltx_eqn_eqno { max-width: none; }
.ltx_math_scroll { display: inline-block; max-width: 100%; overflow-x: auto; overflow-y: hidden;
  padding: 0.3em 0; margin: -0.3em 0; vertical-align: middle; }
@supports (baseline-source: first) {
  .ltx_math_scroll { vertical-align: baseline; baseline-source: first; }
}
.ltx_itemize .ltx_item.ltx_item_widelabel, .ltx_enumerate .ltx_item.ltx_item_widelabel {
  display: flex; align-items: baseline; column-gap: 0.33em; white-space: normal; }
.ltx_item.ltx_item_widelabel > .ltx_tag { flex: none; }
.ltx_item.ltx_item_widelabel > .ltx_tag + .ltx_para { display: block; flex: 1 1 auto; min-width: 0; }
"""


def patch_css(text):
    if LAYOUT_CSS_MARK in text:
        return text
    print("  css: appended layout rules")
    return text.rstrip("\n") + "\n\n" + LAYOUT_CSS


def patch_xhtml(text, self_name=None):
    """Fix language, nav semantics and heading order in a LaTeXML XHTML file."""
    text = fix_toc_order(text)
    text = qualify_toc_hrefs(text, self_name)

    # --- <html lang="en" xml:lang="en"> ------------------------------------
    def add_lang(m):
        attrs = m.group(1)
        if "xml:lang=" in attrs or re.search(r"\blang=", attrs):
            return m.group(0)
        return "<html%s lang=\"%s\" xml:lang=\"%s\">" % (attrs, LANG, LANG)

    text = re.sub(r"<html([^>]*)>", add_lang, text, count=1)

    # --- <nav> semantics ---------------------------------------------------
    for cls, epubtype, label, role in NAVS:
        pattern = re.compile(r"<nav([^>]*\b" + cls + r"\b[^>]*)>")

        def fix_nav(m, epubtype=epubtype, label=label, role=role):
            attrs = m.group(1)
            # LaTeXML stamps epub:type="toc" on all three lists.
            attrs = re.sub(r'\sepub:type="[^"]*"', "", attrs)
            attrs += ' epub:type="%s"' % epubtype
            if role and "role=" not in attrs:
                attrs += role
            if "aria-label=" not in attrs:
                attrs += ' aria-label="%s"' % label
            return "<nav%s>" % attrs

        text = pattern.sub(fix_nav, text)

    # --- heading order: the list titles come out as <h6> after an <h2> -----
    text = re.sub(
        r'<h6(\s[^>]*class="[^"]*ltx_title_contents[^"]*"[^>]*)>(.*?)</h6>',
        r"<h2\1>\2</h2>",
        text,
        flags=re.S,
    )
    return text


def patch_opf(text):
    """Add xml:lang and the schema.org accessibility metadata to content.opf."""

    def add_lang(m):
        attrs = m.group(1)
        if "xml:lang=" in attrs:
            return m.group(0)
        return "<package%s xml:lang=\"%s\">" % (attrs, LANG)

    text = re.sub(r"<package([^>]*)>", add_lang, text, count=1)

    if "schema:accessMode" not in text:
        text = text.replace("</metadata>", ACCESS_METADATA + "  </metadata>", 1)
    return text


def patch_epub(path):
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".epub")
    os.close(tmp_fd)
    changed = []
    with zipfile.ZipFile(path, "r") as zin:
        names = zin.namelist()
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zout:
            # The EPUB OCF spec requires "mimetype" first and uncompressed.
            if "mimetype" in names:
                zout.writestr(
                    zipfile.ZipInfo("mimetype"),
                    zin.read("mimetype"),
                    compress_type=zipfile.ZIP_STORED,
                )
            for info in zin.infolist():
                if info.filename == "mimetype":
                    continue
                data = zin.read(info.filename)
                low = info.filename.lower()
                if low.endswith(".xhtml") or low.endswith(".html"):
                    new = patch_xhtml(
                        data.decode("utf-8"),
                        self_name=os.path.basename(info.filename),
                    )
                    # EPUB only: docs/index.html is left as LaTeXML wrote it.
                    for step in (unscale_tables, unbox_svg_pictures, wrap_wide_blocks,
                                 wrap_long_inline_math, mark_wide_labels):
                        new = step(new)
                    new = new.encode("utf-8")
                    if new != data:
                        changed.append(info.filename)
                    data = new
                elif os.path.basename(low) == "latexml.css":
                    new = patch_css(data.decode("utf-8")).encode("utf-8")
                    if new != data:
                        changed.append(info.filename)
                    data = new
                elif low.endswith(".opf"):
                    new = patch_opf(data.decode("utf-8")).encode("utf-8")
                    if new != data:
                        changed.append(info.filename)
                    data = new
                zout.writestr(info, data)
    shutil.move(tmp_path, path)
    print("patched %s: %s" % (path, ", ".join(changed) if changed else "no change"))


def patch_html_file(path):
    with open(path, "r", encoding="utf-8") as fh:
        original = fh.read()
    patched = patch_xhtml(original)
    if patched != original:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(patched)
        print("patched %s" % path)
    else:
        print("no change %s" % path)


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    for target in argv[1:]:
        if not os.path.exists(target):
            print("skip (missing): %s" % target)
            continue
        if target.lower().endswith(".epub"):
            patch_epub(target)
        else:
            patch_html_file(target)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
