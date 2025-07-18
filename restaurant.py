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
    Initializes the SQLite database, creating 'users' (queue) and 'tables' tables
    if they do not already exist. Populates initial table data.
    """
    conn = sqlite3.connect("users.db") # Using a single database file for both tables
    cursor = conn.cursor()

    # Create 'users' table for customer queue data
    # Added 'timestamp' for FCFS ordering
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT NOT NULL UNIQUE, -- Phone number should be unique in queue
            name TEXT,
            people_count INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Create 'tables' table for restaurant table configuration and status
    # Added 'capacity', 'status', 'occupied_by_user_id', 'occupied_timestamp'
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tables (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_number TEXT NOT NULL UNIQUE,
            capacity INTEGER NOT NULL,
            status TEXT DEFAULT 'free', -- 'free' or 'occupied'
            occupied_by_user_id INTEGER, -- NULL if free, FK to users.id if occupied
            occupied_timestamp DATETIME, -- NULL if free, timestamp when occupied
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, -- When this table record was last updated/created
            FOREIGN KEY (occupied_by_user_id) REFERENCES users(id) ON DELETE SET NULL
        )
    """)
    conn.commit()

    # Populate initial table data if tables are not already present
    initial_tables_config = [
        ("T1", 2), ("T2", 2), ("T3", 2), ("T4", 2), # 4 x 2-seaters
        ("T5", 4), ("T6", 4), ("T7", 4), ("T8", 4), # 4 x 4-seaters
        ("T9", 6), ("T10", 6) # 2 x 6-seaters
    ]

    for table_num, capacity in initial_tables_config:
        # Use INSERT OR IGNORE to prevent adding duplicates if run multiple times
        cursor.execute("INSERT OR IGNORE INTO tables (table_number, capacity, status) VALUES (?, ?, 'free')", (table_num, capacity))
    conn.commit()
    conn.close()
    print("Database 'users.db' initialized with 'users' and 'tables' tables and initial table data.")

def save_user_data_to_db(phone_number, name, people_count):
    """
    Saves customer data (phone number, name, people count) to the 'users' table.
    If the phone number already exists, it updates the existing entry.
    Returns the user's ID.

    Args:
        phone_number (str): The customer's WhatsApp phone number.
        name (str): The customer's name.
        people_count (int): The number of people in the customer's party.

    Returns:
        int: The ID of the inserted or updated user.
    """
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    user_id = None
    try:
        # Check if user already exists
        cursor.execute("SELECT id FROM users WHERE phone_number = ?", (phone_number,))
        existing_user = cursor.fetchone()

        if existing_user:
            user_id = existing_user[0]
            # Update existing user's details and timestamp
            cursor.execute(
                "UPDATE users SET name = ?, people_count = ?, timestamp = CURRENT_TIMESTAMP WHERE id = ?",
                (name, people_count, user_id)
            )
            print(f"Updated user data: ID: {user_id}, Phone: {phone_number}, Name: {name}, People: {people_count}")
        else:
            # Insert new user
            cursor.execute(
                "INSERT INTO users (phone_number, name, people_count, timestamp) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (phone_number, name, people_count)
            )
            user_id = cursor.lastrowid
            print(f"Saved new user data: ID: {user_id}, Phone: {phone_number}, Name: {name}, People: {people_count}")
        conn.commit()
    except sqlite3.Error as e:
        print(f"Database error saving user data: {e}")
    finally:
        conn.close()
    return user_id

def update_table_status_to_free(table_number):
    """
    Marks a specific table as 'free' in the 'tables' table.
    Resets occupied_by_user_id and occupied_timestamp.

    Args:
        table_number (str): The table number to mark as free.
    Returns:
        bool: True if table was found and updated, False otherwise.
    """
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE tables SET status = 'free', occupied_by_user_id = NULL, occupied_timestamp = NULL, timestamp = CURRENT_TIMESTAMP WHERE table_number = ?",
            (table_number,)
        )
        conn.commit()
        if cursor.rowcount > 0:
            print(f"Table {table_number} marked as free.")
            return True
        else:
            print(f"Table {table_number} not found or no change.")
            return False
    except sqlite3.Error as e:
        print(f"Database error marking table free: {e}")
        return False
    finally:
        conn.close()

