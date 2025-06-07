import openai
import logging
import time
from config import OPENAI_API_KEY
from utils import send_whatsapp_message

# Setup
openai.api_key = OPENAI_API_KEY
logging.basicConfig(level=logging.INFO)

def handle_message(sender: str, text: str):
    text_lower = text.lower()
    logging.info(f"Received message from {sender}: {text}")

    try:
        # Intent matching
        if any(word in text_lower for word in ["order", "buy", "purchase"]):
            reply = "Sure! What product would you like to order?"
        elif any(word in text_lower for word in ["menu", "list", "options"]):
            reply = "We offer:\n- Beef\n- Chicken\n- Pork\nReply with your choice."
        elif any(word in text_lower for word in ["hi", "hello", "hey"]):
            reply = "Hi there! Welcome to Para Meats. How can I help you today?"
        else:
            reply = get_gpt_response(text)

        send_whatsapp_message(sender, reply)

    except Exception as e:
        logging.error(f"Error handling message from {sender}: {e}")
        send_whatsapp_message(sender, "Oops! Something went wrong. Please try again shortly.")

def get_gpt_response(prompt: str, retries: int = 2) -> str:
    for attempt in range(retries):
        try:
            response = openai.ChatCompletion.create(
                model="gpt-4.1-nano",
                messages=[{"role": "user", "content": prompt}]
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logging.warning(f"GPT error (attempt {attempt + 1}): {e}")
            time.sleep(1)
    return "Sorry, I couldn't process your request right now."
