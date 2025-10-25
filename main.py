import os
import io
import threading
import requests
from flask import Flask, request, jsonify
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse
import openai

# Initialize Flask app
app = Flask(__name__)

# ---------- Configuration from ENV ----------
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")  # e.g. "whatsapp:+1415XXXXXXX"

if not OPENAI_API_KEY or not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_PHONE_NUMBER:
    raise RuntimeError("Missing one of OPENAI_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER env vars")

openai.api_key = OPENAI_API_KEY
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

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
def extract_text_from_url(url):
    path = download_file(url)
    # Decide by extension
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
            try:
                return extract_text_from_pdf(path)
            except Exception:
                try:
                    return extract_text_from_docx(path)
                except Exception:
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
            chunks.append(current)
            if overlap > 0:
                overlap_text = current[-overlap:]
                current = overlap_text + "\n\n" + p
            else:
                current = p
    if current.strip():
        chunks.append(current)
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
        return "\n".join(chunk_text.splitlines()[:10])

def synthesize_keypoints(all_chunk_points):
    system = (
        "You are an expert curriculum writer and exam question designer. "
        "Given a list of chunk-level key points from a course packet, synthesize them into a single, de-duplicated, prioritized keypoint list."
    )
    user = "Below are chunk-level keypoints. Combine and synthesize them into a concise but comprehensive study guide covering all major ideas."
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
    max_len = 3000
    for i in range(0, len(text), max_len):
        part = text[i:i+max_len]
        twilio_client.messages.create(
            body=part,
            from_=TWILIO_PHONE_NUMBER,
            to=to_whatsapp_number
        )

# ---------- Flask route: Twilio webhook ----------
@app.route("/webhook", methods=["POST"])
def webhook():
    incoming = request.values.get("Body", "").strip()
    from_number = request.values.get("From")  
    resp = MessagingResponse()

    if not incoming:
        resp.message("Hello! How are you today? Do you have a PDF or document that I can analyze for you? Please share it!")
        return str(resp)

    if incoming.startswith("http://") or incoming.startswith("https://"):
        resp.message("Thanks — I received your link and will start analyzing it. You'll get the key points soon.")
        thread = threading.Thread(target=process_document_link_and_send, args=(incoming, from_number))
        thread.daemon = True
        thread.start()
    else:
        resp.message("Please send a link (starting with http:// or https://) to a PDF or DOCX document. I'll extract key points for you!")
    return str(resp)

# ---------- healthcheck ----------
@app.route("/", methods=["GET"])
def index():
    return jsonify({"status":"ok","info":"WhatsApp keypoint-extractor bot"})

if __name__ == "__main__":
    app.run(debug=True)
