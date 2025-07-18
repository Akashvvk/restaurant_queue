from flask import Flask, request
import requests
import sqlite3
import os
from dotenv import load_dotenv
import datetime # Import for timestamp

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

# WhatsApp API credentials - Ensure these are set in your .env file
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN") # This is your webhook verification token
API_URL = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"

# In-memory state to track conversation for each user.
# In a production environment, this should be replaced with a persistent
# store like Redis or a dedicated database table for session management.
# Structure: {phone_number: {"role": "customer" | "waiter", "state": "...", "data": {}}}
user_states = {}

def send_message(to, text):
    """
    Sends a text message to a specified WhatsApp number via the WhatsApp Business API.

    Args:
        to (str): The recipient's WhatsApp phone number.
        text (str): The message content to send.

    Returns:
        dict: The JSON response from the WhatsApp API.
    """
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    try:
        response = requests.post(API_URL, json=payload, headers=headers)
        response.raise_for_status() # Raise an HTTPError for bad responses (4xx or 5xx)
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error sending message: {e}")
        return {"error": str(e)}

def init_db():
    """
    Initializes the SQLite database, creating 'users' and 'free_tables' tables
    if they do not already exist.
    """
    conn = sqlite3.connect("users.db") # Using a single database file for both tables
    cursor = conn.cursor()

    # Create 'users' table for customer data
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT NOT NULL,
            name TEXT,
            people_count INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Create 'free_tables' table for waiter-updated free table information
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS free_tables (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_number TEXT NOT NULL UNIQUE, -- UNIQUE to prevent duplicate entries for the same table number
            status TEXT DEFAULT 'free',       -- 'free' or 'occupied' (can be expanded later)
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
    print("Database 'users.db' initialized with 'users' and 'free_tables' tables.")

def save_user_data_to_db(phone_number, name, people_count):
    """
    Saves customer data (phone number, name, people count) to the 'users' table.

    Args:
        phone_number (str): The customer's WhatsApp phone number.
        name (str): The customer's name.
        people_count (int): The number of people in the customer's party.
    """
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO users (phone_number, name, people_count) VALUES (?, ?, ?)",
            (phone_number, name, people_count)
        )
        conn.commit()
        print(f"Saved user data: Phone: {phone_number}, Name: {name}, People: {people_count}")
    except sqlite3.Error as e:
        print(f"Database error saving user data: {e}")
    finally:
        conn.close()

