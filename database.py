import asyncio
from datetime import datetime
import logging
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple

import os
LOG = logging.getLogger(__name__)
DB_FILE = os.getenv("DATABASE_PATH", "database.db")


class CapacityExceededError(Exception):
    """Raised when an atomic booking attempts to exceed maximum allowed seats."""
    pass


def set_db_file(path: str) -> None:
    """Sets the database file path for connection pooling and isolated tests."""
    global DB_FILE
    DB_FILE = path


@contextmanager
def get_db_connection(immediate: bool = False):
    """Ստեղծում է կապ SQLite տվյալների բազայի հետ՝ WAL ռեժիմով և բարձր concurrency-ի կարգավորումներով"""
    conn = sqlite3.connect(DB_FILE, timeout=30.0, isolation_level=None if immediate else "")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    if immediate:
        conn.execute("BEGIN IMMEDIATE;")
    try:
        yield conn
        if immediate:
            conn.execute("COMMIT;")
    except Exception:
        if immediate:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Ինիցիալիզացնում է users, bookings և drivers աղյուսակները"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # users աղյուսակի ստեղծում
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    language TEXT DEFAULT 'hy',
                    full_name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    address TEXT DEFAULT '',
                    created_at TEXT,
                    updated_at TEXT
                )
            """)

            # drivers աղյուսակի ստեղծում
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS drivers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    car_model TEXT NOT NULL,
                    car_number TEXT NOT NULL,
                    is_active INTEGER DEFAULT 1
                )
            """)

            # bookings աղյուսակի ստեղծում
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS bookings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    username TEXT,
                    route TEXT,
                    address TEXT,
                    date TEXT,
                    phone TEXT,
                    seats INTEGER DEFAULT 1,
                    notes TEXT DEFAULT '',
                    status TEXT DEFAULT 'active',
                    driver_id TEXT,
                    departure_time TEXT DEFAULT '08:00',
                    created_at TEXT
                )
            """)

            # bookings աղյուսակի միգրացիաներ
            migrations = {
                "seats": "INTEGER DEFAULT 1",
                "notes": "TEXT DEFAULT ''",
                "status": "TEXT DEFAULT 'active'",
                "driver_id": "TEXT",
                "departure_time": "TEXT DEFAULT '08:00'",
                "created_at": "TEXT",
            }
            for column, definition in migrations.items():
                try:
                    cursor.execute(f"ALTER TABLE bookings ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError:
                    pass

            cursor.execute("UPDATE bookings SET status = 'active' WHERE status IS NULL")
            cursor.execute("UPDATE bookings SET notes = '' WHERE notes IS NULL")
            cursor.execute(
                "UPDATE bookings SET created_at = ? WHERE created_at IS NULL",
                (datetime.now().isoformat(),),
            )

            # business_chat_states աղյուսակի ստեղծում (Telegram Business cooldown & operator takeover)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS business_chat_states (
                    chat_id INTEGER PRIMARY KEY,
                    last_admin_time REAL DEFAULT 0,
                    last_welcome_time REAL DEFAULT 0
                )
            """)

            conn.commit()
    except sqlite3.Error as err:
        LOG.error(f"Database initialization error: {err}")


# Ավտոմատ ինիցիալիզացիա մոդուլը ներբեռնելիս
init_db()


# ==========================================
# DRIVERS UTILITIES & QUERIES
# ==========================================

def normalize_phone(phone: str) -> str:
    """Compare Armenian phone numbers independently of spaces, +, or leading 0."""
    digits = "".join(char for char in (phone or "") if char.isdigit())
    if digits.startswith("374"):
        return digits[-8:]
    return digits[-8:]


def get_all_drivers() -> Dict[str, Dict[str, Any]]:
    """Վերադարձնում է բոլոր վարորդներին"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, phone, car_model, car_number, is_active FROM drivers")
            rows = cursor.fetchall()
            return {
                str(row["id"]): {
                    "name": row["name"],
                    "phone": row["phone"],
                    "car_model": row["car_model"],
                    "car_number": row["car_number"],
                    "is_active": bool(row["is_active"]),
                }
                for row in rows
            }
    except sqlite3.Error as err:
        LOG.error(f"Database SELECT query error in get_all_drivers: {err}")
        return {}


def get_driver(driver_id: str) -> Optional[Dict[str, Any]]:
    """Ստանում է կոնկրետ վարորդի տվյալները ID-ով"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name, phone, car_model, car_number, is_active FROM drivers WHERE id = ?",
                (driver_id,),
            )
            row = cursor.fetchone()
            if row:
                return {
                    "name": row["name"],
                    "phone": row["phone"],
                    "car_model": row["car_model"],
                    "car_number": row["car_number"],
                    "is_active": bool(row["is_active"]),
                }
            return None
    except sqlite3.Error as err:
        LOG.error(f"Database query error in get_driver: {err}")
        return None


