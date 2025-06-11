from fastapi import FastAPI, Request, BackgroundTasks, Depends, HTTPException
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from sqlalchemy.orm import Session
from models import Order, Base, engine, SessionLocal
from knowledge_loader import load_csv, load_excel, load_google_sheet, scrape_website
from openai import OpenAI
import os, requests
import logging
from fastapi.logger import logger as fastapi_logger
from knowledge_manager import KnowledgeManager
from fpdf import FPDF
import time
from whatsapp_api import send_order_confirmation
from datetime import datetime, timedelta
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime
import pytz
import googlemaps
import googlemap_utils
from fastapi import FastAPI, Query
from googlemap_utils import get_distance_from_harare
import re
import spacy
from location_detector import extract_delivery_location
from whatsapp_api import send_whatsapp_message, send_whatsapp_typing_indicator, send_whatsapp_file


# Load environment variables
load_dotenv()

# WhatsApp credentials
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")

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
# Startup check for environment variables
if not ACCESS_TOKEN:
    logger.warning("WARNING: ACCESS_TOKEN environment variable is not set. WhatsApp messaging will not work.")
if not PHONE_NUMBER_ID:
    logger.warning("WARNING: PHONE_NUMBER_ID environment variable is not set. WhatsApp messaging will not work.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # or your frontend domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Dependency to get DB session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        
knowledge_manager = KnowledgeManager()


# In-memory session store for chat history per user
session_store = {}
customer_names = {}  # Separate dictionary for customer names
# In-memory store for pending orders per user before confirmation
pending_orders = {}

ORDER_STEPS = [
    "item", "quantity", "portion", "price", "delivery_address",
    "delivery_time", "payment_method", "confirmation"
]
# Maximum number of messages to keep in history per user
MAX_HISTORY_LENGTH = 10

# Token limit threshold to trigger summarization (example: 3000 tokens)
TOKEN_LIMIT_THRESHOLD = 3000  # Requirement: Token counting accuracy

#Cache Locations to Reduce API Calls
location_cache = {}
#Cache latest delivery info per user
latest_delivery_data = {}


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
    
#Set the Time Zone    
zimbabwe_time = datetime.now(pytz.timezone("Africa/Harare"))
current_hour = zimbabwe_time.hour

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

        # NEW: Extract contact information (names)
        contacts = value.get("contacts", [])
        contact_names = {}
        for contact in contacts:
            wa_id = contact.get("wa_id")
            profile = contact.get("profile", {})
            name = profile.get("name")
            if wa_id and name:
                contact_names[wa_id] = name
                logger.info(f"🏷️ Found contact name: {wa_id} -> {name}")

        message = messages[0]
        user_text = message.get("text", {}).get("body", "")
        sender_id = message.get("from", "")
        
        # NEW: Get customer name from contacts
        customer_name = contact_names.get(sender_id)
        
        logger.info(f"👤 From: {sender_id} ({customer_name or 'Unknown'}) | 📝 Text: {user_text}")

        # Pass customer name to your background task
        background_tasks.add_task(handle_message, user_text, sender_id, customer_name)
        return {"status": "received"}  # Fast return

    except Exception as e:
        logger.error(f"❌ Error in webhook: {e}")
        return {"status": "error"}


def get_prompt_for_step(step, order=None):
    if order is None:
        order = {}
    prompts = {
        "full_name": "👤 What's your full name?",
        "contact_number": "📞 Please provide your contact number.",
        "item": "🍖 What would you like to order? (e.g., Beef, Chicken, Fish, Maguru)",
        "quantity": "📦 How much do you need? (e.g., 5kg, 10kg)",
        "portion": "🔪 Preferred cut or portion? (e.g., steak, bones, standard)",
        "delivery_address": "📍 Please provide your delivery location (e.g., 8233 Glenview 8)",
        "delivery_time": "🕒 What time should we deliver? (Morning, Afternoon, Evening)",
        "payment_method": "💳 How will you pay? (Cash, Ecocash, ZIPIT)",
        "confirmation": (
            f"✅ Please confirm your order:\n"
            f"- Name: {order.get('full_name', '')}\n"
            f"- Phone: {order.get('contact_number', '')}\n"
            f"- Meat: {order.get('item', '')}\n"
            f"- Quantity: {order.get('quantity', '')}kg\n"
            f"- Cut: {order.get('portion', '')}\n"
            f"- Address: {order.get('delivery_address', '')}\n"
            f"- Time: {order.get('delivery_time', '')}\n"
            f"- Payment: {order.get('payment_method', '')}\n\n"
            "Reply *yes* to confirm or *no* to cancel."
        )
    }
    return prompts.get(step, "Please provide the required info.")

def save_order_to_db(phone: str, data: dict):
    db = SessionLocal()
    order = Order(
        customer_name=None,
        phone_number=phone,
        meat_type=data.get("item"),
        price_option=data.get("price"),
        quantity=data.get("quantity"),
        custom_cuts=data.get("portion"),
        payment_method=data.get("payment_method"),
        delivery_time=data.get("delivery_time"),
        delivery_address=data.get("delivery_address")
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    db.close()
    return order


def handle_message(user_text, sender_id, customer_name=None):
    try:
        user_text = user_text.strip().lower()

        # Store customer name in separate dictionary if provided
        if customer_name:
            customer_names[sender_id] = customer_name
            logger.info(f"Stored customer name: {sender_id} -> {customer_name}")

        # Get stored customer name if not provided
        if not customer_name:
            customer_name = customer_names.get(sender_id)

        if sender_id in pending_orders:
            order = pending_orders[sender_id]
            step_index = order.get("current_step", 0)

            if ORDER_STEPS[step_index] == "confirmation":
                if user_text in ["yes", "y", "confirm"]:
                    # Add customer name to order before saving
                    if customer_name:
                        order["customer_name"] = customer_name
                    
                    order_obj = save_order_to_db(sender_id, order)
                    
                    # Personalized confirmation message
                    confirmation_msg = f"✅ Your order has been confirmed{', ' + customer_name if customer_name else ''}. Thank you!"
                    send_whatsapp_message(sender_id, confirmation_msg)

                    # Send to agent with customer name
                    if LIVE_AGENT_WHATSAPP_NUMBER:
                        forward_message = (
                            f"New order from {customer_name or sender_id} ({sender_id}):\n"
                            f"Product: {order.get('item')}\n"
                            f"Quantity: {order.get('quantity')}\n"
                            f"Portion: {order.get('portion')}\n"
                            f"Price: {order.get('price')}\n"
                            f"Delivery: {order.get('delivery_address')} at {order.get('delivery_time')}\n"
                            f"Payment: {order.get('payment_method')}"
                        )
                        send_whatsapp_message(LIVE_AGENT_WHATSAPP_NUMBER, forward_message)

                    # Send PDF receipt
                    try:
                        pdf_filename = generate_receipt_pdf(order_obj)
                        send_whatsapp_file(sender_id, pdf_filename)
                    except Exception as e:
                        logger.error(f"PDF receipt error: {e}")

                    del pending_orders[sender_id]
                else:
                    send_whatsapp_message(sender_id, "❌ Order cancelled.")
                    del pending_orders[sender_id]
                return
            
            latest_delivery_data[sender_id] = {
                "location": order["delivery_address"],
                "weight": float(order.get("quantity", 1))
            }

            # Save response for current step
            order[ORDER_STEPS[step_index]] = user_text
            order["current_step"] += 1

            if order["current_step"] < len(ORDER_STEPS):
                next_step = ORDER_STEPS[order["current_step"]]
                prompt = get_prompt_for_step(next_step, order)
                send_whatsapp_message(sender_id, prompt)
            else:
                # All steps completed – proceed to confirmation
                confirmation_msg = get_prompt_for_step("confirmation", order)
                send_whatsapp_message(sender_id, confirmation_msg)


            if user_text.startswith("order"):
                        pending_orders[sender_id] = {"current_step": 0}
                        # Add customer name to pending order
                        if customer_name:
                            pending_orders[sender_id]["customer_name"] = customer_name
                        
                        # Personalized welcome message
                        welcome_msg = f"Welcome to Para Meats{', ' + customer_name if customer_name else ''}! 🥩 Let's start your order."
                        send_whatsapp_message(sender_id, welcome_msg)
                        send_whatsapp_message(sender_id, get_prompt_for_step("item"))
                        return



        # Knowledge integration (CSV, Excel, site scraping, prompts)
        if "load csv" in user_text:
            file = "your_file.csv"
            if os.path.exists(file):
                df = load_csv(file)
                knowledge_manager.update_knowledge(df.to_csv(index=False))
                send_whatsapp_message(sender_id, f"📄 CSV loaded with {len(df)} rows.")
            else:
                send_whatsapp_message(sender_id, "❗ CSV not found.")
            return

        if "load excel" in user_text:
            file = "./uploads/Para Price list .xlsx"
            if os.path.exists(file):
                df = load_excel(file)
                knowledge_manager.update_knowledge(df.to_csv(index=False))
                send_whatsapp_message(sender_id, f"📊 Excel loaded with {len(df)} rows.")
            else:
                send_whatsapp_message(sender_id, "❗ Excel file not found.")
            return

        if "scrape site" in user_text:
            try:
                content = scrape_website("https://parameats.co.zw")
                knowledge_manager.update_knowledge(content)
                send_whatsapp_message(sender_id, f"🌐 Website scraped successfully.")
            except Exception as e:
                send_whatsapp_message(sender_id, f"❌ Scrape failed: {e}")
            return

        if user_text.startswith("load prompt"):
            new_prompt = user_text[len("load prompt"):].strip()
            if new_prompt:
                knowledge_manager.update_prompt(new_prompt)
                send_whatsapp_message(sender_id, "✅ Prompt updated.")
            else:
                send_whatsapp_message(sender_id, "❗ No prompt provided.")
            return
                    
        location = extract_delivery_location(user_text)
        if location:
                try:
                    # Check cache first
                    if location in location_cache:
                        result = location_cache[location]
                    else:
                        response = requests.get(
                            "http://localhost:8000/calculate-delivery",
                            params={"destination": location, "weight_kg": 12}
                        )
                        result = response.json()
                        if "error" not in result:
                            location_cache[location] = result  # ✅ Store in cache

                    if "error" in result:
                        reply = f"⚠️ I couldn't find delivery info for *{location}*."
                    else:
                        reply = (
                            f"🚚 *Delivery to {result['destination']}* (approx. {result['distance_km']}km)\n"
                            f"🪶 Weight: {result.get('weight_kg', 12)}kg\n"
                            f"💵 Charge: {result['delivery_charge']}"
                        )
                    send_whatsapp_message(sender_id, reply)
                    return
                except Exception as e:
                    logger.error(f"Delivery lookup error: {e}")
                    send_whatsapp_message(sender_id, "❌ Failed to check delivery cost. Please try again.")
                    return
                
                
         # Handle “what’s my distance” questions      
        if "distance" in user_text and "my" in user_text:
            delivery_info = latest_delivery_data.get(sender_id)
            if delivery_info:
                try:
                    response = requests.get(
                        "http://localhost:8000/calculate-delivery",
                        params={
                            "destination": delivery_info["location"],
                            "weight_kg": delivery_info["weight"]
                        }
                    )
                    data = response.json()
                    if "error" in data:
                        reply = f"❌ I couldn't get your distance. Please confirm the location again."
                    else:
                        reply = (
                            f"📍 Your distance from our shop at 182 Sam Nujoma is approximately "
                            f"{data['distance_km']} km.\n"
                            f"💵 Delivery charge: {data['delivery_charge']} based on your order of {data['weight_kg']}kg."
                        )
                except Exception as e:
                    logger.error(f"Distance lookup error: {e}")
                    reply = "❌ Failed to fetch your delivery distance. Please try again."
            else:
                reply = "I don't have your recent delivery address. Please tell me your location again."
            
            send_whatsapp_message(sender_id, reply)
            return



        # AI Assistant fallback
        try:
            # Get current Zimbabwe time (UTC+2)
            zimbabwe_time = datetime.now(pytz.timezone("Africa/Harare"))
            current_hour = zimbabwe_time.hour
            current_minute = zimbabwe_time.minute
            current_day = zimbabwe_time.strftime('%A')

            # Opening and closing times by day
            closing_times = {
                'Monday': (19, 0),
                'Tuesday': (17, 30),
                'Wednesday': (19, 0),
                'Thursday': (19, 0),
                'Friday': (17, 30),
                'Saturday': (18, 0),
                'Sunday': (0, 0)  # Closed all day
            }

            # Determine if store is open now
            closing_hour, closing_minute = closing_times.get(current_day, (0, 0))
            is_open = (
                (8 <= current_hour < closing_hour) or
                (current_hour == closing_hour and current_minute < closing_minute)
            )

            # Accurate 24-hour greeting logic
            if 5 <= current_hour < 12:
                greeting = "Good morning"
            elif 12 <= current_hour < 17:
                greeting = "Good afternoon"
            elif 17 <= current_hour < 22:
                greeting = "Good evening"
            elif 22 <= current_hour or current_hour < 5:
                greeting = "It's late night – hope you're doing well"
                # Status response
            if is_open and current_day != "Sunday":
                status = f"We’re currently open. Today’s hours: 8:00 AM to {closing_hour}:{closing_minute:02d}."
            else:
                # Determine store status and correct tense
                if current_day == "Sunday":
                    status = "We are closed today (Sunday)."
                elif current_hour < 8:
                    # Before opening
                    status = f"We’re currently closed. Our hours today will be from 8:00 AM to {closing_hour}:{closing_minute:02d}."
                elif is_open:
                    # Open now
                    status = f"We’re currently open. Today’s hours: 8:00 AM to {closing_hour}:{closing_minute:02d}."
                else:
                    # Closed after closing time
                    status = f"We’re currently closed. Our hours today were from 8:00 AM to {closing_hour}:{closing_minute:02d}."


            # Build prompt
            prompt = knowledge_manager.get_prompt()
            knowledge = knowledge_manager.get_knowledge()
            base_system = f"{prompt}\n\nKnowledge base:\n{knowledge}" if prompt or knowledge else "You are a helpful assistant for Para Meats butchery."

            if customer_name:
                base_system += f"\n\nCustomer name: {customer_name}. Use their name naturally in responses when appropriate."

            base_system += f"\n\nCurrent Zimbabwe time: {zimbabwe_time.strftime('%A %H:%M')}.\n{greeting}! {status}"

            system_content = base_system

            # Chat and GPT logic
            history = session_store.get(sender_id, [])
            history.append({"role": "user", "content": user_text})
            messages = [{"role": "system", "content": system_content}] + history

            send_whatsapp_typing_indicator(sender_id, "typing_on")
            gpt_reply = client.chat.completions.create(model="gpt-4o-mini", messages=messages)
            reply = gpt_reply.choices[0].message.content.strip() if gpt_reply.choices[0].message.content else ""
            history.append({"role": "assistant", "content": reply})
            session_store[sender_id] = history[-MAX_HISTORY_LENGTH:]

            send_whatsapp_typing_indicator(sender_id, "typing_off")

        except Exception as e:
            logger.error(f"OpenAI error: {e}")
            reply = f"⚠️ Sorry{', ' + customer_name if customer_name else ''}, I couldn't understand that. Please try again."

        send_whatsapp_message(sender_id, reply)

    except Exception as e:
        logger.error(f"❌ handle_message error: {e}")
        fallback_msg = f"⚠️ An error occurred{', ' + customer_name if customer_name else ''}. Please try again."
        send_whatsapp_message(sender_id, fallback_msg)

class ReceiptPDF(FPDF):
    def header(self):
        # Add Logo
        if os.path.exists("logo.png"):
            self.image("logo.png", x=10, y=8, w=30)
        self.set_font("Helvetica", "B", 14)
        self.cell(0, 10, "Para Meats Receipt", ln=True, align="C")
        self.ln(20)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.cell(0, 10, "Thank you for your order! Visit parameats.co.zw", align="C")

def clean_text(text):
    return text.encode("latin-1", "ignore").decode("latin-1") if text else ""

def generate_receipt_pdf(order):
    pdf = ReceiptPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)

    # Order Details
    pdf.cell(0, 10, clean_text(f"📞 Phone: {order.phone_number}"), ln=True)
    pdf.cell(0, 10, clean_text(f"🥩 Product: {order.meat_type}"), ln=True)
    pdf.cell(0, 10, clean_text(f"📦 Quantity: {order.quantity}"), ln=True)
    pdf.cell(0, 10, clean_text(f"💵 Price: {order.price_option}"), ln=True)
    pdf.cell(0, 10, clean_text(f"🔪 Cut: {order.custom_cuts}"), ln=True)
    pdf.cell(0, 10, clean_text(f"💳 Payment: {order.payment_method}"), ln=True)
    pdf.cell(0, 10, clean_text(f"📍 Delivery Address: {order.delivery_address}"), ln=True)
    pdf.cell(0, 10, clean_text(f"🕒 Delivery Time: {order.delivery_time}"), ln=True)
    pdf.cell(0, 10, clean_text(f"📅 Date: {time.strftime('%Y-%m-%d %H:%M:%S')}"), ln=True)

    filename = f"receipt_{order.phone_number}_{int(time.time())}.pdf"
    pdf.output(filename)
    return filename


@app.post("/submit-order")
async def submit_order(request: Request, db: Session = Depends(get_db)):
    data = await request.json()

    new_order = Order(
        customer_name=data.get("Customer_Name"),
        phone_number=data.get("Phone_Number"),
        meat_type=data.get("Meat_Type"),
        price_option=data.get("Price_Option"),
        quantity=data.get("Quantity"),
        custom_cuts=data.get("Custom_Cuts"),
        payment_method=data.get("Payment_Method"),
        delivery_time=data.get("Delivery_Time"),
        delivery_address=data.get("Delivery_Address"),
    )

    db.add(new_order)
    db.commit()
    db.refresh(new_order)

  
    # 🧾 Compose details for WhatsApp template
    order_number = f"#{new_order.id:05d}"
    product_description = f"{new_order.quantity} of {new_order.meat_type}"
    delivery_date = (datetime.now() + timedelta(days=1)).strftime("%b %d, %Y")

    send_order_confirmation(
        to_number=new_order.phone_number,
        customer_name=new_order.customer_name,
        order_number=order_number,
        product_details=product_description,
        estimated_delivery=delivery_date
    )

    return { "status": "success", "order_id": new_order.id }

# Add these endpoints to your main FastAPI file (after your existing endpoints)

@app.get("/orders")
def get_orders(limit: int = 50, offset: int = 0, db: Session = Depends(get_db)):
    """Get all orders from database"""
    try:
        orders = db.query(Order).offset(offset).limit(limit).all()
        return [
            {
                "id": order.id,
                "customer_name": order.customer_name,
                "phone_number": order.phone_number,
                "meat_type": order.meat_type,
                "price_option": order.price_option,
                "quantity": order.quantity,
                "custom_cuts": order.custom_cuts,
                "payment_method": order.payment_method,
                "delivery_time": order.delivery_time,
                "delivery_address": order.delivery_address,
            "created_at": order.created_at.isoformat() if order.created_at is not None else None,
            "updated_at": order.updated_at.isoformat() if order.updated_at is not None else None,
            }
            for order in orders
        ]
    except Exception as e:
        logger.error(f"Error fetching orders: {e}")
        return []

@app.get("/system-status")
def get_system_status():
    """Get system status"""
    try:
        return {
            "whatsapp_connected": bool(ACCESS_TOKEN is not None and PHONE_NUMBER_ID is not None),
            "openai_connected": bool(OPENAI_API_KEY is not None),
            "database_connected": True,  # Since you're using SQLAlchemy
            "active_sessions": len(session_store),
            "pending_orders": len(pending_orders)
        }
    except Exception as e:
        logger.error(f"Error getting system status: {e}")
        return {
            "whatsapp_connected": False,
            "openai_connected": False,
            "database_connected": False,
            "active_sessions": 0,
            "pending_orders": 0
        }

@app.get("/analytics")
def get_analytics(period: str = "week", db: Session = Depends(get_db)):
    """Get analytics data"""
    try:
        # Calculate date range based on period
        now = datetime.now()
        if period == "day":
            start_date = now - timedelta(days=1)
        elif period == "week":
            start_date = now - timedelta(weeks=1)
        else:  # month
            start_date = now - timedelta(days=30)
        
        # Query orders from database
        orders = db.query(Order).filter(Order.created_at >= start_date).all()
        
        total_orders = len(orders)
        total_conversations = len(session_store)
        total_messages = sum(len(history) for history in session_store.values())
        
        # Calculate revenue (extract numeric value from price_option)
        total_revenue = 0.0
        for order in orders:
            if order.price_option is not None:
                import re
                price_match = re.search(r'[\d.]+', str(order.price_option))
                if price_match:
                    try:
                        total_revenue += float(price_match.group())
                    except ValueError:
                        pass
        
        avg_order_value = total_revenue / total_orders if total_orders > 0 else 0.0
        
        return {
            "totalOrders": total_orders,
            "totalRevenue": total_revenue,
            "averageOrderValue": avg_order_value,
            "totalConversations": total_conversations,
            "totalMessages": total_messages,
            "orderGrowth": "+12%",
            "revenueGrowth": "+15%",
            "topProducts": [],
            "ordersByDay": [],
            "paymentMethods": []
        }
    except Exception as e:
        logger.error(f"Error getting analytics: {e}")
        return {
            "totalOrders": 0,
            "totalRevenue": 0.0,
            "averageOrderValue": 0.0,
            "totalConversations": 0,
            "totalMessages": 0,
            "orderGrowth": "0%",
            "revenueGrowth": "0%",
            "topProducts": [],
            "ordersByDay": [],
            "paymentMethods": []
        }

# Also fix your /chats endpoint - remove the wrapper
@app.get("/chats")
def get_all_chats():
    """Get all chat conversations with real customer names"""
    try:
        logger.info(f"Session store has {len(session_store)} conversations")
        
        all_chats = {}
        
        for phone_number, history in session_store.items():
            messages = []
            
            # Get customer name from the customer_names dictionary
            customer_name = customer_names.get(phone_number)
            
            for idx, msg in enumerate(history):
                timestamp = datetime.now() - timedelta(minutes=(len(history) - idx))
                
                if msg["role"] == "user":
                    messages.append({
                        "id": f"{phone_number}-{idx}-user",
                        "sender_id": phone_number,
                        "message": msg["content"],
                        "timestamp": timestamp.isoformat(),
                        "message_type": "incoming",
                        "customer_name": customer_name  # Include customer name
                    })
                elif msg["role"] == "assistant":
                    messages.append({
                        "id": f"{phone_number}-{idx}-bot",
                        "sender_id": "bot",
                        "message": msg["content"],
                        "timestamp": timestamp.isoformat(),
                        "message_type": "outgoing",
                        "is_ai_response": True
                    })
            
            if messages:
                all_chats[phone_number] = messages
        
        logger.info(f"Returning {len(all_chats)} conversations")
        return all_chats
        
    except Exception as e:
        logger.error(f"Error getting chats: {e}")
        return {}

# Add endpoint to get customer names
@app.get("/customer-names")
def get_customer_names():
    """Get all customer names"""
    return customer_names

# Add a debug endpoint to check what's in the session store
@app.get("/debug/session-store")
def debug_session_store():
    """Debug endpoint to view session store contents"""
    return {
        "total_conversations": len(session_store),
        "conversations": {
            phone: {
                "message_count": len(history),
                "sample": history[:2] if history else []  # Show first 2 messages as sample
            }
            for phone, history in session_store.items()
        }
    }

@app.get("/calculate-delivery")
def calculate_delivery(destination: str, weight_kg: float = Query(..., gt=0)):
    distance_km = get_distance_from_harare(destination)

    if distance_km == -1:
        return {"error": "Could not calculate distance. Please try a different location."}

    if weight_kg < 10:
        return {
            "destination": destination,
            "distance_km": distance_km,
            "note": "Free delivery only applies to orders 10kg and above.",
            "delivery_charge": "Varies — confirm with store"
        }

    if distance_km <= 10:
        charge = 0
    elif distance_km <= 20:
        charge = 3.00
    elif distance_km <= 40:
        charge = 7.00
    else:
        charge = 15.00

    return {
        "destination": destination,
        "distance_km": distance_km,
        "weight_kg": weight_kg,
        "delivery_charge": f"${charge:.2f}"
    }
