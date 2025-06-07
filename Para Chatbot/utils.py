import os
from typing import Optional
from io import StringIO

def extract_text_from_txt(filepath: str) -> Optional[str]:
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        return None

def extract_text_from_pdf(filepath: str) -> Optional[str]:
    try:
        import PyPDF2
        text = ""
        with open(filepath, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                text += page.extract_text() + "\\n"
        return text.strip()
    except Exception as e:
        return None

def extract_text_from_docx(filepath: str) -> Optional[str]:
    try:
        import docx
        doc = docx.Document(filepath)
        full_text = []
        for para in doc.paragraphs:
            full_text.append(para.text)
        return "\\n".join(full_text).strip()
    except Exception as e:
        return None

def extract_text_from_file(filepath: str) -> Optional[str]:
    ext = os.path.splitext(filepath)[1].lower()
    if ext == '.txt':
        return extract_text_from_txt(filepath)
    elif ext == '.pdf':
        return extract_text_from_pdf(filepath)
    elif ext == '.docx':
        return extract_text_from_docx(filepath)
    else:
        return None