def get_driver_by_phone(phone: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Գտնում է ակտիվ վարորդին ըստ հեռախոսահամարի"""
    wanted = normalize_phone(phone)
    drivers = get_all_drivers()
    for driver_id, driver in drivers.items():
        if normalize_phone(driver.get("phone", "")) == wanted and driver.get("is_active"):
            return driver_id, driver
    return None


def add_driver(
    driver_data_or_name: Dict[str, Any] | str,
    phone: Optional[str] = None,
    car_model: Optional[str] = None,
    car_number: Optional[str] = None,
    is_active: bool = True,
) -> str:
    """Ավելացնում է նոր վարորդ"""
    if isinstance(driver_data_or_name, dict):
        name = driver_data_or_name.get("name", "")
        phone = driver_data_or_name.get("phone", "")
        car_model = driver_data_or_name.get("car_model", "")
        car_number = driver_data_or_name.get("car_number", "")
        is_active = driver_data_or_name.get("is_active", True)
    else:
        name = driver_data_or_name

    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO drivers (name, phone, car_model, car_number, is_active)
                VALUES (?, ?, ?, ?, ?)
                """,
                (name, phone, car_model, car_number, 1 if is_active else 0),
            )
            conn.commit()
            return str(cursor.lastrowid)
    except sqlite3.Error as err:
        LOG.error(f"Database INSERT error in add_driver: {err}")
        return ""


def update_driver(driver_id: str, **kwargs) -> bool:
    """Փոփոխում է վարորդի տվյալները"""
    if not kwargs:
        return False

    fields, values = [], []
    for key, value in kwargs.items():
        if value is not None:
            if key == "is_active":
                value = 1 if value else 0
            fields.append(f"{key} = ?")
            values.append(value)

    if not fields:
        return False

    values.append(driver_id)
    query = f"UPDATE drivers SET {', '.join(fields)} WHERE id = ?"

    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, values)
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as err:
        LOG.error(f"Database UPDATE error in update_driver: {err}")
        return False


def format_drivers_list() -> str:
    """Պատրաստում է ձևավորված տեքստ Telegram-ում ցուցադրելու համար"""
    drivers = get_all_drivers()
    if not drivers:
        return "❌ Վարորդների ցուցակը դատարկ է:"

    text = "🚘 **Վարորդների Ցուցակ**\n\n"
    for d_id, info in drivers.items():
        status = "🟢 Ակտիվ" if info.get("is_active") else "🔴 Ոչ ակտիվ"
        text += (
            f"🆔 `{d_id}` | **{info['name']}** ({status})\n"
            f"📞 {info['phone']}\n"
            f"🚘 {info['car_model']} [{info['car_number']}]\n"
            f"-------------------------------\n"
        )
    return text


# ==========================================
# BOOKINGS QUERIES
# ==========================================

