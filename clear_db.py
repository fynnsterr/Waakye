import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "waakye_data.db")

def clear_orders():
    with sqlite3.connect(DB_PATH) as db:
        cursor = db.cursor()
        cursor.execute("DELETE FROM orders")
        cursor.execute("DELETE FROM customers")
        db.commit()
        print("Cleared all orders and customers from database")

if __name__ == "__main__":
    clear_orders()