def get_waiting_customers():
    """
    Fetches all customers currently in the queue, ordered by timestamp (oldest first).
    Returns a list of dictionaries.
    """
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    customers = []
    try:
        cursor.execute("SELECT id, phone_number, name, people_count, timestamp FROM users ORDER BY timestamp ASC")
        rows = cursor.fetchall()
        for row in rows:
            customers.append({
                "id": row[0],
                "phone_number": row[1],
                "name": row[2],
                "people_count": row[3],
                "timestamp": row[4]
            })
    except sqlite3.Error as e:
        print(f"Database error fetching waiting customers: {e}")
    finally:
        conn.close()
    return customers

def get_free_tables():
    """
    Fetches all tables currently marked as 'free', ordered by capacity (smallest first).
    Returns a list of dictionaries.
    """
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    tables = []
    try:
        cursor.execute("SELECT id, table_number, capacity FROM tables WHERE status = 'free' ORDER BY capacity ASC")
        rows = cursor.fetchall()
        for row in rows:
            tables.append({
                "id": row[0],
                "table_number": row[1],
                "capacity": row[2]
            })
    except sqlite3.Error as e:
        print(f"Database error fetching free tables: {e}")
    finally:
        conn.close()
    return tables

def seat_customer(customer_id, table_id, table_number, customer_phone_number, customer_name):
    """
    Assigns a customer to a table, updates database, and notifies the customer.

    Args:
        customer_id (int): ID of the customer to seat.
        table_id (int): ID of the table to assign.
        table_number (str): The actual table number (e.g., "T5").
        customer_phone_number (str): The customer's WhatsApp phone number.
        customer_name (str): The customer's name.
    """
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    try:
        # 1. Update table status
        cursor.execute(
            "UPDATE tables SET status = 'occupied', occupied_by_user_id = ?, occupied_timestamp = CURRENT_TIMESTAMP, timestamp = CURRENT_TIMESTAMP WHERE id = ?",
            (customer_id, table_id)
        )
        # 2. Remove customer from queue
        cursor.execute("DELETE FROM users WHERE id = ?", (customer_id,))
        conn.commit()

        # 3. Notify customer
        send_message(customer_phone_number, f"Great news, {customer_name}! Your table {table_number} is ready. Please proceed to your table.")
        print(f"Seated customer {customer_name} (ID: {customer_id}) at table {table_number} (ID: {table_id}).")
    except sqlite3.Error as e:
        print(f"Database error seating customer: {e}")
    finally:
        conn.close()

