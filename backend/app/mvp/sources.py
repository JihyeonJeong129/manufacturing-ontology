"""원본 문서 등록부와 텍스트 추출.

원본 파일은 RAW_DATA_DIR(기본 /data/raw)에 있고, 적재 시 SHA-256이 등록부 값과 같아야 한다.
PDF는 PDFium(pypdfium2)으로 쪽별 텍스트를, Word 정의서는 제목(Heading) 단위 논리 블록으로 추출한다.
인용 span은 이 추출 텍스트의 부분 문자열이어야 하므로 추출 방식이 바뀌면 extraction_version을 올린다.
"""
from __future__ import annotations

import hashlib
import os
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PDF_EXTRACTION = "pdfium-text-v1"
DOCX_EXTRACTION = "docx-heading-blocks-v1"


@dataclass(frozen=True)
class SourceDocument:
    key: str                 # document_revisions.document_key
    revision: str            # document_revisions.revision_key
    filename: str            # RAW_DATA_DIR 안의 파일 이름 (NFC 기준 비교)
    sha256: str
    source_kind: str         # manual | team_analysis ...
    title: str
    publisher: str
    edition: str
    language: str
    manual_id: str | None    # 정의서의 manual_id (festo, milco, ...)
    drive_type: str | None   # 구동 방식: servo_pneumatic | pneumatic | servo_electric | control_software
    label_ko: str            # 인용 제목 앞에 붙는 짧은 한국어 이름
    pua_map: tuple[tuple[str, str], ...] = ()  # PDF 기호 글꼴(Symbol 등)의 사설 영역 문자 → 표준 문자

    @property
    def is_pdf(self) -> bool:
        return self.filename.lower().endswith(".pdf")

    @property
    def extraction_version(self) -> str:
        return PDF_EXTRACTION if self.is_pdf else DOCX_EXTRACTION

    @property
    def storage_relative(self) -> str:
        suffix = ".pdf" if self.is_pdf else ".docx"
        return f"documents/{self.key}-{self.revision}{suffix}"


SOURCES: dict[str, SourceDocument] = {s.key: s for s in [
    SourceDocument(
        "rsw-mvp-situations", "2026-09-25", "RSW 용접건 MVP 오류 상황 정의서.docx",
        "d3b471e180adf29f2dac511cc381ff87a335fc1bea8fd18344526cdd9b1f3be2", "team_analysis",
        "RSW 용접건 MVP 오류 상황 정의서", "워크비 팀", "2026-09-25 초안", "ko", None, "servo_pneumatic",
        "MVP 오류 상황 정의서"),
    SourceDocument(
        "rsw-manual-rag-guide", "v1", "RSW용접건 매뉴얼_RAG 활용정리.pdf",
        "57cf6f2df17a11d9f0e9569fb7c029228481b5e45d19d58397cd776d92ec2ef5", "team_analysis",
        "RSW 용접건 매뉴얼 5종 RAG·온톨로지·예지보전 활용 정리", "워크비 팀", "v1", "ko", None, None,
        "매뉴얼 RAG 활용정리"),
    SourceDocument(
        "festo-cpx-sb-lt-weld-sa", "v1.5-2014-05", "Festo Servopneumatic Drive System for Robot Welding Guns.pdf",
        "af7b8bdb5b420996364ed34571e0fc7f3356cd8776935b772294bf3a7a7e210b", "manual",
        "Servopneumatic drive system for robot welding guns (CPX-SB-LT-WELD-SA)", "Festo",
        "P.BE-K-SPC-WELD-SBLT-EN-SA V1.5 (May 2014)", "en", "festo", "servo_pneumatic", "Festo 서보 공압 구동계"),
    SourceDocument(
        "milco-modular-weld-guns", "2014-09-12", "MILCO Modular Weld Guns – Service & Technical Manual.pdf",
        "44c7e508c9d36f9ccfafb88682621192f13d7619d2c86bbbf580ee00af1028ce", "manual",
        "Modular Weld Guns - Service & Technical Manual", "MILCO Manufacturing", "09/12/14", "en", "milco",
        "pneumatic", "MILCO 공압식 용접건",
        # Symbol 글꼴: 0xB0 °, 0xB1 ±, 0xC6 ∅(지름 표기), 0x6D µ, 0x57 Ω
        (("\uf0b0", "°"), ("\uf0b1", "±"), ("\uf0c6", "⌀"), ("\uf06d", "µ"), ("\uf057", "Ω"))),
    SourceDocument(
        "yaskawa-yrc1000-air-gun", "re-cso-a055", "Yaskawa YRC1000 – Spot Welding Using Air Gun.pdf",
        "d0b7a58cfcb2e83203ee9e392a97e549044bc8fcf41011c5d6e20188842285aa", "manual",
        "YRC1000 Operator's Manual for Spot Welding Using Air Gun", "Yaskawa", "RE-CSO-A055", "en", "yaskawa",
        "pneumatic", "Yaskawa 공압 건 제어", (("\uf020", " "),)),
    SourceDocument(
        "abb-robotware6-spot-options", "3hac050979-001-g", "ABB RobotWare 6 – Spot Options.pdf",
        "c3c5a7d9056476fe8a6acba477d51e1fea8fc15b0c7d1049c80624bdcd075d4b", "manual",
        "Application manual - Spot options, RobotWare 6", "ABB", "3HAC050979-001 Revision G", "en", "abb",
        "control_software", "ABB 점용접 제어"),
    SourceDocument(
        "fanuc-r30ib-plus-servo-gun", "b-83264en-05", "FANUC R-30iB Plus Servo Gun Function.pdf",
        "d2107d458eff1683fb745904054588b18bb62e15314400f10e34f7765a5b7b6a", "manual",
        "R-30iB / R-30iB Plus Controller Servo Gun Function Operator's Manual", "FANUC", "B-83264EN/05", "en",
        "fanuc", "servo_electric", "FANUC 전동 서보 건"),
]}


