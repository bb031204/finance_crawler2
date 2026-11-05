"""Restore PDF paragraphs deterministically; preserve layout-sensitive tables as reviewable blocks."""
from collections import Counter
from io import BytesIO
from .core import ROOT
import os
import logging
import re
import shutil
import subprocess

VERSION = "pdf_layout_v3_ocr"
BULLET = re.compile(r"^\s*[\u2022\u25cf\u25aa\u25a0\uf06c\u25ba]\s*")
SECTION = re.compile(r"^(?:[一二三四五六七八九十百]+[、．]|[（(][一二三四五六七八九十百]+[）)]|第[一二三四五六七八九十百0-9]+[章节])")
PAGE_NUMBER = re.compile(r"^(?:[-—－]\s*)?\d{1,4}(?:\s*[-—－])?$|^第\s*\d+\s*页(?:\s*共\s*\d+\s*页)?$")
FIELD = re.compile(r"^(?:增持主体|拟增持|首次增持|本次是否|持股数量|持股比例|本次增持前|本次增持后)")
ENDING = re.compile(r"[。！？!?；;]$")


def normalized_lines(text):
    text = re.sub(r"\r+\n", "\n", text).replace("\r", "\n")
    return text.expandtabs(4).split("\n")


def join_lines(parts):
    value = ""
    for part in parts:
        part = re.sub(r"\s+", " ", part).strip()
        if not part:
            continue
        if value and value[-1].isascii() and value[-1].isalnum() and part[0].isascii() and part[0].isalnum():
            value += " "
        value += part
    return value


def is_section(text):
    return bool(SECTION.match(text)) and len(text) < 85


def is_table_line(line):
    text = line.strip()
    cells = re.split(r" {2,}", text)
    if len(cells) < 2:
        return False
    if "《" in text and len(text) >= 25 and not FIELD.match(text):
        return False
    # Very long prose with an incidental alignment gap is not a verified table.
    if len(text) > 100 and ENDING.search(text):
        return False
    return (len(cells) >= 3 or bool(FIELD.match(text)) or
            any(re.search(r"\d|%|是|否|股份|金额|比例|名称|项目|单位|本年|上年", c) for c in cells))


