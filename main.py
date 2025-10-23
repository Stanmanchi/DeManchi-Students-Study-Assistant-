import os
import io
import threading
import math
import requests
import tempfile
from flask import Flask, request, jsonify
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse
import openai

# Optional libs for docx/pdf/extraction/ocr
try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None

try:
    from docx import Document as DocxDocument
except Exception:
    DocxDocument = None

# Optional OCR libs (need system tesseract installed)
try:
    from pdf2image import convert_from_path
    import pytesseract
    from PIL import Image
except Exception:
    convert_from_path = None
    pytesseract = None
    Image = None

# ---------- Configuration from ENV ----------
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")  # e.g. "whatsapp:+1415XXXXXXX"

if not OPENAI_API_KEY or not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_PHONE_NUMBER:
    raise RuntimeError("Missing one of OPENAI_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER env vars")

openai.api_key = OPENAI_API_KEY
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

app = Flask(__name__)

# ---------- Utility: download file ----------
def download_file(url):
    resp = requests.get(url, stream=True, timeout=30)
    resp.raise_for_status()
    tmp = tempfile.NamedTemporaryFile(delete=False)
    for chunk in resp.iter_content(chunk_size=8192):
        if chunk:
            tmp.write(chunk)
    tmp.flush()
    return tmp.name

# ---------- Utility: extract text ----------
def extract_text_from_docx(path):
    if not DocxDocument:
        raise RuntimeError("python-docx not installed")
    doc = DocxDocument(path)
    paragraphs = []
    for p in doc.paragraphs:
        if p.text and p.text.strip():
            paragraphs.append(p.text.strip())
    return "\n".join(paragraphs)

def extract_text_from_pdf(path):
    """
    Try to extract text using PyMuPDF (fitz). If result is very small
    (likely scanned pages), and OCR libs are present, try OCR.
    Returns full text with simple page markers.
    """
    text_pages = []
    if fitz:
        doc = fitz.open(path)
        for i, page in enumerate(doc):
            page_text = page.get_text("text").strip()
            if page_text:
                text_pages.append(f"[page {i+1}]\n{page_text}")
            else:
                text_pages.append("")  # placeholder
    else:
        raise RuntimeError("PyMuPDF (fitz) not installed")
    full_text = "\n\n".join([p for p in text_pages if p])

    # If text is too short and OCR is available, attempt OCR
    if (not full_text or len(full_text) < 200) and convert_from_path and pytesseract:
        try:
            images = convert_from_path(path, dpi=200)
            ocr_text_pages = []
            for i, img in enumerate(images):
                txt = pytesseract.image_to_string(img)
                ocr_text_pages.append(f"[page {i+1}]\n{txt.strip()}")
            full_text = "\n\n".join(ocr_text_pages)
        except Exception as e:
            # fallback: return whatever we have from extract
            print("OCR failed:", e)
    return full_text

def extract_text_from_url(url):
    """Detect file type by extension and extract text accordingly."""
    path = download_file(url)
    # decide by extension
    lower = url.lower()
    try:
        if lower.endswith(".pdf") or ".pdf?" in lower:
            return extract_text_from_pdf(path)
        elif lower.endswith(".docx") or ".docx?" in lower:
            return extract_text_from_docx(path)
        else:
            # try pdf first, then docx, else attempt to read as text/html and extract visible text
            if lower.endswith(".txt"):
                with open(path, "r", encoding="utf8", errors="ignore") as f:
                    return f.read()
            # fallback: try pdf, then docx
            try:
                return extract_text_from_pdf(path)
            except Exception:
                try:
                    return extract_text_from_docx(path)
                except Exception:
                    # try to read raw bytes and decode
                    with open(path, "rb") as f:
                        raw = f.read()
                    try:
                        return raw.decode("utf-8", "ignore")
                    except Exception:
                        return ""
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass

# ---------- Chunking ----------
def chunk_text(text, chunk_size=3000, overlap=500):
    text = text.replace("\r", "\n")
    paragraphs = text.split("\n\n")
    chunks = []
    current = ""
    for p in paragraphs:
        if not p.strip():
            continue
        if len(current) + len(p) + 2 <= chunk_size:
            current += ("\n\n" + p) if current else p
        else:
            # push current
            chunks.append(current)
            # start new with overlap
            if overlap > 0:
                overlap_text = current[-overlap:]
                current = overlap_text + "\n\n" + p
            else:
                current = p
    if current.strip():
        chunks.append(current)
    # as a safety: if any chunk > chunk_size, split by chars
    final_chunks = []
    for c in chunks:
        if len(c) <= chunk_size:
            final_chunks.append(c)
        else:
            for i in range(0, len(c), chunk_size - overlap):
                final_chunks.append(c[i:i + chunk_size])
    return final_chunks

# ---------- OpenAI helpers ----------
def extract_keypoints_from_chunk(chunk_text, chunk_index=None):
    """
    Use OpenAI chat completion to extract bullet key points from a chunk.
    Returns a short bullet list text.
    """
    system = (
        "You are an expert academic study-assistant. "
        "Given a chunk of study material, extract the MOST exam-relevant key points and facts "
        "— concise bullets, include definitions, formulas, names, dates, and any examples. "
        "If the chunk contains headings or page markers, respect them."
    )
    user = f"Chunk index: {chunk_index}\n\nText:\n{chunk_text}\n\nProduce a numbered list (or bullets) of the key points. Keep each bullet concise (1-2 sentences)."
    try:
        resp = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[{"role":"system","content":system},{"role":"user","content":user}],
            temperature=0.0,
            max_tokens=800
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print("OpenAI chunk extraction error:", e)
        # fallback: return first 5 lines
        return "\n".join(chunk_text.splitlines()[:10])

