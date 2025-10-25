import time
import openai
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse

# Twilio client
twilio_client = TwilioClient("your_account_sid", "your_auth_token")
TWILIO_PHONE_NUMBER = "your_twilio_phone_number"

def send_whatsapp_message(to_whatsapp_number, text):
    twilio_client.messages.create(
        body=text,
        from_=TWILIO_PHONE_NUMBER,
        to=to_whatsapp_number
    )

def send_typing_animation(to_whatsapp_number):
    typing_messages = [
        "Processing...",
        "Processing..",
        "Processing...",
        "Processing...."
    ]
    for msg in typing_messages:
        twilio_client.messages.create(
            body=msg,
            from_=TWILIO_PHONE_NUMBER,
            to=to_whatsapp_number
        )
        time.sleep(1)  # Delay to simulate animation

def handle_incoming_message(incoming_message, from_number):
    # If the message is "Hi", respond with a friendly greeting
    if "hi" in incoming_message.lower() or "hello" in incoming_message.lower():
        send_whatsapp_message(from_number, "Hello! How are you today? 😊 Do you have a file I can help analyze for you?")
        return

    # If the message is a link (URL to a PDF or DOCX file), process it
    if incoming_message.startswith("http://") or incoming_message.startswith("https://"):
        send_whatsapp_message(from_number, "✅ I have received your file! Analyzing it now... Please wait a moment.")
        send_typing_animation(from_number)  # Simulate processing

        try:
            # Start processing the document (You can define your own method here)
            file_url = incoming_message
            text = extract_text_from_url(file_url)  # Use your method to extract text from the file

            if not text:
                raise Exception("Failed to extract text from the PDF file.")

            # Send progress updates (optional, you can adjust this for your use case)
            send_whatsapp_message(from_number, "🔍 Analyzing the content... Please wait.")
            send_whatsapp_message(from_number, "🔎 Processing pages...")

            # Example: simulate processing multiple pages
            num_pages = 5  # Just an example
            for i in range(1, num_pages + 1):
                send_whatsapp_message(from_number, f"📄 Analyzing page {i} of {num_pages}...")

            # Final completion message
            send_whatsapp_message(from_number, "✅ Analysis complete. Here are the key points:")
            send_whatsapp_message(from_number, "• Key point 1")
            send_whatsapp_message(from_number, "• Key point 2")
            send_whatsapp_message(from_number, "• Key point 3")

        except Exception as e:
            # If there's an error, report it
            send_whatsapp_message(from_number, f"❌ Error: {str(e)}. Please try again.")
    else:
        # If the message is not a valid link, ask for a file
        send_whatsapp_message(from_number, "Oops! I need a link to a valid PDF or DOCX file to analyze. Please send a file link.")
        
def extract_text_from_url(url):
    # Implement your file extraction logic here (e.g., using PDF parsing or OCR)
    # For now, let's return some dummy text for testing purposes
    return "Dummy text from the document."

# Assuming this is your Flask route to handle the incoming webhook (you would adjust this to match your route)
@app.route("/webhook", methods=["POST"])
def whatsapp_webhook():
    incoming_message = request.values.get("Body", "").strip()
    from_number = request.values.get("From")
    resp = MessagingResponse()

    # Handle the message based on its content
    handle_incoming_message(incoming_message, from_number)

    return str(resp)