def save_booking(
    user_id: int,
    username: str,
    route: str,
    address: str,
    date: str,
    phone: str,
    seats: int,
    notes: str = "",
    departure_time: str = "08:00",
    max_capacity: Optional[int] = None,
) -> int:
    """Պահպանում է նոր ամրագրումը բազայում (ատոմիկ ստուգմամբ և BEGIN IMMEDIATE տրանզակցիայով)"""
    use_immediate = max_capacity is not None
    with get_db_connection(immediate=use_immediate) as conn:
        cursor = conn.cursor()
        if max_capacity is not None:
            cursor.execute(
                """
                SELECT COALESCE(SUM(seats), 0) FROM bookings
                WHERE date = ? AND route = ? AND status = 'active'
                """,
                (date, route),
            )
            current_count = cursor.fetchone()[0]
            if current_count + seats > max_capacity:
                raise CapacityExceededError(
                    f"Capacity exceeded: {current_count} booked, {seats} requested, max {max_capacity}"
                )

        cursor.execute(
            """
            INSERT INTO bookings
            (user_id, username, route, address, date, phone, seats, notes, status, departure_time, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                user_id,
                username,
                route,
                address,
                date,
                phone,
                seats,
                notes,
                departure_time,
                datetime.now().isoformat(),
            ),
        )
        booking_id = cursor.lastrowid
        if not use_immediate:
            conn.commit()
        return booking_id


def get_monthly_booking_counts(year: int, month: int, route: Optional[str] = None) -> Dict[str, int]:
    """Վերադարձնում է տվյալ ամսվա բոլոր օրերի զբաղված տեղերի քանակը մեկ SQL հարցմամբ (N+1 query optimization)"""
    date_pattern = f"%-{month:02d}-{year}"
    with get_db_connection() as conn:
        cursor = conn.cursor()
        if route:
            cursor.execute(
                """
                SELECT date, COALESCE(SUM(seats), 0) FROM bookings
                WHERE date LIKE ? AND route = ? AND status = 'active'
                GROUP BY date
                """,
                (date_pattern, route),
            )
        else:
            cursor.execute(
                """
                SELECT date, COALESCE(SUM(seats), 0) FROM bookings
                WHERE date LIKE ? AND status = 'active'
                GROUP BY date
                """,
                (date_pattern,),
            )
        return {row[0]: row[1] for row in cursor.fetchall()}


def add_booking(booking_data: dict) -> int:
    """Լրացուցիչ ֆունկցիա մենեջերի պանելի համար՝ ըստ բառարանի (dict) գրանցելու պատվեր"""
    return save_booking(
        user_id=booking_data.get('user_id', 0),
        username=booking_data.get('username', 'Մենեջեր'),
        route=booking_data.get('route', ''),
        address=booking_data.get('address', ''),
        date=booking_data.get('date', ''),
        phone=booking_data.get('phone', ''),
        seats=int(booking_data.get('seats', 1)),
        notes=booking_data.get('notes', ''),
        departure_time=booking_data.get('departure_time', '08:00')
    )


def get_booking_count(date_str: str, route_str: str) -> int:
    """Վերադարձնում է ակտիվ տեղերի ընդհանուր քանակը ըստ ամսաթվի և երթուղու"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COALESCE(SUM(seats), 0) FROM bookings
            WHERE date = ? AND route = ? AND status = 'active'
            """,
            (date_str, route_str),
        )
        return cursor.fetchone()[0]


def get_user_bookings(user_id: int) -> List[sqlite3.Row]:
    """Վերադարձնում է օգտատիրոջ բոլոր ամրագրումները"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM bookings WHERE user_id = ? ORDER BY id DESC", (user_id,)
        )
        return cursor.fetchall()


