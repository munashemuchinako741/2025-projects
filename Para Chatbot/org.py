from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from models import Order, Base, engine, SessionLocal
from knowledge_loader import load_csv, load_excel, load_google_sheet, scrape_website
from openai import OpenAI
import os, requests
import logging
from fastapi.logger import logger as fastapi_logger
from knowledge_manager import KnowledgeManager

# Load environment variables
load_dotenv()

# WhatsApp credentials
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Live agent contact details (WhatsApp or phone number)
LIVE_AGENT_WHATSAPP_NUMBER = os.getenv("LIVE_AGENT_WHATSAPP_NUMBER")
LIVE_AGENT_PHONE_NUMBER = os.getenv("LIVE_AGENT_PHONE_NUMBER")

client = OpenAI(api_key=OPENAI_API_KEY)
# Initialize DB
Base.metadata.create_all(bind=engine)

# Configure logging to use FastAPI's logger (which integrates with uvicorn)
fastapi_logger.setLevel(logging.INFO)
logger = fastapi_logger

app = FastAPI()

knowledge_manager = KnowledgeManager()

# In-memory session store for chat history per user
session_store = {}

# In-memory store for pending orders per user before confirmation
pending_orders = {}

# Order steps to collect
ORDER_STEPS = [
    "item",
    "quantity",
    "portion",
    "price",
    "confirmation"
]

# Maximum number of messages to keep in history per user
MAX_HISTORY_LENGTH = 10

# Token limit threshold to trigger summarization (example: 3000 tokens)
TOKEN_LIMIT_THRESHOLD = 3000  # Requirement: Token counting accuracy

# Import tiktoken for token counting
try:
    import tiktoken
    # Use explicit encoding for GPT-4 models
    tokenizer = tiktoken.get_encoding("cl100k_base")
except ImportError:
    tokenizer = None

def count_tokens(messages):
    if not tokenizer:
        return 0
    num_tokens = 0
    for message in messages:
        for key, value in message.items():
            num_tokens += len(tokenizer.encode(value))
    return num_tokens  # Requirement: Token counting accuracy

import asyncio

async def summarize_messages(messages):
    summary_prompt = "Summarize the following conversation briefly, keeping important details:\n\n"
    conversation_text = ""
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        conversation_text += f"{role}: {content}\n"
    summary_prompt += conversation_text

    try:
        response = client.chat.completions.create(
            model="gpt-4.1-nano",
            messages=[
                {"role": "system", "content": "You are a helpful assistant that summarizes conversations."},
                {"role": "user", "content": summary_prompt}
            ]
        )
        summary = response.choices[0].message.content
        if summary is not None:
            summary = summary.strip()
        return summary  # Requirement: Summarization triggers correctly
    except Exception as e:
        logger.error(f"Summarization error: {e}")
        return None

@app.get("/")
async def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        if challenge is not None:
            return int(challenge)
        else:
            return JSONResponse(content={"error": "Missing challenge"}, status_code=400)
    return JSONResponse(content={"error": "Verification failed"}, status_code=403)

@app.post("/")
async def receive_message(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        logger.info(f"📥 Incoming data: {data}")

        value = data.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {})
        messages = value.get("messages", [])
        if not messages:
            return {"status": "ignored"}

        message = messages[0]
        user_text = message.get("text", {}).get("body", "")
        sender_id = message.get("from", "")

        logger.info(f"👤 From: {sender_id} | 📝 Text: {user_text}")

        background_tasks.add_task(handle_message, user_text, sender_id)
        return {"status": "received"}  # Fast return

    except Exception as e:
        logger.error(f"❌ Error in webhook: {e}")
        return {"status": "error"}

