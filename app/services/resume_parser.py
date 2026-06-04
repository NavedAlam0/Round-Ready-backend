import os
import uuid

import pdfplumber


def save_upload(upload_dir: str, user_id: str, file_bytes: bytes, filename: str) -> str:
    user_dir = os.path.join(upload_dir, user_id)
    os.makedirs(user_dir, exist_ok=True)
    ext = os.path.splitext(filename)[1] or ".pdf"
    file_path = os.path.join(user_dir, f"{uuid.uuid4()}{ext}")
    with open(file_path, "wb") as f:
        f.write(file_bytes)
    return file_path


def parse_resume(file_path: str) -> dict:
    text_parts = []
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    raw_text = "\n\n".join(text_parts)
    return {"raw_text": raw_text, "pages": len(text_parts)}