def get_booking(booking_id: int) -> Optional[sqlite3.Row]:
    """Ստանում է կոնկրետ ամրագրումն ըստ ID-ի"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        return cursor.fetchone()


def cancel_booking(booking_id: int, user_id: int) -> bool:
    """Չեղարկում է ամրագրումը"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE bookings SET status = 'cancelled' WHERE id = ? AND user_id = ? AND status = 'active'",
            (booking_id, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def get_bookings_for_driver(driver_id: str, date_str: Optional[str] = None) -> List[sqlite3.Row]:
    """Վերադարձնում է կոնկրետ վարորդին կցված ակտիվ ամրագրումները"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        query = "SELECT * FROM bookings WHERE driver_id = ? AND status = 'active'"
        params = [str(driver_id)]
        if date_str:
            query += " AND date = ?"
            params.append(date_str)
        query += " ORDER BY date, address, id"
        cursor.execute(query, params)
        return cursor.fetchall()


def get_unassigned_bookings(date_str: str, route: str) -> List[sqlite3.Row]:
    """Վերադարձնում է չկցված (անվարորդ) ակտիվ ամրագրումները"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM bookings WHERE date = ? AND route = ? AND status = 'active'
            AND (driver_id IS NULL OR driver_id = '') ORDER BY id
            """,
            (date_str, route),
        )
        return cursor.fetchall()


def assign_booking_driver(booking_id: int, driver_id: str) -> bool:
    """Կցում է վարորդին ամրագրմանը"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE bookings SET driver_id = ? WHERE id = ? AND status = 'active'",
            (str(driver_id), booking_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def unassign_booking_driver(booking_id: int) -> bool:
    """Հեռացնում է վարորդի կցումը պատվերից"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE bookings SET driver_id = NULL WHERE id = ?",
            (booking_id,),
        )
        conn.commit()
        return cursor.rowcount > 0


def reassign_booking_driver(booking_id: int, driver_id: Optional[str]) -> bool:
    """Փոխում կամ կցում է վարորդին պատվերին (եթե driver_id-ն դատարկ է՝ հեռացնում է)"""
    if not driver_id:
        return unassign_booking_driver(booking_id)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE bookings SET driver_id = ? WHERE id = ?",
            (str(driver_id), booking_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def update_booking(booking_id: int, **kwargs) -> bool:
    """Թարմացնում է պատվերի կամայական դաշտերը (ադմինիստրատիվ իրավասություն)"""
    allowed_fields = {
        "route", "date", "departure_time", "seats", "address",
        "username", "phone", "notes", "status", "driver_id"
    }
    fields, values = [], []
    for k, v in kwargs.items():
        if k in allowed_fields:
            fields.append(f"{k} = ?")
            values.append(v)

    if not fields:
        return False

    values.append(booking_id)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE bookings SET {', '.join(fields)} WHERE id = ?",
            values
        )
        conn.commit()
        return cursor.rowcount > 0


def cancel_booking_admin(booking_id: int) -> bool:
    """Չեղարկում է պատվերը մենեջերի կողմից առանց ժամանակային և user_id սահմանափակումների"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE bookings SET status = 'cancelled' WHERE id = ?",
            (booking_id,)
        )
        conn.commit()
        return cursor.rowcount > 0


def get_bookings_by_date(date_str: str) -> List[sqlite3.Row]:
    """Վերադարձնում է տվյալ ամսաթվի բոլոր ոչ չեղարկված պատվերները՝ վարորդի տվյալներով"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT b.*, d.name as driver_name, d.car_model, d.car_number, d.phone as driver_phone
            FROM bookings b
            LEFT JOIN drivers d ON b.driver_id = d.id
            WHERE b.date = ? AND b.status != 'cancelled'
            ORDER BY b.departure_time ASC, b.id ASC
        """, (date_str,))
        return cursor.fetchall()


def get_driver_occupancy_for_date(driver_id: str, date_str: str) -> int:
    """Հաշվարկում է կոնկրետ վարորդին կցված պատվերների տեղերի ընդհանուր գումարը տվյալ ամսաթվի համար"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COALESCE(SUM(seats), 0)
            FROM bookings
            WHERE driver_id = ? AND date = ? AND status = 'active'
        """, (str(driver_id), date_str))
        row = cursor.fetchone()
        return row[0] if row else 0


def get_recent_bookings(limit: int = 30) -> List[sqlite3.Row]:
    """Վերադարձնում է վերջին պատվերները մենեջերի դիտման և խմբագրման համար"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT b.*, d.name as driver_name, d.car_model, d.car_number
            FROM bookings b
            LEFT JOIN drivers d ON b.driver_id = d.id
            ORDER BY b.id DESC
            LIMIT ?
        """, (limit,))
        return cursor.fetchall()


def get_all_active_dates():
    """Վերադարձնում է բազայում առկա բոլոր այն ամսաթվերը, որոնք ունեն ամրագրումներ"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT date 
            FROM bookings 
            WHERE status != 'cancelled' 
            ORDER BY date ASC
        """)
        dates = [row[0] for row in cursor.fetchall()]
        return dates


def get_daily_stats(date):
    """Վերադարձնում է տվյալ օրվա կցված և չկցված մարդկանց/տեղերի քանակը"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                SUM(CASE WHEN driver_id IS NOT NULL AND driver_id != '' THEN seats ELSE 0 END) as assigned_seats,
                SUM(CASE WHEN driver_id IS NULL OR driver_id = '' THEN seats ELSE 0 END) as unassigned_seats
            FROM bookings 
            WHERE date = ? AND status != 'cancelled'
        """, (date,))
        row = cursor.fetchone()
        
        assigned = row[0] if row[0] else 0
        unassigned = row[1] if row[1] else 0
        return assigned, unassigned


def get_unassigned_bookings_by_date(date):
    """Վերադարձնում է տվյալ օրվա բոլոր ՉԿՑՎԱԾ պատվերները"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM bookings 
            WHERE date = ? AND (driver_id IS NULL OR driver_id = '') AND status != 'cancelled'
            ORDER BY id ASC
        """, (date,))
        bookings = [dict(row) for row in cursor.fetchall()]
        return bookings


