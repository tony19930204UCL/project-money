"""Bounded official disclosure extraction; text/tables only, never a trade signal."""
from __future__ import annotations
from html.parser import HTMLParser
from io import BytesIO
from urllib.parse import urljoin, urlparse
import re

ALLOWED_HOSTS = {'www.microsoft.com', 'investor.tsmc.com'}
MAX_BYTES = 8_000_000

class DisclosureHTMLParser(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base=base; self.skip=0; self.table_depth=0; self.table=[]; self.tables=[]
        self.anchor=None; self.links=[]; self.paragraph=None; self.paragraphs=[]
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag in {'script','style','noscript'}: self.skip+=1
        if self.skip: return
        if tag=='table':
            if self.table_depth==0: self.table=[]
            self.table_depth+=1
        if tag in {'tr','td','th','br'} and self.table_depth: self.table.append(' | ')
        if tag=='a': self.anchor=[urljoin(self.base,attrs.get('href','')),[]]
        if tag=='p': self.paragraph=[]
    def handle_endtag(self,tag):
        if tag in {'script','style','noscript'}:
            self.skip=max(0,self.skip-1); return
        if self.skip: return
        if tag=='table' and self.table_depth:
            self.table_depth-=1
            if not self.table_depth: self.tables.append(self.clean(' '.join(self.table)))
        if tag=='a' and self.anchor:
            self.links.append({'document_part':'link','text':self.clean(' '.join(self.anchor[1])),'href':self.anchor[0]}); self.anchor=None
        if tag=='p' and self.paragraph is not None:
            self.paragraphs.append(self.clean(' '.join(self.paragraph))); self.paragraph=None
    def handle_data(self,data):
        if self.skip: return
        if self.table_depth: self.table.append(data)
        if self.anchor: self.anchor[1].append(data)
        if self.paragraph is not None: self.paragraph.append(data)
    @staticmethod
    def clean(text): return re.sub(r'\s+',' ',text).strip()

def parse_official_document(url, body, content_type):
    if urlparse(url).hostname not in ALLOWED_HOSTS:
        raise ValueError('UNAPPROVED_DISCLOSURE_HOST')
    if len(body)>MAX_BYTES: raise ValueError('OFFICIAL_DOCUMENT_OVERSIZE')
    if body.startswith(b'%PDF') or 'application/pdf' in content_type:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            # The supported install path is the declared `official` extra.
            # Never fall back to an ad-hoc candidate-local vendor directory.
            raise RuntimeError('PDF_PARSER_UNAVAILABLE') from exc
        pdf=PdfReader(BytesIO(body))
        rows=[]
        for i,page in enumerate(pdf.pages[:10]):
            text=(page.extract_text() or '').strip()
            if text: rows.append({'document_part':f'PDF page {i+1}','text':text})
        if not rows: raise ValueError('NO_EXTRACTABLE_OFFICIAL_PDF_TEXT')
        return rows
    parser=DisclosureHTMLParser(url);parser.feed(body.decode('utf-8',errors='replace'))
    rows=[{'document_part':f'HTML table {i+1}','text':text} for i,text in enumerate(parser.tables) if text]
    keywords=('revenue','remaining performance','azure','operating income','capital','lease','openai','cash flow','financial statement')
    rows += [{'document_part':f'HTML paragraph {i+1}','text':text} for i,text in enumerate(parser.paragraphs)
             if 40<=len(text)<=4500 and any(k in text.lower() for k in keywords)][:28]
    rows += [row for row in parser.links if row['text']=='Financial Statements' and urlparse(row['href']).hostname=='investor.tsmc.com']
    if not rows: raise ValueError('NO_SUPPORTED_OFFICIAL_DISCLOSURE_CONTENT')
    if sum(len(row['text']) for row in rows)>120_000: raise ValueError('OFFICIAL_EXCERPT_BUDGET_EXCEEDED')
    return rows