class SourceError(RuntimeError):
    pass


def locate(raw_dir: Path, source: SourceDocument) -> Path:
    """파일 이름의 유니코드 정규화(NFC/NFD) 차이를 무시하고 원본 파일을 찾는다."""
    wanted = unicodedata.normalize("NFC", source.filename)
    if raw_dir.is_dir():
        for name in os.listdir(raw_dir):
            if unicodedata.normalize("NFC", name) == wanted:
                return raw_dir / name
    raise SourceError(f"원본 파일을 찾을 수 없습니다: {raw_dir}/{source.filename}")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class ExtractedPage:
    number: int              # PDF 쪽 번호 또는 논리 블록 번호 (1부터)
    label: str | None        # 인쇄 쪽 번호 / 블록 제목
    text: str


def _clean(text: str, pua_map: tuple[tuple[str, str], ...] = ()) -> str:
    # PDFium은 줄바꿈을 CRLF로, 줄끝 하이픈 분리를 U+FFFE로 돌려준다. 내용은 바꾸지 않고 표기만 정리한다.
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\ufffe", "")
    for private, standard in pua_map:
        text = text.replace(private, standard)
    return "\n".join(line.rstrip() for line in text.split("\n")).strip("\n")


def extract_pdf(path: Path, pua_map: tuple[tuple[str, str], ...] = ()) -> list[ExtractedPage]:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    try:
        pages = []
        for index in range(len(document)):
            page = document[index]
            textpage = page.get_textpage()
            try:
                pages.append(ExtractedPage(index + 1, None, _clean(textpage.get_text_range(), pua_map)))
            finally:
                textpage.close()
                page.close()
        return pages
    finally:
        document.close()


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _paragraph_text(p: ET.Element) -> str:
    parts = []
    for node in p.iter():
        if node.tag == _W + "t":
            parts.append(node.text or "")
        elif node.tag == _W + "tab":
            parts.append("\t")
        elif node.tag == _W + "br":
            parts.append("\n")
    return "".join(parts)


def _style(p: ET.Element) -> str:
    style = p.find(f"{_W}pPr/{_W}pStyle")
    return style.get(_W + "val") if style is not None else ""


def extract_docx(path: Path) -> list[ExtractedPage]:
    """제목(Heading1~3)마다 새 논리 블록을 시작한다. 목록은 '- ', 표는 행마다 ' | '로 셀을 잇는다."""
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    body = root.find(_W + "body")
    if body is None:
        raise SourceError("docx 본문이 없습니다.")
    blocks: list[tuple[str, list[str]]] = []
    for element in body:
        tag = element.tag.replace(_W, "")
        if tag == "p":
            text = _paragraph_text(element).strip()
            if not text:
                continue
            style = _style(element)
            if style.startswith("Heading"):
                blocks.append((text, [text]))
                continue
            if not blocks:
                blocks.append(("", []))
            blocks[-1][1].append(f"- {text}" if style == "ListParagraph" else text)
        elif tag == "tbl":
            if not blocks:
                blocks.append(("", []))
            for row in element.iter(_W + "tr"):
                cells = []
                for cell in row.findall(_W + "tc"):
                    lines = [_paragraph_text(p).strip() for p in cell.iter(_W + "p")]
                    cells.append(" / ".join(line for line in lines if line))
                blocks[-1][1].append(" | ".join(cells))
    return [ExtractedPage(i, heading or None, "\n".join(lines)) for i, (heading, lines) in enumerate(blocks, start=1)]


_LABEL_PATTERNS = {
    "festo-cpx-sb-lt-weld-sa": r"^(\d+)$",                       # 머리글 다음 줄의 쪽 번호
    "milco-modular-weld-guns": r"^Page (\d+) of \d+$",
    "yaskawa-yrc1000-air-gun": r"^(\d+-\d+)$",
    "abb-robotware6-spot-options": r"Application manual - Spot options (\d+)$",
    "fanuc-r30ib-plus-servo-gun": r"^-(\d+)-$",
}


def printed_label(source: SourceDocument, page: ExtractedPage) -> str | None:
    """인쇄된 쪽 번호. 찾지 못하면 None (PDF 쪽 번호로 인용한다)."""
    if not source.is_pdf:
        return page.label
    pattern = _LABEL_PATTERNS.get(source.key)
    if pattern is None:
        return str(page.number)
    import re

    for line in page.text.split("\n")[:6]:
        match = re.search(pattern, line.strip())
        if match:
            return match.group(1)
    return None


@lru_cache(maxsize=16)
def _extract_cached(path: str, mtime: float, source: SourceDocument) -> tuple[ExtractedPage, ...]:
    return tuple(extract_pdf(Path(path), source.pua_map) if source.is_pdf else extract_docx(Path(path)))


def extract(raw_dir: Path, source: SourceDocument, *, verify: bool = True) -> list[ExtractedPage]:
    path = locate(raw_dir, source)
    if verify:
        actual = file_sha256(path)
        if actual != source.sha256:
            raise SourceError(f"{source.filename}의 SHA-256이 등록부와 다릅니다 (등록 {source.sha256[:12]}…, 실제 {actual[:12]}…).")
    return list(_extract_cached(str(path), path.stat().st_mtime, source))