def handle_message(user_text, sender_id):
    try:
        # Check if user has a pending order in progress
        if sender_id in pending_orders:
            pending_order = pending_orders[sender_id]
            current_step = pending_order.get("current_step", 0)

            if current_step < len(ORDER_STEPS) - 1:
                # Save user response for current step
                step_name = ORDER_STEPS[current_step]
                pending_order[step_name] = user_text.strip()
                pending_order["current_step"] = current_step + 1

                # Ask for next step
                next_step = ORDER_STEPS[pending_order["current_step"]]
                if next_step == "confirmation":
                    # Summarize order for confirmation
                    summary = (
                        f"Thank you! Your order is for {pending_order.get('quantity', '')} of "
                        f"{pending_order.get('fresh_or_frozen', '')} {pending_order.get('item', '')} "
                        f"with {pending_order.get('portion', '')} portion at price {pending_order.get('price', '')}. "
                        "Would you like to confirm this order? Please reply 'yes' or 'no'."
                    )
                    response = summary
                else:
                    prompts = {
                        "item": "Which type of product would you like? Beef, Chicken, Fish, Pork, or Lamb?",
                        "quantity": "How many kilograms do you need?",
                        "portion": "Do you want a specific portion?",
                        "address": "Please provide your full residential address?",
                        "price": "Please provide the price per kg or total price."
                    }
                    response = prompts.get(next_step, "Please provide the information.")

                pending_orders[sender_id] = pending_order
                send_whatsapp_message(sender_id, response)
                return

            else:
                # Final confirmation step
                if user_text.lower() in ["yes", "confirm", "y"]:
                    # Save order to DB
                    db = SessionLocal()
                    order = Order(
                        phone=sender_id,
                        product=pending_order.get("item", ""),
                        quantity=pending_order.get("quantity", ""),
                    )
                    db.add(order)
                    db.commit()
                    db.close()
                    response = "✅ Your order has been confirmed and saved. Thank you!"
                    # Forward order to live agent via WhatsApp if configured
                    if LIVE_AGENT_WHATSAPP_NUMBER:
                        forward_message = (
                            f"New order from {sender_id}:\n"
                            f"Product: {pending_order.get('item', '')}\n"
                            f"Quantity: {pending_order.get('quantity', '')}\n"
                            f"Fresh/Frozen: {pending_order.get('fresh_or_frozen', '')}\n"
                            f"Portion: {pending_order.get('portion', '')}\n"
                            f"Price: {pending_order.get('price', '')}"
                        )
                        try:
                            send_whatsapp_message(LIVE_AGENT_WHATSAPP_NUMBER, forward_message)
                            logger.info(f"Order forwarded to live agent {LIVE_AGENT_WHATSAPP_NUMBER}")
                        except Exception as e:
                            logger.error(f"Failed to forward order to live agent: {e}")
                else:
                    response = "❌ Your order has been cancelled."
                del pending_orders[sender_id]
                send_whatsapp_message(sender_id, response)
                return

        if user_text.lower().startswith("order"):
            # Start new order process
            pending_orders[sender_id] = {"current_step": 0}
            response = "Welcome to Para Meats! Let's start your order."
            send_whatsapp_message(sender_id, response)
            # Ask first question
            first_question = "Which type of fish would you like? Tilapia, Bream, or Mackerel?"
            send_whatsapp_message(sender_id, first_question)
            return
        elif "load csv" in user_text.lower():
            if os.path.exists("your_file.csv"):
                df = load_csv("your_file.csv")
                knowledge_manager.update_knowledge(df.to_csv(index=False))
                response = f"📄 CSV loaded with {len(df)} rows."
            else:
                response = "❗ CSV file not found. Please upload it first."

        elif "load csv" in user_text.lower():
            if os.path.exists("your_file.csv"):
                df = load_csv("your_file.csv")
                knowledge_manager.update_knowledge(df.to_csv(index=False))
                response = f"📄 CSV loaded with {len(df)} rows."
            else:
                response = "❗ CSV file not found. Please upload it first."

        elif "load excel" in user_text.lower():
            if os.path.exists("./uploads/Copy of PRICE LIST new(1).xlsx"):
                df = load_excel("./uploads/Copy of PRICE LIST new(1).xlsx")
                knowledge_manager.update_knowledge(df.to_csv(index=False))
                response = f"📊 Excel loaded with {len(df)} rows."
            else:
                response = "❗ Excel file not found. Please upload it first."

        elif "load google sheet" in user_text.lower():
            try:
                df = load_google_sheet("YOUR_GOOGLE_SHEET_ID", "Sheet1")
                knowledge_manager.update_knowledge(df.to_csv(index=False))
                response = f"📄 Google Sheet loaded with {len(df)} rows."
            except Exception as e:
                response = f"❌ Failed to load Google Sheet: {e}"

        elif "scrape site" in user_text.lower():
            try:
                content = scrape_website("https://parameats.co.zw")
                knowledge_manager.update_knowledge(content)
                response = f"🌐 Website content loaded ({len(content)} characters)."
            except Exception as e:
                response = f"❌ Failed to scrape website: {e}"

        elif user_text.lower().startswith("load prompt"):
            # Extract prompt text after the command
            new_prompt = user_text[len("load prompt"):].strip()
            if new_prompt:
                knowledge_manager.update_prompt(new_prompt)
                response = "✅ Prompt loaded successfully."
            else:
                response = "❗ Please provide a prompt text after 'load prompt' command."

        elif user_text.lower().startswith("load prompt file"):
            filepath = user_text[len("load prompt file"):].strip()
            from utils import extract_text_from_file
            if filepath:
                content = extract_text_from_file(filepath)
                if content:
                    knowledge_manager.update_prompt(content)
                    response = f"✅ Prompt loaded successfully from file: {filepath}"
                else:
                    response = f"❌ Failed to load prompt from file: {filepath}. Unsupported format or read error."
            else:
                response = "❗ Please provide a file path after 'load prompt file' command."

        else:
            try:
                prompt = knowledge_manager.get_prompt()
                knowledge = knowledge_manager.get_knowledge()
                system_content = ""
                if prompt and knowledge:
                    system_content = f"{prompt}\n\nKnowledge base:\n{knowledge}"
                elif prompt:
                    system_content = prompt
                elif knowledge:
                    system_content = f"Knowledge base:\n{knowledge}"
                else:
                    system_content = "You are a helpful assistant."

                # Retrieve chat history for user
                history = session_store.get(sender_id, [])
                # Append current user message to history
                history.append({"role": "user", "content": user_text})
                # Prepare messages with system content and history
                messages = [{"role": "system", "content": system_content}] + history
                # Call OpenAI API
                gpt_reply = client.chat.completions.create(
                    model="gpt-4.1-nano",
                    messages=messages
                )
                content = gpt_reply.choices[0].message.content
                reply = content.strip() if content is not None else ""
                # Append assistant reply to history
                history.append({"role": "assistant", "content": reply})
                # Limit history length
                if len(history) > MAX_HISTORY_LENGTH:
                    history = history[-MAX_HISTORY_LENGTH:]
                # Update session store
                session_store[sender_id] = history
            except Exception as e:
                logger.error(f"OpenAI error: {e}")
                response = "⚠️ Sorry, I couldn't understand that. Please try again."

        send_whatsapp_message(sender_id, reply)
    except Exception as e:
        logger.error(f"❌ Background handler error: {e}")

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
        "text": {"body": message}
    }
    response = requests.post(url, headers=headers, json=payload)
    logger.info(f"📤 Sent: {response.status_code} {response.text}")

