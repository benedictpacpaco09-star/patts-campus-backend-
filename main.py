import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor

app = FastAPI(
    title="PATTS Campus Ecosystem Cloud Server", 
    description="Serverless API running on Vercel linked with Supabase.",
    version="4.0"
)

# Fetch database transaction pooler URL from environment variables
DATABASE_URL = os.environ.get("DATABASE_URL")

def get_db_connection():
    """Opens a single, short-lived connection to Supabase."""
    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="Database URL missing from environment variables.")
    try:
        # RealDictCursor allows us to read rows as dictionary items like student["balance"]
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database handshake failed: {str(e)}")

# --- DATA MODELS ---
class TransactionRequest(BaseModel):
    uid: str
    amount: float
    device_id: str = "CASHIER_01"
    offline_timestamp: str = None 

# --- ENDPOINTS ---

@app.get("/api/scan/{uid}")
def scan_card(uid: str):
    """Instant balance/status lookups for the ESP32 terminal."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT uid, name, student_no, balance, status FROM students WHERE uid = %s", (uid,))
            student = cur.fetchone()
            
            if not student:
                return {"status": "NOT_FOUND", "message": "Card not registered."}
            
            if student["status"] == 1:
                return {"status": "LOCKED", "name": student["name"], "message": "Card suspended."}
            elif student["status"] == 2:
                return {"status": "STOLEN", "name": student["name"], "message": "Card flagged stolen."}
                
            return {
                "status": "ACTIVE",
                "uid": student["uid"],
                "name": student["name"],
                "student_no": student["student_no"],
                "balance": float(student["balance"])
            }
    finally:
        conn.close() # CRUCIAL: Closes socket immediately so Supabase doesn't choke

@app.post("/api/deduct")
def deduct_funds(req: TransactionRequest):
    """Processes instant deductions from the cashier terminal."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Row locking mechanism for security against multi-tap exploits
            cur.execute("SELECT balance, status, name FROM students WHERE uid = %s FOR UPDATE", (req.uid,))
            student = cur.fetchone()
            
            if not student:
                raise HTTPException(status_code=404, detail="Student record missing.")
            if student["status"] != 0:
                return {"success": False, "message": "Card status is restricted."}
                
            current_balance = float(student["balance"])
            if current_balance < req.amount:
                return {"success": False, "message": "Insufficient balance.", "current_balance": current_balance}
                
            new_balance = current_balance - req.amount
            
            # Handle timestamps
            if req.offline_timestamp:
                try:
                    timestamp = datetime.strptime(req.offline_timestamp, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    timestamp = datetime.now()
            else:
                timestamp = datetime.now()
            
            # Execute standard data updates
            cur.execute("UPDATE students SET balance = %s WHERE uid = %s", (new_balance, req.uid))
            cur.execute('''
                INSERT INTO logs (uid, timestamp, type, amount, running_balance, device_id)
                VALUES (%s, %s, 'PURCHASE', %s, %s, %s)
            ''', (req.uid, timestamp, req.amount, new_balance, req.device_id))
            
            conn.commit() # Save changes
            return {"success": True, "name": student["name"], "new_balance": new_balance}
            
    except Exception as e:
        conn.rollback() # Undo any broken queries safely
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close() # CRUCIAL: Release the connection pool slot