def attempt_seating_allocation():
    """
    Attempts to seat waiting customers at free tables based on smart allocation logic:
    1. Prioritize minimum wasted seats.
    2. Then, prioritize first-come, first-served.
    3. Allows skipping customers if a better match for a later customer is found.
    """
    print("Attempting seating allocation...")
    waiting_customers = get_waiting_customers()
    free_tables = get_free_tables()
    
    # Store potential allocations: (wasted_seats, customer_timestamp, customer, table)
    potential_allocations = []

    for customer in waiting_customers:
        best_match_for_customer = None
        min_wasted_seats_for_customer = float('inf')

        for table in free_tables:
            if table["capacity"] >= customer["people_count"]:
                wasted_seats = table["capacity"] - customer["people_count"]
                
                # If this table is a better fit (fewer wasted seats)
                if wasted_seats < min_wasted_seats_for_customer:
                    min_wasted_seats_for_customer = wasted_seats
                    best_match_for_customer = (wasted_seats, customer["timestamp"], customer, table)
                # If same wasted seats, prioritize by customer's wait time (FCFS)
                elif wasted_seats == min_wasted_seats_for_customer:
                    # Compare timestamps to ensure FCFS for equally efficient matches
                    if customer["timestamp"] < best_match_for_customer[1]:
                        best_match_for_customer = (wasted_seats, customer["timestamp"], customer, table)
        
        if best_match_for_customer:
            potential_allocations.append(best_match_for_customer)

    # Sort potential allocations:
    # 1. By wasted seats (ascending)
    # 2. By customer timestamp (ascending - FCFS)
    sorted_allocations = sorted(potential_allocations, key=lambda x: (x[0], x[1]))

    # Execute allocations
    allocated_something = False
    seated_customer_ids = set()
    occupied_table_ids = set()

    for wasted_seats, customer_timestamp, customer, table in sorted_allocations:
        # Ensure customer hasn't been seated by a previous allocation in this run
        # and table hasn't been occupied by a previous allocation in this run
        if customer["id"] not in seated_customer_ids and table["id"] not in occupied_table_ids:
            seat_customer(customer["id"], table["id"], table["table_number"], customer["phone_number"], customer["name"])
            seated_customer_ids.add(customer["id"])
            occupied_table_ids.add(table["id"])
            allocated_something = True
            # Since a table and customer are now used, we should re-evaluate the remaining.
            # For simplicity, we can break and re-run the entire allocation if something was allocated.
            # In a very high-throughput system, a more complex graph matching might be needed.
            break # Break and re-run to ensure fresh state for next allocation

    if allocated_something:
        # If an allocation happened, re-run the process to see if more can be seated
        attempt_seating_allocation()
    else:
        print("No further allocations possible at this time.")


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
                                send_message(sender, "Waiter authenticated. Please enter the table number that is now free (e.g., T4 or just 4).")
                                user_states[sender]["state"] = "awaiting_free_table_number"
                            else:
                                send_message(sender, "Incorrect password. Please try again or say 'hi' to start as a customer.")
                                # Reset state and role if password is incorrect
                                user_states[sender] = {"role": "customer", "state": "initial", "data": {}}
                        elif user_state["state"] == "awaiting_free_table_number" and user_state["role"] == "waiter":
                            # Normalize table number input (e.g., "table 4" -> "T4", "4" -> "T4")
                            table_input = text.replace("table", "").strip()
                            if table_input.isdigit():
                                table_number = f"T{table_input}"
                            else:
                                table_number = table_input.upper() # Assume T1, T2 etc.

                            if update_table_status_to_free(table_number):
                                send_message(sender, f"Table {table_number} marked as free. Attempting to seat waiting customers...")
                                attempt_seating_allocation() # Trigger seating attempt after table is free
                            else:
                                send_message(sender, f"Could not find or update table {table_number}. Please ensure the table number is correct (e.g., T1, T5, T10).")
                            # Reset state and role after operation
                            user_states[sender] = {"role": "customer", "state": "initial", "data": {}}

                        # --- Customer Flow Logic ---
                        elif user_state["state"] == "initial" and text == "hi":
                            send_message(sender, "Enter your name and how many people are there (e.g., John, 5)")
                            user_states[sender]["state"] = "awaiting_name_people"
                            user_states[sender]["role"] = "customer" # Ensure role is customer
                        elif user_state["state"] == "awaiting_name_people" and user_state["role"] == "customer":
                            try:
                                name, people_count_str = map(str.strip, text.split(","))
                                people_count = int(people_count_str)
                                
                                if people_count <= 0:
                                    send_message(sender, "Number of people must be a positive integer. Please try again.")
                                elif people_count > 6: # Max capacity of largest table
                                    send_message(sender, "We currently don't have tables for more than 6 people. Please try with a smaller group.")
                                else:
                                    # Save customer data to the database
                                    user_id = save_user_data_to_db(sender, name, people_count)
                                    if user_id:
                                        send_message(sender, f"Got it! {name} with {people_count} people. You are in the queue. We will notify you when a table is ready.")
                                        # Reset state after successful operation
                                        user_states[sender] = {"role": "customer", "state": "initial", "data": {}}
                                        attempt_seating_allocation() # Trigger seating attempt after new customer joins
                                    else:
                                        send_message(sender, "There was an issue adding you to the queue. Please try again.")
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
