import time
import random
from fastapi import FastAPI, Request, BackgroundTasks, Query
from fastapi.responses import JSONResponse, FileResponse
from dotenv import load_dotenv
from models import Order, Base, engine, SessionLocal
from knowledge_loader import load_csv, load_excel, load_google_sheet, scrape_website
from openai import OpenAI
import os, requests, logging, datetime, io
from fastapi.logger import logger as fastapi_logger
from knowledge_manager import KnowledgeManager
from fpdf import FPDF

# Load environment variables
load_dotenv()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
LIVE_AGENT_WHATSAPP_NUMBERS = os.getenv("LIVE_AGENT_WHATSAPP_NUMBERS", "").split(",")

client = OpenAI(api_key=OPENAI_API_KEY)
Base.metadata.create_all(bind=engine)
fastapi_logger.setLevel(logging.INFO)
logger = fastapi_logger
app = FastAPI()

knowledge_manager = KnowledgeManager()
session_store = {}
pending_orders = {}

ORDER_STEPS = ["item", "quantity", "portion", "price", "address", "confirmation"]
MAX_HISTORY_LENGTH = 10
TOKEN_LIMIT_THRESHOLD = 3000

try:
    import tiktoken
    tokenizer = tiktoken.get_encoding("cl100k_base")
except ImportError:
    tokenizer = None

def count_tokens(messages):
    if not tokenizer:
        return 0
    return sum(len(tokenizer.encode(value)) for msg in messages for value in msg.values())

@app.get("/")
async def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return int(challenge) if challenge else JSONResponse(content={"error": "Missing challenge"}, status_code=400)
    return JSONResponse(content={"error": "Verification failed"}, status_code=403)

@app.get("/orders")
def list_orders(skip: int = 0, limit: int = 10):
    db = SessionLocal()
    orders = db.query(Order).offset(skip).limit(limit).all()
    db.close()
    return [{"phone": o.phone, "product": o.product, "quantity": o.quantity, "portion": o.portion, "price": o.price, "address": o.address, "created_at": o.created_at.isoformat()} for o in orders]

def generate_receipt_pdf(order):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)
    pdf.cell(200, 10, "Para Meats - Order Receipt", ln=True, align='C')
    pdf.cell(200, 10, f"Phone: {order.phone}", ln=True)
    pdf.cell(200, 10, f"Product: {order.product}", ln=True)
    pdf.cell(200, 10, f"Quantity: {order.quantity}", ln=True)
    pdf.cell(200, 10, f"Portion: {order.portion}", ln=True)
    pdf.cell(200, 10, f"Price: {order.price}", ln=True)
    pdf.cell(200, 10, f"Address: {order.address}", ln=True)
    pdf.cell(200, 10, f"Date: {order.created_at.strftime('%Y-%m-%d %H:%M')}", ln=True)
    filename = f"receipt_{order.phone}_{order.created_at.strftime('%Y%m%d%H%M%S')}.pdf"
    pdf.output(filename)
    return filename

def send_whatsapp_message(recipient_id, message):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": recipient_id,
        "type": "text",
        "text": {"body": message},
        "recipient_type": "individual"
    }
    try:
        response = requests.post(url, headers=headers, json=payload)
        logger.info(f"📤 Sent: {response.status_code} {response.text}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to send WhatsApp message: {e}")

def send_whatsapp_file(recipient_id, file_path):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}"
    }
    files = {
        'file': (os.path.basename(file_path), open(file_path, 'rb'))
    }
    try:
        response = requests.post(
            url,
            headers=headers,
            files=files,
            data={
                "messaging_product": "whatsapp",
                "to": recipient_id,
                "type": "document",
                "document[filename]": os.path.basename(file_path),
                "document[caption]": "Here is your order receipt."
            }
        )
        logger.info(f"📎 File Sent: {response.status_code} {response.text}")
    except Exception as e:
        logger.error(f"Failed to send file: {e}")

def send_whatsapp_template_button(recipient_id, template_name, buttons):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": recipient_id,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": "Please confirm or cancel your order."},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "confirm_order", "title": "Confirm ✅"}},
                    {"type": "reply", "reply": {"id": "cancel_order", "title": "Cancel ❌"}}
                ]
            }
        }
    }
    try:
        response = requests.post(url, headers=headers, json=payload)
        logger.info(f"🧩 Template Sent: {response.status_code} {response.text}")
    except Exception as e:
        logger.error(f"Failed to send template: {e}")

def send_typing_indicator(recipient_id):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": recipient_id,
        "type": "typing_on"
    }
    try:
        response = requests.post(url, headers=headers, json=payload)
        logger.info(f"💬 Typing indicator sent: {response.status_code} {response.text}")
    except Exception as e:
        logger.error(f"Failed to send typing indicator: {e}")

@app.post("/")
async def receive_message(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        logger.info(f"📥 Incoming data: {data}")

        value = data.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {})
        sender_id = value.get("messages", [{}])[0].get("from") if value.get("messages") else None

        interactive = value.get("messages", [{}])[0].get("interactive")
        if interactive:
            reply_id = interactive.get("button_reply", {}).get("id")
            if reply_id == "cancel_order" and sender_id in pending_orders:
                del pending_orders[sender_id]
                send_whatsapp_message(sender_id, "❌ Your order has been cancelled as requested.")
                return {"status": "cancelled"}
            elif reply_id == "confirm_order" and sender_id:
                send_typing_indicator(sender_id)
                background_tasks.add_task(handle_message, "yes", sender_id)
                return {"status": "confirmed"}

        message = value.get("messages", [{}])[0]
        user_text = message.get("text", {}).get("body", "")
        if sender_id and user_text:
            send_typing_indicator(sender_id)
            background_tasks.add_task(handle_message, user_text, sender_id)
            return {"status": "received"}
        else:
            return {"status": "ignored"}

    except Exception as e:
        logger.error(f"❌ Error in receive_message: {e}")
        return {"status": "error"}

# Simulate thinking delay inside handle_message
def handle_message(user_text, sender_id):
    import time
    time.sleep(random.uniform(1.5, 3.0))  # simulate human-like delay

    try:
        history = session_store.get(sender_id, [])
        history.append({"role": "user", "content": user_text})

        messages = [{"role": "system", "content": "You are a helpful assistant for ."}] + history

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages
        )
        reply = response.choices[0].message.content.strip()

        history.append({"role": "assistant", "content": reply})
        session_store[sender_id] = history[-MAX_HISTORY_LENGTH:]  # trim history

        send_whatsapp_message(sender_id, reply)

    except Exception as e:
        logger.error(f"❌ GPT Error: {e}")
        send_whatsapp_message(sender_id, "⚠️ Sorry, I couldn't process that. Please try again.")
