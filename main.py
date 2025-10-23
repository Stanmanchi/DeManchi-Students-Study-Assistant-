from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import os
import openai
import requests
from io import BytesIO
from PyPDF2 import PdfReader
import docx

app = Flask(__name__)
openai.api_key = os.getenv("OPENAI_API_KEY")

def extract_text_from_pdf_link(url):
    response = requests.get(url)
    pdf_file = BytesIO(response.content)
    reader = PdfReader(pdf_file)
    text = ""
    for page in reader.pages:
        text += page.extract_text() + "\n"
    return text

def extract_text_from_docx_link(url):
    response = requests.get(url)
    doc_file = BytesIO(response.content)
    doc = docx.Document(doc_file)
    text = ""
    for para in doc.paragraphs:
        text += para.text + "\n"
    return text

def generate_keypoints(text):
    prompt = f"Extract the most important key points from this study material:\n{text}"
    response = openai.Completion.create(
        engine="text-davinci-003",
        prompt=prompt,
        max_tokens=1500
    )
    return response.choices[0].text.strip()

@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.values.get("Body", "").strip()
    resp = MessagingResponse()

    if incoming_msg.startswith("http://") or incoming_msg.startswith("https://"):
        if incoming_msg.lower().endswith(".pdf"):
            text = extract_text_from_pdf_link(incoming_msg)
        elif incoming_msg.lower().endswith(".docx"):
            text = extract_text_from_docx_link(incoming_msg)
        else:
            text = "Sorry, I can only process PDF or Word (.docx) links for now."
            resp.message(text)
            return str(resp)

        keypoints = generate_keypoints(text)
        resp.message(keypoints)
    else:
        resp.message("Please send me a link to a PDF or Word document for analysis.")

    return str(resp)

if __name__ == "__main__":
    app.run(debug=True)