def blocks_from_pages(page_texts):
    """Return full paragraphs/table blocks with source line ranges; never infer missing words."""
    pages = [normalized_lines(text) for text in page_texts]
    boundary_counts = Counter()
    for lines in pages:
        nonempty = [(i, line.strip()) for i, line in enumerate(lines) if line.strip()]
        for _, text in nonempty[:1] + nonempty[-1:]:
            boundary_counts[text] += 1
    repeated = {text for text, count in boundary_counts.items()
                if len(pages) >= 3 and count >= max(3, len(pages) * 0.65) and len(text) < 100
                and not re.search(r"本页无正文|签字|签名", text)}
    blocks, removed = [], []
    pending, refs, kind, section = [], [], None, None
    blank = False

    def flush():
        nonlocal pending, refs, kind
        if not pending:
            return
        if kind == "table":
            text = "\n".join(pending).rstrip()
        else:
            text = join_lines(pending)
        if text:
            spans = []
            for page, line in refs:
                if spans and spans[-1]["page"] == page and spans[-1]["line_end"] + 1 == line:
                    spans[-1]["line_end"] = line
                else:
                    spans.append({"page": page, "line_start": line, "line_end": line})
            blocks.append({"text": text, "kind": kind or "paragraph", "section": section,
                           "page_start": spans[0]["page"], "page_end": spans[-1]["page"],
                           "source_spans": spans})
        pending, refs, kind = [], [], None

    def begin(line, page, number, new_kind):
        nonlocal kind
        kind = new_kind
        pending.append(line)
        refs.append((page, number))

    for page_number, lines in enumerate(pages, 1):
        nonempty = [i for i, line in enumerate(lines) if line.strip()]
        for index, line in enumerate(lines):
            text = line.strip()
            if not text:
                blank = True
                continue
            boundary = index in nonempty[:1] + nonempty[-1:]
            page_marker = bool(PAGE_NUMBER.fullmatch(text))
            if text.isdigit() and (index != nonempty[-1] or kind == "table" or text == "0"):
                page_marker = False
            if boundary and (page_marker or text in repeated):
                removed.append({"page": page_number, "line": index + 1, "text": text, "reason": "page_header_footer"})
                continue
            indent = len(line) - len(line.lstrip())
            bullet = bool(BULLET.match(line))
            heading = is_section(text) or text in {"重要内容提示：", "重要内容提示", "特别提示："}
            title_start = page_number == 1 and text.startswith("关于") and len(blocks) < 6
            header = bool(re.match(r"^(证券代码|股票代码)[：:]", text)) and page_number == 1
            if heading:
                flush()
                section = text
                begin(text, page_number, index+1, "heading")
                flush()
            elif header:
                flush()
                begin(text, page_number, index+1, "header")
                flush()
            elif title_start:
                flush()
                begin(text, page_number, index+1, "heading")
                if text.endswith(("公告", "报告", "报告书", "法律意见", "法律意见书", "会议决议")):
                    flush()
            elif bullet:
                flush()
                begin("• " + BULLET.sub("", line).strip(), page_number, index+1, "paragraph")
            elif kind == "heading":
                pending.append(text)
                refs.append((page_number,index+1))
                if text.endswith(("公告", "报告", "报告书", "法律意见", "法律意见书", "会议决议")):
                    flush()
            elif kind == "table":
                # A paragraph resuming after a blank and indent ends a table region.
                prose = (blank and indent >= 2 and len(text) >= 30 and not is_table_line(line)
                         and not re.match(r"^[（(]?(?:占|注|[0-9])",text))
                if prose:
                    flush()
                    begin(text,page_number,index+1,"paragraph")
                else:
                    if blank and pending and pending[-1] != "":
                        pending.append("")
                    pending.append(line.rstrip())
                    refs.append((page_number,index+1))
            elif is_table_line(line):
                # Keep prose that is already mid-sentence together.
                if pending and not blank and not ENDING.search(pending[-1].strip()) and not FIELD.match(text):
                    pending.append(text)
                    refs.append((page_number,index+1))
                else:
                    flush()
                    begin(line.rstrip(),page_number,index+1,"table")
            else:
                new_paragraph = blank or (
                    pending and indent >= 2 and ENDING.search(pending[-1].strip()))
                if new_paragraph:
                    flush()
                begin(text,page_number,index+1,"paragraph") if not pending else (
                    pending.append(text), refs.append((page_number,index+1)))
                # Company heading preceding a wrapped article title stays separate.
                if page_number == 1 and len(blocks) < 4 and len(text) < 40 and text.endswith(("有限公司", "股份公司")):
                    kind = "heading"
                    flush()
            blank = False
        # Tables are page-scoped; continuous prose may legitimately cross a page.
        if kind == "table":
            flush()
        elif pending and ENDING.search(pending[-1].strip()):
            flush()
        blank = False
    flush()
    return blocks, removed


