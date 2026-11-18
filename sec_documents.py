"""Choose readable documents from one verified SEC accession, never from other filings."""
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, parse_qs, unquote

SEC_DOCUMENT_VERSION = 3
EXHIBIT_TYPE = re.compile(r'EX-(?:99|13)(?:\.\d+)?', re.I)
RELEVANT_DESCRIPTION = re.compile(r'earnings|results|financial|press\s+release|annual\s+report|outlook|guidance|acquisition|merger|dividend', re.I)


def document_policy(sec):
    return {'include_exhibits': sec.include_exhibits, 'max_exhibits_per_filing': sec.max_exhibits_per_filing}


def accession_document_url(index_url, href, cik, accession):
    url = urljoin(index_url, href)
    parts = urlsplit(url)
    if parts.hostname == 'www.sec.gov' and parts.path in {'/ix', '/ix.xhtml', '/ixviewer/doc/action'}:
        document = parse_qs(parts.query).get('doc', [None])[0]
        if not document:
            raise ValueError('SEC iXBRL链接缺少文档路径')
        url = urljoin(index_url, document)
        parts = urlsplit(url)
    prefix = f"/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"
    decoded = unquote(parts.path)
    if (parts.scheme != 'https' or parts.hostname != 'www.sec.gov' or parts.port not in {None, 443}
            or parts.username or parts.password or parts.query or parts.fragment
            or not parts.path.startswith(prefix) or not decoded.startswith(prefix)
            or '\\' in decoded or '%' in decoded
            or any(piece in {'.', '..'} for piece in decoded.split('/'))):
        raise ValueError('SEC文档不属于当前申报目录，保留待复核')
    return url


@dataclass(frozen=True)
class FilingDocument:
    sequence: int
    kind: str
    description: str
    href: str


def filing_documents(tree, expected_form=None):
    main, exhibits = [], []
    for tr in tree.xpath("//table[contains(concat(' ',normalize-space(@class),' '),' tableFile ')]//tr"):
        cells = tr.xpath('./td')
        if len(cells) < 4:
            continue
        links = cells[2].xpath('.//a/@href')
        sequence = ' '.join(cells[0].itertext()).strip()
        kind = ' '.join(cells[3].itertext()).strip()
        description = ' '.join(cells[1].itertext()).strip()
        if not links or not sequence.isdigit():
            continue
        document = FilingDocument(int(sequence), kind, description, links[0])
        if EXHIBIT_TYPE.fullmatch(kind):
            path = urlsplit(links[0]).path.lower()
            if not path.endswith(('.jpg', '.jpeg', '.gif', '.png', '.xml', '.xsd', '.zip')):
                exhibits.append(document)
        elif (kind == expected_form if expected_form else bool(re.fullmatch(r'(?:\d+-[A-Z]+|[A-Z]+)(?:/A)?', kind))):
            main.append(document)
    if not main:
        raise ValueError('SEC索引未找到对应类型的主文档，保留待复核')
    exhibits.sort(key=lambda doc: (not bool(RELEVANT_DESCRIPTION.search(doc.description)), doc.sequence))
    return min(main, key=lambda doc: doc.sequence), exhibits
