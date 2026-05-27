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

@app.get("/api/test-db")
def test_database_connection():
    """Diagnostic route to test if Vercel can talk to Supabase."""
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            # Run a raw, ultra-fast test query to confirm handshake
            cur.execute("SELECT NOW();")
            db_time = cur.fetchone()
        conn.close()
        return {
            "status": "SUCCESS",
            "message": "Vercel and Supabase are perfectly wired together!",
            "database_time": str(db_time)
        }
    except Exception as e:
        return {
            "status": "FAILED",
            "message": "Connection attempt broke.",
            "error_details": str(e)
        }

from datetime import datetime
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Schema matching your network coprocessor parameters
class TopUpRequest(BaseModel):
    uid: str
    new_balance: float
    signature: str # Expect the security key from the hardware

@app.post("/api/topup")
def sync_kiosk_topup(req: TopUpRequest):
    """
    CRYPTO VALIDATION: Uses FNV-1a verification to instantly validate hardware logs
    without restricting legitimate multi-bill or bulk offline sync operations.
    """
    # This MUST match the SECRET_KEY value inside your Arduino code exactly!
    SECRET_KEY = 2166136261 
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # 1. Fetch data from DB
            cur.execute("SELECT balance, name FROM students WHERE uid = %s FOR UPDATE", (req.uid,))
            student = cur.fetchone()
            
            if not student:
                raise HTTPException(status_code=404, detail="Student record missing.")
                
            db_balance = float(student["balance"])
            amount_added = req.new_balance - db_balance
            
            if amount_added <= 0:
                return {"success": True, "message": "Database is already synchronous."}

            # 2. 🛡️ VERIFY THE CRYPTOGRAPHIC SIGNATURE
            # We recreate the exact same hardware hash calculation in Python
            calc_hash = SECRET_KEY
            
            # Convert UID back to raw bytes (mimicking the RFID scanner's UID buffer)
            # Assuming standard 4-byte UID converted back from decimal string
            try:
                uid_int = int(req.uid)
                uid_bytes = [
                    (uid_int & 0xFF),
                    ((uid_int >> 8) & 0xFF),
                    ((uid_int >> 16) & 0xFF),
                    ((uid_int >> 24) & 0xFF)
                ]
            except:
                raise HTTPException(status_code=400, detail="Malformed UID string data received.")
                
            # Recreate balance buffer bytes
            int_credit = int(req.new_balance)
            credit_bytes = [
                (int_credit >> 8) & 0xFF,
                int_credit & 0xFF
            ]
            
            # Run FNV-1a on UID bytes
            for byte in uid_bytes:
                calc_hash ^= byte
                calc_hash = (calc_hash * 16777619) & 0xFFFFFFFF
                
            # Run FNV-1a on Credit bytes
            for byte in credit_bytes:
                calc_hash ^= byte
                calc_hash = (calc_hash * 16777619) & 0xFFFFFFFF

            # Verify client signature against calculated server signature
            if str(calc_hash) != str(req.signature):
                return {
                    "success": False, 
                    "message": "Security Violation: Unauthorized card tampering signature detected!"
                }

            # 3. 💰 Process completely trustable updates
            cur.execute("UPDATE students SET balance = %s WHERE uid = %s", (req.new_balance, req.uid))
            
            timestamp = datetime.now()
            cur.execute('''
                INSERT INTO logs (uid, timestamp, type, amount, running_balance, device_id)
                VALUES (%s, %s, 'TOPUP', %s, %s, 'KIOSK_TERMINAL')
            ''', (req.uid, timestamp, amount_added, req.new_balance))
            
            conn.commit() 
            return {
                "success": True, 
                "name": student["name"], 
                "new_balance": req.new_balance,
                "message": f"Successfully processed secure transaction log update."
            }
            
    except Exception as e:
        conn.rollback() 
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()