def extract_pdf_layout(data, ocr_options=None):
    from pypdf import PdfReader
    if not data.lstrip()[:1024].startswith(b"%PDF"):
        raise ValueError("响应不是PDF文件（可能是HTML错误/登录页），禁止按PDF解析")
    warnings = []
    class Capture(logging.Handler):
        def emit(self, record):
            warnings.append(record.getMessage())
    logger = logging.getLogger("pypdf")
    handler = Capture()
    old_handlers, old_propagate = logger.handlers[:], logger.propagate
    logger.handlers, logger.propagate = [handler], False
    try:
        reader = PdfReader(BytesIO(data))
        page_count = len(reader.pages)
    finally:
        logger.handlers, logger.propagate = old_handlers, old_propagate
    if reader.is_encrypted:
        raise ValueError("加密PDF需合法可读原件，不自动解密")
    tool = shutil.which("pdftotext")
    backend = "pypdf_layout_fallback"
    if tool:
        result = subprocess.run([tool, "-layout", "-enc", "UTF-8", "-", "-"], input=data,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode != 0:
            raise ValueError("PDF版面提取失败：" + result.stderr.decode("utf-8",errors="replace")[:400])
        page_texts = re.sub(r"\r+\n", "\n", result.stdout.decode("utf-8")).replace("\r","\n").split("\f")
        while len(page_texts) > len(reader.pages) and not page_texts[-1].strip():
            page_texts.pop()
        if len(page_texts) != len(reader.pages):
            raise ValueError("版面提取页数与PDF页数不一致")
        backend = "poppler_layout"
        if result.stderr:
            warnings.extend(result.stderr.decode("utf-8", errors="replace").splitlines()[:20])
    else:
        page_texts = [page.extract_text(extraction_mode="layout") or "" for page in reader.pages]
    empty_before = [i+1 for i,text in enumerate(page_texts) if not text.strip()]
    ocr_pages, ocr_failures = [], []
    options = ocr_options or {}
    if empty_before and options.get("ocr_enabled", False):
        from .pdf_ocr import recognize_pages
        recovered, ocr_failures = recognize_pages(data, empty_before,
            max_pages=options.get("ocr_max_pages", 16), root=options.get("root", ROOT))
        for number,text in recovered.items():
            page_texts[number-1] = text
            ocr_pages.append(number)
    blank_reviews = [f for f in ocr_failures if f.get("verified_blank")]
    blank_pages = [f["page"] for f in blank_reviews]
    ocr_failures = [f for f in ocr_failures if not f.get("verified_blank")]
    blocks, removed = blocks_from_pages(page_texts)
    def chars(value):
        value = re.sub(r"\s+", "", value)
        value = re.sub(r"[\u2022\u25cf\u25aa\u25a0\uf06c\u25ba]", "•", value)
        return Counter(value)
    expected = chars("".join(page_texts))
    expected.subtract(chars("".join(item["text"] for item in removed)))
    actual = chars("".join(item["text"] for item in blocks))
    if +expected != actual:
        raise ValueError("PDF段落重组字符核对失败，停止发布并保留原件")
    if not blocks:
        raise ValueError("PDF 无可提取文本，可能为扫描件；保留原件待 OCR")
    empty_pages = [i+1 for i,text in enumerate(page_texts) if not text.strip() and i+1 not in blank_pages]
    tables = [{"id":"T"+str(i), "text":b["text"], "page_start":b["page_start"],
               "page_end":b["page_end"], "source_spans":b["source_spans"],
               "extraction_status":"layout_preserved_not_cell_verified"}
              for i,b in enumerate((b for b in blocks if b["kind"]=="table"),1)]
    pages = [{"page":i+1,"characters":len(text)} for i,text in enumerate(page_texts)]
    quality = {"parser_version":VERSION,"backend":backend,
        "status":"needs_ocr" if empty_pages else ("ocr_unverified" if ocr_pages else "text_parsed"),
        "empty_text_pages_before_ocr":empty_before, "ocr_pages":sorted(ocr_pages),
        "ocr_failures":ocr_failures, "ocr_engine":"windows_zh-Hans-CN" if ocr_pages else None,
        "ocr_confidence":None, "requires_visual_review":bool(ocr_pages or empty_pages),
        "verified_blank_pages":blank_pages, "blank_page_verification":blank_reviews,
        "pdf_warnings":list(dict.fromkeys(warnings))[:20],
        "empty_text_pages":empty_pages,"table_status":"needs_cell_validation" if tables else "not_detected",
        "paragraph_restoration":"layout_and_rules; semantic boundaries require review",
        "content_accounting_passed":True,
        "removed_artifacts":removed}
    return {"paragraphs":[b["text"] for b in blocks], "paragraph_metadata":[{k:v for k,v in b.items() if k!="text"} for b in blocks],
            "tables":tables,"pages":pages,"quality":quality,
            "title":(reader.metadata or {}).get("/Title")}
