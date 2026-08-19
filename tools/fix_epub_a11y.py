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

Usage:  python3 tools/fix_epub_a11y.py docs/thesis.epub [docs/index.html ...]
"""

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
                    ).encode("utf-8")
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