# ==========================================
# USERS PERSISTENCE (MULTILINGUAL PROFILE)
# ==========================================

def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    """Վերադարձնում է օգտատիրոջ պրոֆիլը ըստ Telegram ID-ի"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    except sqlite3.Error as err:
        LOG.error(f"Database query error in get_user: {err}")
        return None


def save_or_update_user(
    user_id: int,
    language: str = "hy",
    full_name: str = "",
    phone: str = "",
    address: str = ""
) -> None:
    """Պահպանում կամ թարմացնում է օգտատիրոջ պրոֆիլը (հիշում է օգտատիրոջը հաջորդ այցերի համար)"""
    now_iso = datetime.now().isoformat()
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO users (id, language, full_name, phone, address, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    language = excluded.language,
                    full_name = CASE WHEN excluded.full_name != '' THEN excluded.full_name ELSE users.full_name END,
                    phone = CASE WHEN excluded.phone != '' THEN excluded.phone ELSE users.phone END,
                    address = CASE WHEN excluded.address != '' THEN excluded.address ELSE users.address END,
                    updated_at = excluded.updated_at
            """, (user_id, language, full_name, phone, address, now_iso, now_iso))
            conn.commit()
    except sqlite3.Error as err:
        LOG.error(f"Database error in save_or_update_user: {err}")


def update_user_field(user_id: int, field: str, value: Any) -> bool:
    """Թարմացնում է օգտատիրոջ կոնկրետ դաշտը (լեզու, անուն, հեռախոս կամ հասցե)"""
    allowed = {"language", "full_name", "phone", "address"}
    if field not in allowed:
        return False
    now_iso = datetime.now().isoformat()
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE users SET {field} = ?, updated_at = ? WHERE id = ?",
                (value, now_iso, user_id)
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as err:
        LOG.error(f"Database error in update_user_field: {err}")
        return False


# ==========================================
# ASYNCHRONOUS NON-BLOCKING WRAPPERS
# ==========================================

async def async_get_user(user_id: int) -> Optional[Dict[str, Any]]:
    return await asyncio.to_thread(get_user, user_id)

async def async_save_or_update_user(
    user_id: int,
    language: str = "hy",
    full_name: str = "",
    phone: str = "",
    address: str = ""
) -> None:
    return await asyncio.to_thread(save_or_update_user, user_id, language, full_name, phone, address)

async def async_update_user_field(user_id: int, field: str, value: Any) -> bool:
    return await asyncio.to_thread(update_user_field, user_id, field, value)

async def async_save_booking(*args, **kwargs) -> int:
    return await asyncio.to_thread(save_booking, *args, **kwargs)

async def async_get_booking(booking_id: int) -> Optional[sqlite3.Row]:
    return await asyncio.to_thread(get_booking, booking_id)

async def async_get_booking_count(date_str: str, route_str: str) -> int:
    return await asyncio.to_thread(get_booking_count, date_str, route_str)

async def async_get_user_bookings(user_id: int) -> List[sqlite3.Row]:
    return await asyncio.to_thread(get_user_bookings, user_id)

async def async_cancel_booking(booking_id: int, user_id: int) -> bool:
    return await asyncio.to_thread(cancel_booking, booking_id, user_id)

async def async_cancel_booking_admin(booking_id: int) -> bool:
    return await asyncio.to_thread(cancel_booking_admin, booking_id)

async def async_update_booking(booking_id: int, **kwargs) -> bool:
    return await asyncio.to_thread(update_booking, booking_id, **kwargs)

async def async_get_bookings_for_driver(driver_id: str, date_str: Optional[str] = None) -> List[sqlite3.Row]:
    return await asyncio.to_thread(get_bookings_for_driver, driver_id, date_str)

async def async_get_all_drivers() -> Dict[str, Dict[str, Any]]:
    return await asyncio.to_thread(get_all_drivers)

async def async_get_driver(driver_id: str) -> Optional[Dict[str, Any]]:
    return await asyncio.to_thread(get_driver, driver_id)