def synthesize_keypoints(all_chunk_points):
    """
    Combine all chunk-level keypoints into a single, de-duplicated, prioritized keypoint list.
    """
    system = (
        "You are an expert curriculum writer and exam question designer. "
        "Given a list of chunk-level key points from a course packet, synthesize them into a single, structured set of comprehensive key points "
        "arranged by major headings when possible. Remove duplicates, combine repeated points, and prioritize by likely exam relevance. "
        "Where possible, include short page references (if provided) and tag each keypoint as [Easy/Medium/Hard] difficulty."
    )
    user = "Below are chunk-level keypoints. Combine and synthesize into a concise but comprehensive study guide covering all major ideas. Keep bullets short.\n\n"
    for i, cp in enumerate(all_chunk_points):
        user += f"--- CHUNK {i+1} ---\n{cp}\n\n"
    try:
        resp = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[{"role":"system","content":system},{"role":"user","content":user}],
            temperature=0.0,
            max_tokens=1500
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print("OpenAI synth error:", e)
        return "\n".join(all_chunk_points[:10])

# ---------- Message sending helper ----------
def send_whatsapp_message(to_whatsapp_number, text):
    # Twilio expects whatsapp:+number format for both from and to
    # split text into reasonable chunks for readability (e.g., 3000 chars)
    max_len = 3000
    for i in range(0, len(text), max_len):
        part = text[i:i+max_len]
        twilio_client.messages.create(
            body=part,
            from_=TWILIO_PHONE_NUMBER,
            to=to_whatsapp_number
        )

# ---------- Document processing pipeline ----------
def process_document_link_and_send(url, to_whatsapp):
    try:
        send_whatsapp_message(to_whatsapp, "✅ Got the link. Downloading and analyzing the document now. This may take a bit for long files (I will send key points when ready).")
        text = extract_text_from_url(url)
        if not text or len(text.strip()) < 50:
            send_whatsapp_message(to_whatsapp, "⚠️ I could not extract text from that link. If it's a scanned PDF, OCR may be required (server must have tesseract). Please try a text PDF or public docx link.")
            return

        # chunk
        chunks = chunk_text(text, chunk_size=3000, overlap=500)
        send_whatsapp_message(to_whatsapp, f"🔎 Document split into {len(chunks)} chunks. Analyzing each chunk now...")

        # per-chunk extraction
        chunk_points = []
        for i, c in enumerate(chunks, start=1):
            # optional: send progress messages for very big docs every N chunks
            cp = extract_keypoints_from_chunk(c, chunk_index=i)
            chunk_points.append(cp)

        send_whatsapp_message(to_whatsapp, "🧠 Synthesizing chunk-level key points into a comprehensive study guide...")

        final = synthesize_keypoints(chunk_points)

        # final safety: if final is too long, split by headings or pages
        if len(final) < 2000:
            send_whatsapp_message(to_whatsapp, "✅ Analysis complete. Here are the comprehensive key points:\n\n" + final)
        else:
            # try to split by headings "Chapter" or newlines into digestible parts
            parts = []
            # naive split by double newlines + headings
            if "Chapter" in final or "CHAPTER" in final or "\n\n" in final:
                # split by double newline and send in parts
                parts = [p for p in final.split("\n\n") if p.strip()]
                # recombine into paragraphs of ~2000 chars
                out_parts = []
                cur = ""
                for p in parts:
                    if len(cur) + len(p) + 2 <= 1800:
                        cur += ("\n\n" + p) if cur else p
                    else:
                        out_parts.append(cur)
                        cur = p
                if cur:
                    out_parts.append(cur)
                for op in out_parts:
                    send_whatsapp_message(to_whatsapp, op)
            else:
                # fallback: split by 3000 chars
                send_whatsapp_message(to_whatsapp, "✅ Analysis complete. Sending results in parts...")
                send_whatsapp_message(to_whatsapp, final)
    except Exception as e:
        print("Processing error:", e)
        try:
            send_whatsapp_message(to_whatsapp, f"❌ An error occurred while processing: {e}")
        except Exception:
            pass

# ---------- Flask route: Twilio webhook ----------
@app.route("/webhook", methods=["POST"])
def whatsapp_webhook():
    """
    Twilio will POST here for incoming WhatsApp messages.
    Expect the student to paste a link in the message body.
    We'll ACK immediately and process in background.
    """
    incoming = request.values.get("Body", "").strip()
    from_number = request.values.get("From")  # e.g., "whatsapp:+234xxxxxxxx"
    resp = MessagingResponse()

    if not incoming:
        resp.message("Please send a link to the document you want analyzed (PDF or DOCX).")
        return str(resp)

    # quick validation: does it look like a URL?
    if incoming.startswith("http://") or incoming.startswith("https://"):
        resp.message("Thanks — I received your link and will start analyzing. You'll get the key points in WhatsApp when ready.")
        # process in background so Twilio doesn't wait
        thread = threading.Thread(target=process_document_link_and_send, args=(incoming, from_number))
        thread.daemon = True
        thread.start()
    else:
        resp.message("I expected a link (starting with http:// or https://). Please paste the link to the PDF or DOCX and send it again.")
    return str(resp)

# ---------- healthcheck ----------
@app.route("/", methods=["GET"])
def index():
    return jsonify({"status":"ok","info":"WhatsApp keypoint-extractor bot"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