def save_free_table_to_db(table_number):
    """
    Saves or updates a free table entry in the 'free_tables' table.
    If the table number already exists, its status is updated to 'free' and timestamp refreshed.
    Otherwise, a new entry is created.

    Args:
        table_number (str): The table number to mark as free.
    """
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    try:
        # Check if the table number already exists in the database
        cursor.execute("SELECT * FROM free_tables WHERE table_number = ?", (table_number,))
        existing_table = cursor.fetchone()

        if existing_table:
            # If table exists, update its status to 'free' and refresh the timestamp
            cursor.execute(
                "UPDATE free_tables SET status = 'free', timestamp = CURRENT_TIMESTAMP WHERE table_number = ?",
                (table_number,)
            )
            print(f"Updated table {table_number} status to 'free'.")
        else:
            # If table does not exist, insert a new entry
            cursor.execute(
                "INSERT INTO free_tables (table_number, status, timestamp) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (table_number, 'free')
            )
            print(f"Added new free table: {table_number}")
        conn.commit()
    except sqlite3.Error as e:
        print(f"Database error saving free table: {e}")
    finally:
        conn.close()

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Handles incoming WhatsApp messages from the Meta webhook.
    Processes messages based on user conversation state and role (customer or waiter).
    """
    data = request.get_json()
    # print(f"Received webhook data: {data}") # Uncomment for debugging incoming data

    # Ensure the incoming data is from a WhatsApp Business Account message
    if data and data.get("object") == "whatsapp_business_account":
        for entry in data["entry"]:
            for change in entry["changes"]:
                if change["field"] == "messages" and change["value"].get("messages"):
                    msg = change["value"]["messages"][0]
                    sender = msg["from"] # The sender's WhatsApp phone number
                    message_type = msg["type"]

                    # Initialize user state if it doesn't exist for this sender
                    if sender not in user_states:
                        user_states[sender] = {"role": "customer", "state": "initial", "data": {}}

                    user_state = user_states[sender]

                    # Process only text messages for now
                    if message_type == "text":
                        text = msg["text"]["body"].lower().strip()

                        # --- Waiter Flow Logic ---
                        if user_state["state"] == "initial" and text == "waiter":
                            send_message(sender, "Please enter the waiter password.")
                            user_states[sender]["state"] = "awaiting_waiter_password"
                            user_states[sender]["role"] = "waiter" # Set role to waiter
                        elif user_state["state"] == "awaiting_waiter_password" and user_state["role"] == "waiter":
                            if text == "waiter123": # Simple password check (consider more secure methods for production)
                                send_message(sender, "Waiter authenticated. Please enter the table number that is free (e.g., Table 4 or just 4).")
                                user_states[sender]["state"] = "awaiting_free_table_number"
                            else:
                                send_message(sender, "Incorrect password. Please try again or say 'hi' to start as a customer.")
                                # Reset state and role if password is incorrect
                                user_states[sender] = {"role": "customer", "state": "initial", "data": {}}
                        elif user_state["state"] == "awaiting_free_table_number" and user_state["role"] == "waiter":
                            # Extract table number (e.g., "Table 4" or "4")
                            table_number = text.replace("table", "").strip()
                            if table_number.isdigit() or (table_number.startswith("table") and table_number[5:].strip().isdigit()):
                                # Save the free table information to the database
                                save_free_table_to_db(table_number)
                                send_message(sender, f"Table {table_number} marked as free. Thank you!")
                                # Reset state and role after successful operation
                                user_states[sender] = {"role": "customer", "state": "initial", "data": {}}
                            else:
                                send_message(sender, "Invalid table number format. Please enter just the number, e.g., '4' or 'Table 4'.")

                        # --- Customer Flow Logic ---
                        elif user_state["state"] == "initial" and text == "hi":
                            send_message(sender, "Enter your name and how many people are there (e.g., John, 5)")
                            user_states[sender]["state"] = "awaiting_name_people"
                            user_states[sender]["role"] = "customer" # Ensure role is customer
                        elif user_state["state"] == "awaiting_name_people" and user_state["role"] == "customer":
                            try:
                                name, people_count_str = map(str.strip, text.split(","))
                                people_count = int(people_count_str)
                                # Save customer data to the database
                                save_user_data_to_db(sender, name, people_count)
                                send_message(sender, f"Got it! Saved {name} with {people_count} people. You are in the queue.")
                                # Reset state after successful operation
                                user_states[sender] = {"role": "customer", "state": "initial", "data": {}}
                            except ValueError:
                                send_message(sender, "Please provide name and number in format: Name, Number (e.g., John, 5)")
                        else:
                            # Default response for unhandled messages or states
                            send_message(sender, "Please say 'hi' to start as a customer or 'waiter' to access waiter functions.")
                    else:
                        # Inform user about unsupported message types
                        send_message(sender, "I can only process text messages. Please say 'hi' to start.")

    return "EVENT_RECEIVED", 200

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """
    Verifies the webhook with Meta when setting up or refreshing the webhook URL.
    """
    # Check if the hub.verify_token in the request matches your VERIFY_TOKEN
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        # Return the hub.challenge to Meta to complete verification
        return request.args.get("hub.challenge")
    # If tokens do not match, return a 403 Forbidden status
    return "Verification failed", 403

if __name__ == "__main__":
    # Initialize the database when the Flask application starts
    init_db()
    # Run the Flask app in debug mode (set debug=False for production)
    app.run(debug=True)