async def async_get_driver_by_phone(phone: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    return await asyncio.to_thread(get_driver_by_phone, phone)

async def async_add_driver(*args, **kwargs) -> str:
    return await asyncio.to_thread(add_driver, *args, **kwargs)

async def async_assign_booking_driver(booking_id: int, driver_id: str) -> bool:
    return await asyncio.to_thread(assign_booking_driver, booking_id, driver_id)

async def async_reassign_booking_driver(booking_id: int, driver_id: Optional[str]) -> bool:
    return await asyncio.to_thread(reassign_booking_driver, booking_id, driver_id)

async def async_get_all_active_dates() -> List[str]:
    return await asyncio.to_thread(get_all_active_dates)

async def async_get_daily_stats(date: str) -> Tuple[int, int]:
    return await asyncio.to_thread(get_daily_stats, date)

async def async_get_unassigned_bookings_by_date(date: str) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(get_unassigned_bookings_by_date, date)

async def async_get_driver_occupancy_for_date(driver_id: str, date_str: str) -> int:
    return await asyncio.to_thread(get_driver_occupancy_for_date, driver_id, date_str)

async def async_get_recent_bookings(limit: int = 30) -> List[sqlite3.Row]:
    return await asyncio.to_thread(get_recent_bookings, limit)

async def async_get_monthly_booking_counts(year: int, month: int, route: Optional[str] = None) -> Dict[str, int]:
    return await asyncio.to_thread(get_monthly_booking_counts, year, month, route)


# ============================================================
# TELEGRAM BUSINESS CHAT COOLDOWN & OPERATOR TAKEOVER
# ============================================================

def record_business_admin_activity(chat_id: int) -> None:
    """Գրանցում է ադմինի վերջին ակտիվության ժամանակը տվյալ բիզնես չատում"""
    import time
    now = time.time()
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO business_chat_states (chat_id, last_admin_time, last_welcome_time)
                VALUES (?, ?, 0)
                ON CONFLICT(chat_id) DO UPDATE SET last_admin_time = excluded.last_admin_time
            """, (chat_id, now))
            conn.commit()
    except Exception as e:
        LOG.error("Error recording business admin activity for chat %s: %s", chat_id, e)


def record_business_welcome_sent(chat_id: int) -> None:
    """Գրանցում է ողջույնի հաղորդագրության ուղարկման ժամանակը տվյալ բիզնես չատում"""
    import time
    now = time.time()
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO business_chat_states (chat_id, last_admin_time, last_welcome_time)
                VALUES (?, 0, ?)
                ON CONFLICT(chat_id) DO UPDATE SET last_welcome_time = excluded.last_welcome_time
            """, (chat_id, now))
            conn.commit()
    except Exception as e:
        LOG.error("Error recording business welcome sent for chat %s: %s", chat_id, e)


def can_send_business_welcome(chat_id: int, silence_seconds: int = 18000) -> bool:
    """
    Ստուգում է, թե արդյոք թույլատրելի է բիզնես չատում ողջույն ուղարկել:
    Վերադարձնում է False, եթե՝
    1. Ադմինը ակտիվ է եղել վերջին `silence_seconds` վայրկյանում (Human takeover, default: 5 ժամ):
    2. Բոտը արդեն ողջույն է ուղարկել վերջին `silence_seconds` վայրկյանում (Cooldown, default: 5 ժամ):
    """
    import time
    now = time.time()
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT last_admin_time, last_welcome_time FROM business_chat_states WHERE chat_id = ?",
                (chat_id,)
            )
            row = cursor.fetchone()
            if not row:
                return True
            last_admin_time = row["last_admin_time"] or 0
            last_welcome_time = row["last_welcome_time"] or 0

            # Եթե ադմինը պատասխանել է վերջին silence_seconds-ում (Human takeover)
            if now - last_admin_time < silence_seconds:
                return False

            # Եթե ողջույն արդեն ուղարկվել է վերջին silence_seconds-ում (Cooldown)
            if now - last_welcome_time < silence_seconds:
                return False

            return True
    except Exception as e:
        LOG.error("Error checking can_send_business_welcome for chat %s: %s", chat_id, e)
        return False


async def async_record_business_admin_activity(chat_id: int) -> None:
    await asyncio.to_thread(record_business_admin_activity, chat_id)


async def async_record_business_welcome_sent(chat_id: int) -> None:
    await asyncio.to_thread(record_business_welcome_sent, chat_id)


async def async_can_send_business_welcome(chat_id: int, silence_seconds: int = 18000) -> bool:
    return await asyncio.to_thread(can_send_business_welcome, chat_id, silence_seconds)