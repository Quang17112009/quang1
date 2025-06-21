import telebot
import requests
import time
import json
import os
import random
import string
import sys
from datetime import datetime, timedelta
from threading import Thread, Event, Lock

from flask import Flask, request

# --- Cấu hình Bot ---
BOT_TOKEN = "8118428622:AAFR4sxqk20-TiDxPSzM74O7UYDbRz3anp8" 
ADMIN_IDS = [6915752059] # Ví dụ: [6915752059, 123456789]

# --- Tên file dữ liệu ---
DATA_FILE = 'user_data.json'
CAU_PATTERNS_FILE = 'cau_patterns.json'
CODES_FILE = 'codes.json'
GLOBAL_STATS_FILE = 'global_stats.json' # File mới cho thống kê toàn cục

# --- Cấu hình API cho các game ---
GAME_APIS = {
    "luckywin": {
        "url": "https://1.bot/GetNewLottery/LT_Taixiu", # Placeholder, cần thay bằng API Luckywin thật
        "id_key": "ID",
        "expect_key": "Expect",
        "opencod_key": "OpenCode",
        "dice_separator": ","
    },
    "hitclub": {
        "url": "https://apihitclub.up.railway.app/api/taixiu",
        "id_key": "Phien",
        "expect_key": "Phien",
        "opencod_key": ["Xuc_xac_1", "Xuc_xac_2", "Xuc_xac_3"], # Sẽ lấy từng xúc xắc
        "result_key": "Ket_qua" # Key chứa kết quả "Tài" hoặc "Xỉu"
    },
    "sunwin": {
        "url": "https://wanglinapiws.up.railway.app/api/taixiu",
        "id_key": "Phien",
        "expect_key": "Phien",
        "opencod_key": ["Xuc_xac_1", "Xuc_xac_2", "Xuc_xac_3"],
        "result_key": "Ket_qua"
    }
}

# --- Khởi tạo Flask App và Telegram Bot ---
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN)

# Global flags và objects
bot_enabled = True
bot_disable_reason = "Không có"
bot_disable_admin_id = None
prediction_stop_events = {game: Event() for game in GAME_APIS.keys()} # Mỗi game một Event
bot_initialized = False # Cờ để đảm bảo bot chỉ được khởi tạo một lần
bot_init_lock = Lock() # Khóa để tránh race condition khi khởi tạo

# Global sets for patterns and codes
CAU_PATTERNS = {game: {'dep': set(), 'xau': set()} for game in GAME_APIS.keys()}
GENERATED_CODES = {} # {code: {"value": 1, "type": "day", "used_by": null, "used_time": null}}
GLOBAL_STATS = {game: {'total_predictions': 0, 'correct_predictions': 0, 'wrong_predictions': 0} for game in GAME_APIS.keys()}
MAINTENANCE_STATUS = {game: {'is_down': False, 'reason': 'Không có', 'admin_id': None} for game in GAME_APIS.keys()}
OVERRIDE_MAINTENANCE_USERS = set() # User IDs that can bypass maintenance

# --- Quản lý dữ liệu người dùng, mẫu cầu và code ---
user_data = {}

def load_data_from_file(file_path, default_value):
    if os.path.exists(file_path):
        with open(file_path, 'r', encoding='utf-8') as f:
            try:
                data = json.load(f)
                print(f"DEBUG: Tải dữ liệu từ {file_path}")
                return data
            except json.JSONDecodeError:
                print(f"LỖI: Lỗi đọc {file_path}. Khởi tạo lại dữ liệu.")
            except Exception as e:
                print(f"LỖI: Lỗi không xác định khi tải {file_path}: {e}")
    print(f"DEBUG: File {file_path} không tồn tại hoặc lỗi. Khởi tạo dữ liệu rỗng/mặc định.")
    return default_value

def save_data_to_file(file_path, data):
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        # print(f"DEBUG: Đã lưu dữ liệu vào {file_path}")
    except Exception as e:
        print(f"LỖI: Không thể lưu dữ liệu vào {file_path}: {e}")
    sys.stdout.flush()

def load_user_data():
    global user_data
    user_data = load_data_from_file(DATA_FILE, {})
    # Ensure all users have prediction settings for each game
    for user_id, u_data in user_data.items():
        if 'prediction_settings' not in u_data:
            u_data['prediction_settings'] = {}
        for game_name in GAME_APIS.keys():
            if game_name not in u_data['prediction_settings']:
                u_data['prediction_settings'][game_name] = True # Mặc định bật nhận dự đoán cho tất cả game
        if 'banned' not in u_data:
            u_data['banned'] = False
        if 'ban_reason' not in u_data:
            u_data['ban_reason'] = None
        if 'override_maintenance' not in u_data:
            u_data['override_maintenance'] = False
    save_user_data(user_data) # Save to update new fields

def save_user_data(data=None):
    save_data_to_file(DATA_FILE, data if data is not None else user_data)

def load_cau_patterns():
    global CAU_PATTERNS
    loaded_patterns = load_data_from_file(CAU_PATTERNS_FILE, {})
    for game in GAME_APIS.keys():
        if game not in loaded_patterns:
            loaded_patterns[game] = {'dep': [], 'xau': []}
        CAU_PATTERNS[game]['dep'].update(loaded_patterns[game].get('dep', []))
        CAU_PATTERNS[game]['xau'].update(loaded_patterns[game].get('xau', []))
    print(f"DEBUG: Tải mẫu cầu cho tất cả game từ {CAU_PATTERNS_FILE}")
    sys.stdout.flush()

def save_cau_patterns():
    serializable_patterns = {game: {'dep': list(data['dep']), 'xau': list(data['xau'])}
                             for game, data in CAU_PATTERNS.items()}
    save_data_to_file(CAU_PATTERNS_FILE, serializable_patterns)

def load_codes():
    global GENERATED_CODES
    GENERATED_CODES = load_data_from_file(CODES_FILE, {})

def save_codes():
    save_data_to_file(CODES_FILE, GENERATED_CODES)

def load_global_stats():
    global GLOBAL_STATS, MAINTENANCE_STATUS, OVERRIDE_MAINTENANCE_USERS
    loaded_stats_data = load_data_from_file(GLOBAL_STATS_FILE, {})
    
    # Load GLOBAL_STATS
    for game in GAME_APIS.keys():
        if game in loaded_stats_data and 'stats' in loaded_stats_data[game]:
            GLOBAL_STATS[game].update(loaded_stats_data[game]['stats'])
        else:
            GLOBAL_STATS[game] = {'total_predictions': 0, 'correct_predictions': 0, 'wrong_predictions': 0}
    
    # Load MAINTENANCE_STATUS
    if 'maintenance' in loaded_stats_data:
        for game in GAME_APIS.keys():
            if game in loaded_stats_data['maintenance']:
                MAINTENANCE_STATUS[game].update(loaded_stats_data['maintenance'][game])
    
    # Load OVERRIDE_MAINTENANCE_USERS
    OVERRIDE_MAINTENANCE_USERS.clear()
    if 'override_users' in loaded_stats_data:
        OVERRIDE_MAINTENANCE_USERS.update(loaded_stats_data['override_users'])

    print(f"DEBUG: Tải thống kê, trạng thái bảo trì và người dùng override từ {GLOBAL_STATS_FILE}")
    sys.stdout.flush()

def save_global_stats():
    serializable_stats = {
        game: {'stats': stats} for game, stats in GLOBAL_STATS.items()
    }
    serializable_stats['maintenance'] = MAINTENANCE_STATUS
    serializable_stats['override_users'] = list(OVERRIDE_MAINTENANCE_USERS)
    save_data_to_file(GLOBAL_STATS_FILE, serializable_stats)

def is_admin(user_id):
    return user_id in ADMIN_IDS

def is_ctv(user_id):
    return is_admin(user_id) or (str(user_id) in user_data and user_data[str(user_id)].get('is_ctv'))

def check_subscription(user_id):
    user_id_str = str(user_id)
    if is_admin(user_id) or is_ctv(user_id):
        return True, "Bạn là Admin/CTV, quyền truy cập vĩnh viễn."

    if user_id_str not in user_data or user_data[user_id_str].get('expiry_date') is None:
        return False, "⚠️ Bạn chưa đăng ký hoặc tài khoản chưa được gia hạn."

    expiry_date_str = user_data[user_id_str]['expiry_date']
    expiry_date = datetime.strptime(expiry_date_str, '%Y-%m-%d %H:%M:%S')

    if datetime.now() < expiry_date:
        remaining_time = expiry_date - datetime.now()
        days = remaining_time.days
        hours = remaining_time.seconds // 3600
        minutes = (remaining_time.seconds % 3600) // 60
        seconds = remaining_time.seconds % 60
        return True, f"✅ Tài khoản của bạn còn hạn đến: `{expiry_date_str}` ({days} ngày {hours} giờ {minutes} phút {seconds} giây)."
    else:
        return False, "❌ Tài khoản của bạn đã hết hạn."

def is_banned(user_id):
    user_id_str = str(user_id)
    return user_id_str in user_data and user_data[user_id_str].get('banned', False)

# --- Logic dự đoán Tài Xỉu ---
def du_doan_theo_xi_ngau(dice_list):
    # Dựa trên một xúc xắc trong 3 xúc xắc và tổng để đưa ra dự đoán.
    # Logic này có thể phức tạp hơn tùy thuộc vào thuật toán AI.
    if not dice_list:
        return "Đợi thêm dữ liệu"
    
    # Lấy xúc xắc cuối cùng để dự đoán
    # Ví dụ đơn giản: dựa vào tổng chẵn/lẻ của xúc xắc cuối + tổng toàn bộ
    d1, d2, d3 = dice_list[-1]
    total = d1 + d2 + d3

    # Một cách dự đoán đơn giản hơn, có thể thay thế bằng thuật toán phức tạp hơn
    # Dựa vào số lượng Tài/Xỉu trong các cặp (x1, x2), (x2, x3), (x3, tổng)
    results = []
    
    # Example 1: (d1+d2)%2, (d2+d3)%2, (d1+d3)%2
    # Example 2: (d1+total)%2, (d2+total)%2, (d3+total)%2
    # Dùng phương pháp trong code cũ, nhân bản cho 3 xúc xắc
    for d in [d1, d2, d3]:
        tmp = d + total
        if tmp in [4, 5]: # Điều chỉnh để giữ giá trị trong một khoảng nhất định
            tmp -= 4
        elif tmp >= 6:
            tmp -= 6
        results.append("Tài" if tmp % 2 == 0 else "Xỉu")

    # Chọn kết quả xuất hiện nhiều nhất, nếu hòa chọn ngẫu nhiên Tài/Xỉu
    if results.count("Tài") > results.count("Xỉu"):
        return "Tài"
    elif results.count("Xỉu") > results.count("Tài"):
        return "Xỉu"
    else: # Hòa, chọn ngẫu nhiên
        return random.choice(["Tài", "Xỉu"])


def tinh_tai_xiu(dice):
    total = sum(dice)
    if total <= 10:
        return "Xỉu", total
    else:
        return "Tài", total

# --- Cập nhật mẫu cầu động ---
def update_cau_patterns(game_name, new_cau, prediction_correct):
    global CAU_PATTERNS
    if game_name not in CAU_PATTERNS:
        CAU_PATTERNS[game_name] = {'dep': set(), 'xau': set()} # Khởi tạo nếu chưa có
    
    if prediction_correct:
        CAU_PATTERNS[game_name]['dep'].add(new_cau)
        if new_cau in CAU_PATTERNS[game_name]['xau']:
            CAU_PATTERNS[game_name]['xau'].remove(new_cau)
            print(f"DEBUG: Xóa mẫu cầu '{new_cau}' khỏi cầu xấu của {game_name}.")
    else:
        CAU_PATTERNS[game_name]['xau'].add(new_cau)
        if new_cau in CAU_PATTERNS[game_name]['dep']:
            CAU_PATTERNS[game_name]['dep'].remove(new_cau)
            print(f"DEBUG: Xóa mẫu cầu '{new_cau}' khỏi cầu đẹp của {game_name}.")
    save_cau_patterns()
    sys.stdout.flush()

def is_cau_xau(game_name, cau_str):
    if game_name not in CAU_PATTERNS:
        return False
    return cau_str in CAU_PATTERNS[game_name]['xau']

def is_cau_dep(game_name, cau_str):
    if game_name not in CAU_PATTERNS:
        return False
    return cau_str in CAU_PATTERNS[game_name]['dep'] and cau_str not in CAU_PATTERNS[game_name]['xau'] # Đảm bảo không trùng cầu xấu

# --- Lấy dữ liệu từ API ---
def lay_du_lieu(game_name):
    config = GAME_APIS.get(game_name)
    if not config:
        print(f"LỖI: Cấu hình API cho game '{game_name}' không tồn tại.")
        sys.stdout.flush()
        return None

    try:
        response = requests.get(config['url'], timeout=10)
        response.raise_for_status()
        data = response.json()

        # Kiểm tra cấu trúc dữ liệu trả về của từng API
        if game_name == "luckywin":
            if data.get("state") != 1:
                print(f"DEBUG: API {game_name} trả về state không thành công: {data.get('state')}. Phản hồi đầy đủ: {data}")
                sys.stdout.flush()
                return None
            return data.get("data")
        elif game_name in ["hitclub", "sunwin"]:
            # Các API này trả về trực tiếp dict chứa dữ liệu
            return data
        else:
            print(f"LỖI: Game '{game_name}' có cấu hình API không rõ.")
            sys.stdout.flush()
            return None
    except requests.exceptions.Timeout:
        print(f"LỖI: Hết thời gian chờ khi lấy dữ liệu từ API {game_name}: {config['url']}")
        sys.stdout.flush()
        return None
    except requests.exceptions.ConnectionError as e:
        print(f"LỖI: Lỗi kết nối khi lấy dữ liệu từ API {game_name}: {config['url']} - {e}")
        sys.stdout.flush()
        return None
    except requests.exceptions.RequestException as e:
        print(f"LỖI: Lỗi HTTP hoặc Request khác khi lấy dữ liệu từ API {game_name}: {config['url']} - {e}")
        sys.stdout.flush()
        return None
    except json.JSONDecodeError:
        print(f"LỖI: Lỗi giải mã JSON từ API {game_name} ({config['url']}). Phản hồi không phải JSON hợp lệ hoặc trống.")
        print(f"DEBUG: Phản hồi thô nhận được: {response.text}")
        sys.stdout.flush()
        return None
    except Exception as e:
        print(f"LỖI: Lỗi không xác định khi lấy dữ liệu API {game_name} ({config['url']}): {e}")
        sys.stdout.flush()
        return None

# --- Logic chính của Bot dự đoán (chạy trong luồng riêng) ---
def prediction_loop(game_name, stop_event: Event):
    last_id = None
    tx_history = [] # Lịch sử T/X của game này
    
    print(f"LOG: Luồng dự đoán cho {game_name.upper()} đã khởi động.")
    sys.stdout.flush()

    while not stop_event.is_set():
        if not bot_enabled:
            print(f"LOG: Bot dự đoán đang tạm dừng. Lý do: {bot_disable_reason}")
            sys.stdout.flush()
            time.sleep(10)
            continue
        
        if MAINTENANCE_STATUS[game_name]['is_down']:
            print(f"LOG: Game {game_name.upper()} đang bảo trì. Lý do: {MAINTENANCE_STATUS[game_name]['reason']}")
            sys.stdout.flush()
            time.sleep(10) # Ngủ lâu hơn khi game bảo trì
            continue

        data = lay_du_lieu(game_name)
        if not data:
            print(f"LOG: ❌ {game_name.upper()}: Không lấy được dữ liệu từ API hoặc dữ liệu không hợp lệ. Đang chờ phiên mới...")
            sys.stdout.flush()
            time.sleep(5)
            continue
        
        config = GAME_APIS[game_name]
        issue_id = data.get(config['id_key'])
        expect = data.get(config['expect_key'])
        
        dice = []
        ket_qua_tx = ""
        tong = 0

        # Xử lý OpenCode hoặc xúc xắc tùy theo API
        if isinstance(config['opencod_key'], list): # Đối với Sunwin/Hitclub (xúc xắc riêng lẻ)
            try:
                dice = [data.get(k) for k in config['opencod_key']]
                if not all(isinstance(d, int) for d in dice):
                    raise ValueError(f"Dữ liệu xúc xắc không hợp lệ: {dice}")
                ket_qua_tx = data.get(config['result_key']) # Lấy kết quả trực tiếp từ API (ví dụ "Tài" hoặc "Xỉu")
                tong = sum(dice)
            except Exception as e:
                print(f"LỖI: {game_name.upper()}: Lỗi phân tích xúc xắc hoặc kết quả từ API: {data}. {e}. Bỏ qua phiên này.")
                sys.stdout.flush()
                last_id = issue_id 
                time.sleep(5)
                continue
        else: # Đối với Luckywin (OpenCode string)
            open_code_str = data.get(config['opencod_key'])
            if not open_code_str:
                print(f"LOG: {game_name.upper()}: Dữ liệu API không đầy đủ (thiếu {config['opencod_key']}) cho phiên {expect}. Bỏ qua phiên này. Dữ liệu: {data}")
                sys.stdout.flush()
                last_id = issue_id
                time.sleep(5)
                continue
            try:
                dice = tuple(map(int, open_code_str.split(config['dice_separator'])))
                if len(dice) != 3:
                    raise ValueError("OpenCode không chứa 3 giá trị xúc xắc.")
                ket_qua_tx, tong = tinh_tai_xiu(dice)
            except ValueError as e:
                print(f"LỖI: {game_name.upper()}: Lỗi phân tích OpenCode: '{open_code_str}'. {e}. Bỏ qua phiên này.")
                sys.stdout.flush()
                last_id = issue_id 
                time.sleep(5)
                continue
            except Exception as e:
                print(f"LỖI: {game_name.upper()}: Lỗi không xác định khi xử lý OpenCode '{open_code_str}': {e}. Bỏ qua phiên này.")
                sys.stdout.flush()
                last_id = issue_id
                time.sleep(5)
                continue
        
        if not all([issue_id, expect, dice, ket_qua_tx]):
            print(f"LOG: {game_name.upper()}: Dữ liệu API không đầy đủ (thiếu ID, Expect, Dice, hoặc Result) cho phiên {expect}. Bỏ qua phiên này. Dữ liệu: {data}")
            sys.stdout.flush()
            time.sleep(5)
            continue

        if issue_id != last_id:
            # Lưu lịch sử 5 phiên
            if len(tx_history) >= 5:
                tx_history.pop(0)
            tx_history.append("T" if ket_qua_tx == "Tài" else "X")

            next_expect = str(int(expect) + 1).zfill(len(str(expect)))
            du_doan = du_doan_theo_xi_ngau([dice])

            ly_do = ""
            current_cau = ""

            if len(tx_history) < 5:
                ly_do = "AI Dự đoán theo xí ngầu (chưa đủ mẫu cầu)"
            else:
                current_cau = ''.join(tx_history)
                if is_cau_dep(game_name, current_cau):
                    ly_do = f"AI Cầu đẹp ({current_cau}) → Giữ nguyên kết quả"
                elif is_cau_xau(game_name, current_cau):
                    du_doan = "Xỉu" if du_doan == "Tài" else "Tài" # Đảo chiều
                    ly_do = f"AI Cầu xấu ({current_cau}) → Đảo chiều kết quả"
                else:
                    ly_do = f"AI Không rõ mẫu cầu ({current_cau}) → Dự đoán theo xí ngầu"
            
            # Cập nhật mẫu cầu dựa trên kết quả thực tế
            if len(tx_history) >= 5:
                prediction_correct = (du_doan == "Tài" and ket_qua_tx == "Tài") or \
                                     (du_doan == "Xỉu" and ket_qua_tx == "Xỉu")
                update_cau_patterns(game_name, current_cau, prediction_correct)
                print(f"DEBUG: Cập nhật mẫu cầu cho {game_name}: '{current_cau}' - Chính xác: {prediction_correct}")
                sys.stdout.flush()
            
            # Cập nhật thống kê toàn cục
            GLOBAL_STATS[game_name]['total_predictions'] += 1
            if prediction_correct:
                GLOBAL_STATS[game_name]['correct_predictions'] += 1
            else:
                GLOBAL_STATS[game_name]['wrong_predictions'] += 1
            save_global_stats()


            # Gửi tin nhắn dự đoán tới tất cả người dùng có quyền truy cập và đã bật dự đoán cho game này
            for user_id_str, user_info in list(user_data.items()): 
                user_id = int(user_id_str)

                if is_banned(user_id):
                    continue

                # Bỏ qua nếu game đang bảo trì và người dùng không có quyền override
                if MAINTENANCE_STATUS[game_name]['is_down'] and not user_info.get('override_maintenance', False):
                    continue

                is_sub, sub_message = check_subscription(user_id)
                if is_sub and user_info.get('prediction_settings', {}).get(game_name, True): # Check if user wants predictions for this game
                    try:
                        prediction_message = (
                            f"🎮 **KẾT QUẢ PHIÊN {game_name.upper()} HIỆN TẠI** 🎮\n"
                            f"Phiên: `{expect}` | Kết quả: **{ket_qua_tx}** (Tổng: **{tong}**)\n\n"
                            f"**Dự đoán cho phiên tiếp theo:**\n"
                            f"🔢 Phiên: `{next_expect}`\n"
                            f"🤖 Dự đoán: **{du_doan}**\n"
                            f"📌 Lý do: _{ly_do}_\n"
                            f"⚠️ **Hãy đặt cược sớm trước khi phiên kết thúc!**"
                        )
                        bot.send_message(user_id, prediction_message, parse_mode='Markdown')
                        # print(f"DEBUG: Đã gửi dự đoán cho user {user_id_str} cho game {game_name}")
                        sys.stdout.flush()
                    except telebot.apihelper.ApiTelegramException as e:
                        print(f"LỖI: Lỗi Telegram API khi gửi tin nhắn cho user {user_id} (game {game_name}): {e}")
                        sys.stdout.flush()
                        if "bot was blocked by the user" in str(e) or "user is deactivated" in str(e):
                            print(f"CẢNH BÁO: Người dùng {user_id} đã chặn bot hoặc bị vô hiệu hóa. Set banned = True.")
                            user_data[user_id_str]['banned'] = True
                            user_data[user_id_str]['ban_reason'] = "Bot bị chặn hoặc tài khoản vô hiệu hóa"
                            save_user_data(user_data)
                    except Exception as e:
                        print(f"LỖI: Lỗi không xác định khi gửi tin nhắn cho user {user_id} (game {game_name}): {e}")
                        sys.stdout.flush()

            print("-" * 50)
            print(f"LOG: {game_name.upper()}: Phiên {expect} -> {next_expect}. Kết quả: {ket_qua_tx} ({tong}). Dự đoán: {du_doan}. Lý do: {ly_do}")
            print("-" * 50)
            sys.stdout.flush()

            last_id = issue_id

        time.sleep(5) 
    print(f"LOG: Luồng dự đoán cho {game_name.upper()} đã dừng.")
    sys.stdout.flush()

# --- Keep-alive function ---
def keep_alive():
    while True:
        try:
            response = requests.get("http://localhost:" + os.environ.get('PORT', '5000') + "/")
            if response.status_code == 200:
                print("DEBUG: Keep-alive ping thành công.")
            else:
                print(f"CẢNH BÁO: Keep-alive ping thất bại, status code: {response.status_code}")
        except requests.exceptions.RequestException as e:
            print(f"LỖI: Lỗi trong keep-alive: {e}")
        time.sleep(300) # Ping mỗi 5 phút (300 giây)


# --- Xử lý lệnh Telegram ---

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = str(message.chat.id)
    username = message.from_user.username or message.from_user.first_name
    
    if user_id not in user_data:
        user_data[user_id] = {
            'username': username,
            'expiry_date': None,
            'is_ctv': False,
            'banned': False,
            'ban_reason': None,
            'override_maintenance': False,
            'prediction_settings': {game: True for game in GAME_APIS.keys()} # Default to enable all
        }
        save_user_data(user_data)
        bot.reply_to(message, 
                     "Chào mừng bạn đến với **BOT DỰ ĐOÁN TÀI XỈU**!\n"
                     "Hãy dùng lệnh /help để xem danh sách các lệnh hỗ trợ.", 
                     parse_mode='Markdown')
    else:
        user_data[user_id]['username'] = username 
        # Ensure new games are added to prediction settings if they weren't there before
        if 'prediction_settings' not in user_data[user_id]:
            user_data[user_id]['prediction_settings'] = {}
        for game_name in GAME_APIS.keys():
            if game_name not in user_data[user_id]['prediction_settings']:
                user_data[user_id]['prediction_settings'][game_name] = True
        save_user_data(user_data)
        bot.reply_to(message, "Bạn đã khởi động bot rồi. Dùng /help để xem các lệnh.")

@bot.message_handler(commands=['help'])
def show_help(message):
    help_text = (
        "🤖 **DANH SÁCH LỆNH HỖ TRỢ** 🤖\n\n"
        "**Lệnh người dùng:**\n"
        "🔸 `/start`: Khởi động bot và thêm bạn vào hệ thống.\n"
        "🔸 `/help`: Hiển thị danh sách các lệnh.\n"
        "🔸 `/support`: Thông tin hỗ trợ Admin.\n"
        "🔸 `/gia`: Xem bảng giá dịch vụ.\n"
        "🔸 `/gopy <nội dung>`: Gửi góp ý/báo lỗi cho Admin.\n"
        "🔸 `/nap`: Hướng dẫn nạp tiền.\n"
    )
    for game_name in GAME_APIS.keys():
        help_text += f"🔸 `/dudoan_{game_name}`: Bắt đầu nhận dự đoán cho {game_name.replace('_', ' ').title()}.\n"
    help_text += (
        "🔸 `/code <mã_code>`: Nhập mã code để gia hạn tài khoản.\n"
        "🔸 `/stop [tên game]`: Tạm ngừng nhận dự đoán (để trống để tạm ngừng tất cả, hoặc chỉ định game).\n"
        "🔸 `/continue [tên game]`: Tiếp tục nhận dự đoán (để trống để tiếp tục tất cả, hoặc chỉ định game).\n\n"
    )
    
    if is_ctv(message.chat.id):
        help_text += (
            "**Lệnh Admin/CTV:**\n"
            "🔹 `/full <id>`: Xem thông tin người dùng (để trống ID để xem của bạn).\n"
            "🔹 `/giahan <id> <số ngày/giờ>`: Gia hạn tài khoản người dùng. Ví dụ: `/giahan 12345 1 ngày` hoặc `/giahan 12345 24 giờ`.\n\n"
        )
    
    if is_admin(message.chat.id):
        help_text += (
            "**Lệnh Admin Chính:**\n"
            "👑 `/ctv <id>`: Thêm người dùng làm CTV.\n"
            "👑 `/xoactv <id>`: Xóa người dùng khỏi CTV.\n"
            "👑 `/tb <nội dung>`: Gửi thông báo đến tất cả người dùng.\n"
            "👑 `/tatbot <lý do>`: Tắt mọi hoạt động của bot dự đoán.\n"
            "👑 `/mokbot`: Mở lại hoạt động của bot dự đoán.\n"
            "👑 `/taocode <giá trị> <ngày/giờ> <số lượng>`: Tạo mã code gia hạn. Ví dụ: `/taocode 1 ngày 5`.\n"
            "👑 `/maucau <tên game>`: Hiển thị các mẫu cầu bot đã thu thập (xấu/đẹp) cho game.\n"
            "👑 `/kiemtra`: Kiểm tra thông tin tất cả người dùng bot và thống kê.\n"
            "👑 `/xoahan <id>`: Xóa số ngày còn lại của người dùng.\n"
            "👑 `/ban <id> [lý do]`: Cấm người dùng sử dụng bot.\n"
            "👑 `/unban <id>`: Bỏ cấm người dùng.\n"
            "👑 `/baotri <tên game> [lý do]`: Đặt game vào trạng thái bảo trì.\n"
            "👑 `/mobaochi <tên game>`: Bỏ trạng thái bảo trì cho game.\n"
            "👑 `/override <id>`: Cấp quyền Admin/CTV vẫn nhận dự đoán khi game bảo trì.\n"
            "👑 `/unoverride <id>`: Xóa quyền Admin/CTV override bảo trì.\n"
        )
    
    bot.reply_to(message, help_text, parse_mode='Markdown')

@bot.message_handler(commands=['support'])
def show_support(message):
    bot.reply_to(message, 
        "Để được hỗ trợ, vui lòng liên hệ Admin:\n"
        "@heheviptool hoặc @Besttaixiu999"
    )

@bot.message_handler(commands=['gia'])
def show_price(message):
    price_text = (
        "📊 **BOT SUNWIN XIN THÔNG BÁO BẢNG GIÁ SUN BOT** 📊\n\n"
        "💸 **20k**: 1 Ngày\n"
        "💸 **50k**: 1 Tuần\n"
        "💸 **80k**: 2 Tuần\n"
        "💸 **130k**: 1 Tháng\n\n"
        "🤖 BOT SUN TỈ Lệ **85-92%**\n"
        "⏱️ ĐỌC 24/24\n\n"
        "Vui Lòng ib @heheviptool hoặc @Besttaixiu999 Để Gia Hạn"
    )
    bot.reply_to(message, price_text, parse_mode='Markdown')

@bot.message_handler(commands=['gopy'])
def send_feedback(message):
    feedback_text = telebot.util.extract_arguments(message.text)
    if not feedback_text:
        bot.reply_to(message, "Vui lòng nhập nội dung góp ý. Ví dụ: `/gopy Bot dự đoán rất chuẩn!`", parse_mode='Markdown')
        return
    
    admin_id = ADMIN_IDS[0] # Gửi cho Admin đầu tiên trong danh sách
    user_name = message.from_user.username or message.from_user.first_name
    bot.send_message(admin_id, 
                     f"📢 **GÓP Ý MỚI TỪ NGƯỜI DÙNG** 📢\n\n"
                     f"**ID:** `{message.chat.id}`\n"
                     f"**Tên:** @{user_name}\n\n"
                     f"**Nội dung:**\n`{feedback_text}`",
                     parse_mode='Markdown')
    bot.reply_to(message, "Cảm ơn bạn đã gửi góp ý! Admin đã nhận được.")

@bot.message_handler(commands=['nap'])
def show_deposit_info(message):
    user_id = message.chat.id
    deposit_text = (
        "⚜️ **NẠP TIỀN MUA LƯỢT** ⚜️\n\n"
        "Để mua lượt, vui lòng chuyển khoản đến:\n"
        "- Ngân hàng: **MB BANK**\n"
        "- Số tài khoản: **0939766383**\n"
        "- Tên chủ TK: **Nguyen Huynh Nhut Quang**\n\n"
        "**NỘI DUNG CHUYỂN KHOẢN (QUAN TRỌNG):**\n"
        "`mua luot {user_id}`\n\n"
        f"❗️ Nội dung bắt buộc của bạn là:\n"
        f"`mua luot {user_id}`\n\n"
        "(Vui lòng sao chép đúng nội dung trên để được cộng lượt tự động)\n"
        "Sau khi chuyển khoản, vui lòng chờ 1-2 phút. Nếu có sự cố, hãy dùng lệnh /support."
    )
    bot.reply_to(message, deposit_text, parse_mode='Markdown')

# Dynamic prediction commands for each game
for game_key in GAME_APIS.keys():
    @bot.message_handler(commands=[f'dudoan_{game_key}'])
    def start_prediction_for_game(message, game=game_key):
        user_id = message.chat.id
        if is_banned(user_id):
            bot.reply_to(message, f"❌ Bạn đã bị cấm sử dụng bot. Lý do: `{user_data[str(user_id)].get('ban_reason', 'Không rõ')}`", parse_mode='Markdown')
            return

        is_sub, sub_message = check_subscription(user_id)
        if not is_sub:
            bot.reply_to(message, sub_message + "\nVui lòng liên hệ Admin @heheviptool hoặc @Besttaixiu999 để được hỗ trợ.", parse_mode='Markdown')
            return
        
        if MAINTENANCE_STATUS[game]['is_down'] and not user_data[str(user_id)].get('override_maintenance', False):
            bot.reply_to(message, f"❌ Game {game.upper()} hiện đang bảo trì. Lý do: `{MAINTENANCE_STATUS[game]['reason']}`", parse_mode='Markdown')
            return

        user_id_str = str(user_id)
        if user_id_str not in user_data or 'prediction_settings' not in user_data[user_id_str]:
            user_data.setdefault(user_id_str, {}).setdefault('prediction_settings', {game_name: True for game_name in GAME_APIS.keys()})
            save_user_data(user_data) # Ensure structure exists

        user_data[user_id_str]['prediction_settings'][game] = True
        save_user_data(user_data)
        bot.reply_to(message, f"✅ Bạn đã bật nhận dự đoán cho **{game.replace('_', ' ').title()}**. Bot sẽ tự động gửi dự đoán các phiên mới nhất tại đây.")

# General /dudoan command (legacy or for all)
@bot.message_handler(commands=['dudoan'])
def start_all_predictions(message):
    user_id = message.chat.id
    if is_banned(user_id):
        bot.reply_to(message, f"❌ Bạn đã bị cấm sử dụng bot. Lý do: `{user_data[str(user_id)].get('ban_reason', 'Không rõ')}`", parse_mode='Markdown')
        return

    is_sub, sub_message = check_subscription(user_id)
    if not is_sub:
        bot.reply_to(message, sub_message + "\nVui lòng liên hệ Admin @heheviptool hoặc @Besttaixiu999 để được hỗ trợ.", parse_mode='Markdown')
        return
    
    # Check if any game is in maintenance and user cannot override
    maintenance_games = [g for g, status in MAINTENANCE_STATUS.items() if status['is_down'] and not user_data[str(user_id)].get('override_maintenance', False)]
    if maintenance_games:
        bot.reply_to(message, f"❌ Các game sau đang bảo trì và bạn không có quyền nhận dự đoán: {', '.join([g.upper() for g in maintenance_games])}. Vui lòng thử lại sau.", parse_mode='Markdown')
        return

    user_id_str = str(user_id)
    if user_id_str not in user_data or 'prediction_settings' not in user_data[user_id_str]:
        user_data.setdefault(user_id_str, {}).setdefault('prediction_settings', {game_name: True for game_name in GAME_APIS.keys()})
    
    for game_name in GAME_APIS.keys():
        user_data[user_id_str]['prediction_settings'][game_name] = True
    save_user_data(user_data)
    bot.reply_to(message, "✅ Bạn đã bật nhận dự đoán cho **TẤT CẢ CÁC GAME** (Luckywin, Hit Club, Sunwin). Bot sẽ tự động gửi dự đoán các phiên mới nhất tại đây.")


@bot.message_handler(commands=['stop'])
def stop_predictions(message):
    user_id = str(message.chat.id)
    if is_banned(user_id):
        bot.reply_to(message, f"❌ Bạn đã bị cấm sử dụng bot. Lý do: `{user_data[user_id].get('ban_reason', 'Không rõ')}`", parse_mode='Markdown')
        return

    args = telebot.util.extract_arguments(message.text).split()
    game_to_stop = args[0].lower() if args else None

    if user_id not in user_data or 'prediction_settings' not in user_data[user_id]:
        user_data.setdefault(user_id, {}).setdefault('prediction_settings', {game_name: True for game_name in GAME_APIS.keys()})
        save_user_data(user_data)

    if game_to_stop:
        if game_to_stop in GAME_APIS:
            user_data[user_id]['prediction_settings'][game_to_stop] = False
            bot.reply_to(message, f"✅ Đã tạm ngừng nhận dự đoán cho **{game_to_stop.replace('_', ' ').title()}**.", parse_mode='Markdown')
        else:
            bot.reply_to(message, f"❌ Game `{game_to_stop}` không hợp lệ. Các game hỗ trợ: {', '.join(GAME_APIS.keys())}.", parse_mode='Markdown')
    else:
        for game_name in GAME_APIS.keys():
            user_data[user_id]['prediction_settings'][game_name] = False
        bot.reply_to(message, "✅ Đã tạm ngừng nhận dự đoán cho **TẤT CẢ CÁC GAME**.", parse_mode='Markdown')
    
    save_user_data(user_data)

@bot.message_handler(commands=['continue'])
def continue_predictions(message):
    user_id = str(message.chat.id)
    if is_banned(user_id):
        bot.reply_to(message, f"❌ Bạn đã bị cấm sử dụng bot. Lý do: `{user_data[user_id].get('ban_reason', 'Không rõ')}`", parse_mode='Markdown')
        return

    is_sub, sub_message = check_subscription(int(user_id))
    if not is_sub:
        bot.reply_to(message, sub_message + "\nVui lòng liên hệ Admin @heheviptool hoặc @Besttaixiu999 để được hỗ trợ.", parse_mode='Markdown')
        return

    args = telebot.util.extract_arguments(message.text).split()
    game_to_continue = args[0].lower() if args else None

    if user_id not in user_data or 'prediction_settings' not in user_data[user_id]:
        user_data.setdefault(user_id, {}).setdefault('prediction_settings', {game_name: True for game_name in GAME_APIS.keys()})
        save_user_data(user_data)
    
    if game_to_continue:
        if game_to_continue in GAME_APIS:
            if MAINTENANCE_STATUS[game_to_continue]['is_down'] and not user_data[user_id].get('override_maintenance', False):
                bot.reply_to(message, f"❌ Game {game_to_continue.upper()} hiện đang bảo trì. Lý do: `{MAINTENANCE_STATUS[game_to_continue]['reason']}`", parse_mode='Markdown')
                return
            user_data[user_id]['prediction_settings'][game_to_continue] = True
            bot.reply_to(message, f"✅ Đã tiếp tục nhận dự đoán cho **{game_to_continue.replace('_', ' ').title()}**.", parse_mode='Markdown')
        else:
            bot.reply_to(message, f"❌ Game `{game_to_continue}` không hợp lệ. Các game hỗ trợ: {', '.join(GAME_APIS.keys())}.", parse_mode='Markdown')
    else:
        # Check for maintenance on any game if attempting to resume all
        maintenance_games = [g for g, status in MAINTENANCE_STATUS.items() if status['is_down'] and not user_data[user_id].get('override_maintenance', False)]
        if maintenance_games:
            bot.reply_to(message, f"❌ Một số game đang bảo trì và bạn không có quyền nhận dự đoán: {', '.join([g.upper() for g in maintenance_games])}. Không thể bật lại tất cả.", parse_mode='Markdown')
            return
            
        for game_name in GAME_APIS.keys():
            user_data[user_id]['prediction_settings'][game_name] = True
        bot.reply_to(message, "✅ Đã tiếp tục nhận dự đoán cho **TẤT CẢ CÁC GAME**.", parse_mode='Markdown')
    
    save_user_data(user_data)

@bot.message_handler(commands=['code'])
def use_code(message):
    code_str = telebot.util.extract_arguments(message.text)
    user_id = str(message.chat.id)

    if is_banned(user_id):
        bot.reply_to(message, f"❌ Bạn đã bị cấm sử dụng bot. Lý do: `{user_data[user_id].get('ban_reason', 'Không rõ')}`", parse_mode='Markdown')
        return

    if not code_str:
        bot.reply_to(message, "Vui lòng nhập mã code. Ví dụ: `/code ABCXYZ`", parse_mode='Markdown')
        return
    
    if code_str not in GENERATED_CODES:
        bot.reply_to(message, "❌ Mã code không tồn tại hoặc đã hết hạn.")
        return

    code_info = GENERATED_CODES[code_str]
    if code_info.get('used_by') is not None:
        bot.reply_to(message, "❌ Mã code này đã được sử dụng rồi.")
        return

    # Apply extension
    current_expiry_str = user_data.get(user_id, {}).get('expiry_date')
    if current_expiry_str:
        current_expiry_date = datetime.strptime(current_expiry_str, '%Y-%m-%d %H:%M:%S')
        # If current expiry is in the past, start from now
        if datetime.now() > current_expiry_date:
            new_expiry_date = datetime.now()
        else:
            new_expiry_date = current_expiry_date
    else:
        new_expiry_date = datetime.now() # Start from now if no previous expiry

    value = code_info['value']
    if code_info['type'] == 'ngày':
        new_expiry_date += timedelta(days=value)
    elif code_info['type'] == 'giờ':
        new_expiry_date += timedelta(hours=value)
    
    user_data.setdefault(user_id, {})['expiry_date'] = new_expiry_date.strftime('%Y-%m-%d %H:%M:%S')
    user_data[user_id]['username'] = message.from_user.username or message.from_user.first_name
    # Ensure prediction settings are initialized if user is new
    if 'prediction_settings' not in user_data[user_id]:
        user_data[user_id]['prediction_settings'] = {game_name: True for game_name in GAME_APIS.keys()}
    
    GENERATED_CODES[code_str]['used_by'] = user_id
    GENERATED_CODES[code_str]['used_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    save_user_data(user_data)
    save_codes()

    bot.reply_to(message, 
                 f"🎉 Bạn đã đổi mã code thành công! Tài khoản của bạn đã được gia hạn thêm **{value} {code_info['type']}**.\n"
                 f"Ngày hết hạn mới: `{user_data[user_id]['expiry_date']}`", 
                 parse_mode='Markdown')

def user_expiry_date(user_id):
    if str(user_id) in user_data and user_data[str(user_id)].get('expiry_date'):
        return user_data[str(user_id)]['expiry_date']
    return "Không có"

# --- Lệnh Admin/CTV ---
@bot.message_handler(commands=['full'])
def get_user_info(message):
    if not is_ctv(message.chat.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return
    
    args = telebot.util.extract_arguments(message.text).split()
    target_user_id_str = str(message.chat.id)
    if args and args[0].isdigit():
        target_user_id_str = args[0]
    
    if target_user_id_str not in user_data:
        bot.reply_to(message, f"Không tìm thấy thông tin cho người dùng ID `{target_user_id_str}`.")
        return

    user_info = user_data[target_user_id_str]
    expiry_date_str = user_info.get('expiry_date', 'Không có')
    username = user_info.get('username', 'Không rõ')
    is_ctv_status = "Có" if is_ctv(int(target_user_id_str)) else "Không"
    is_banned_status = "Có" if user_info.get('banned', False) else "Không"
    ban_reason = user_info.get('ban_reason', 'N/A') if is_banned_status == "Có" else "N/A"
    override_status = "Có" if user_info.get('override_maintenance', False) else "Không"

    prediction_status = []
    for game_name, status in user_info.get('prediction_settings', {}).items():
        prediction_status.append(f"{game_name.replace('_', ' ').title()}: {'BẬT' if status else 'TẮT'}")
    
    info_text = (
        f"**THÔNG TIN NGƯỜI DÙNG**\n"
        f"**ID:** `{target_user_id_str}`\n"
        f"**Tên:** @{username}\n"
        f"**Ngày hết hạn:** `{expiry_date_str}`\n"
        f"**Là CTV/Admin:** {is_ctv_status}\n"
        f"**Bị cấm:** {is_banned_status} (Lý do: `{ban_reason}`)\n"
        f"**Override BT:** {override_status}\n"
        f"**Trạng thái dự đoán:**\n- " + "\n- ".join(prediction_status)
    )
    bot.reply_to(message, info_text, parse_mode='Markdown')

@bot.message_handler(commands=['giahan'])
def extend_subscription(message):
    if not is_ctv(message.chat.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return
    
    args = telebot.util.extract_arguments(message.text).split()
    if len(args) != 3 or not args[0].isdigit() or not args[1].isdigit() or args[2].lower() not in ['ngày', 'giờ']:
        bot.reply_to(message, "Cú pháp sai. Ví dụ: `/giahan <id_nguoi_dung> <số_lượng> <ngày/giờ>`\n"
                              "Ví dụ: `/giahan 12345 1 ngày` hoặc `/giahan 12345 24 giờ`", parse_mode='Markdown')
        return
    
    target_user_id_str = args[0]
    value = int(args[1])
    unit = args[2].lower() # 'ngày' or 'giờ'
    
    if target_user_id_str not in user_data:
        user_data[target_user_id_str] = {
            'username': "UnknownUser",
            'expiry_date': None,
            'is_ctv': False,
            'banned': False,
            'ban_reason': None,
            'override_maintenance': False,
            'prediction_settings': {game: True for game in GAME_APIS.keys()}
        }
        bot.send_message(message.chat.id, f"Đã tạo tài khoản mới cho user ID `{target_user_id_str}`.")

    current_expiry_str = user_data[target_user_id_str].get('expiry_date')
    if current_expiry_str:
        current_expiry_date = datetime.strptime(current_expiry_str, '%Y-%m-%d %H:%M:%S')
        if datetime.now() > current_expiry_date:
            new_expiry_date = datetime.now()
        else:
            new_expiry_date = current_expiry_date
    else:
        new_expiry_date = datetime.now() # Start from now if no previous expiry

    if unit == 'ngày':
        new_expiry_date += timedelta(days=value)
    elif unit == 'giờ':
        new_expiry_date += timedelta(hours=value)
    
    user_data[target_user_id_str]['expiry_date'] = new_expiry_date.strftime('%Y-%m-%d %H:%M:%S')
    save_user_data(user_data)
    
    bot.reply_to(message, 
                 f"Đã gia hạn thành công cho user ID `{target_user_id_str}` thêm **{value} {unit}**.\n"
                 f"Ngày hết hạn mới: `{user_data[target_user_id_str]['expiry_date']}`",
                 parse_mode='Markdown')
    
    try:
        bot.send_message(int(target_user_id_str), 
                         f"🎉 Tài khoản của bạn đã được gia hạn thêm **{value} {unit}** bởi Admin/CTV!\n"
                         f"Ngày hết hạn mới của bạn là: `{user_data[target_user_id_str]['expiry_date']}`",
                         parse_mode='Markdown')
    except telebot.apihelper.ApiTelegramException as e:
        if "bot was blocked by the user" in str(e):
            print(f"CẢNH BÁO: Không thể thông báo gia hạn cho user {target_user_id_str}: Người dùng đã chặn bot.")
        else:
            print(f"LỖI: Không thể thông báo gia hạn cho user {target_user_id_str}: {e}")
        sys.stdout.flush()

# --- Lệnh Admin Chính ---
@bot.message_handler(commands=['ctv'])
def add_ctv(message):
    if not is_admin(message.chat.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return
    
    args = telebot.util.extract_arguments(message.text).split()
    if not args or not args[0].isdigit():
        bot.reply_to(message, "Cú pháp sai. Ví dụ: `/ctv <id_nguoi_dung>`", parse_mode='Markdown')
        return
    
    target_user_id_str = args[0]
    if target_user_id_str not in user_data:
        user_data[target_user_id_str] = {
            'username': "UnknownUser",
            'expiry_date': None,
            'is_ctv': True,
            'banned': False,
            'ban_reason': None,
            'override_maintenance': False,
            'prediction_settings': {game: True for game in GAME_APIS.keys()}
        }
    else:
        user_data[target_user_id_str]['is_ctv'] = True
    
    save_user_data(user_data)
    bot.reply_to(message, f"Đã cấp quyền CTV cho user ID `{target_user_id_str}`.")
    try:
        bot.send_message(int(target_user_id_str), "🎉 Bạn đã được cấp quyền CTV!")
    except Exception:
        pass

@bot.message_handler(commands=['xoactv'])
def remove_ctv(message):
    if not is_admin(message.chat.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return
    
    args = telebot.util.extract_arguments(message.text).split()
    if not args or not args[0].isdigit():
        bot.reply_to(message, "Cú pháp sai. Ví dụ: `/xoactv <id_nguoi_dung>`", parse_mode='Markdown')
        return
    
    target_user_id_str = args[0]
    if target_user_id_str in user_data:
        user_data[target_user_id_str]['is_ctv'] = False
        save_user_data(user_data)
        bot.reply_to(message, f"Đã xóa quyền CTV của user ID `{target_user_id_str}`.")
        try:
            bot.send_message(int(target_user_id_str), "❌ Quyền CTV của bạn đã bị gỡ bỏ.")
        except Exception:
            pass
    else:
        bot.reply_to(message, f"Không tìm thấy người dùng có ID `{target_user_id_str}`.")

@bot.message_handler(commands=['tb'])
def send_broadcast(message):
    if not is_admin(message.chat.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return
    
    broadcast_text = telebot.util.extract_arguments(message.text)
    if not broadcast_text:
        bot.reply_to(message, "Vui lòng nhập nội dung thông báo. Ví dụ: `/tb Bot sẽ bảo trì vào 2h sáng mai.`", parse_mode='Markdown')
        return
    
    success_count = 0
    fail_count = 0
    for user_id_str in list(user_data.keys()):
        # Do not send broadcast to banned users
        if user_data[user_id_str].get('banned', False):
            continue

        try:
            bot.send_message(int(user_id_str), f"📢 **THÔNG BÁO TỪ ADMIN** 📢\n\n{broadcast_text}", parse_mode='Markdown')
            success_count += 1
            time.sleep(0.1) # Tránh bị rate limit
        except telebot.apihelper.ApiTelegramException as e:
            print(f"LỖI: Không thể gửi thông báo cho user {user_id_str}: {e}")
            sys.stdout.flush()
            fail_count += 1
            if "bot was blocked by the user" in str(e) or "user is deactivated" in str(e):
                print(f"CẢNH BÁO: Người dùng {user_id_str} đã chặn bot hoặc bị vô hiệu hóa. Set banned = True.")
                user_data[user_id_str]['banned'] = True
                user_data[user_id_str]['ban_reason'] = "Bot bị chặn hoặc tài khoản vô hiệu hóa"
                save_user_data(user_data) # Save immediately if a user is marked banned
        except Exception as e:
            print(f"LỖI: Lỗi không xác định khi gửi thông báo cho user {user_id_str}: {e}")
            sys.stdout.flush()
            fail_count += 1
            
    bot.reply_to(message, f"Đã gửi thông báo đến {success_count} người dùng. Thất bại: {fail_count}.")

@bot.message_handler(commands=['tatbot'])
def disable_bot_command(message):
    global bot_enabled, bot_disable_reason, bot_disable_admin_id
    if not is_admin(message.chat.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return

    reason = telebot.util.extract_arguments(message.text)
    if not reason:
        bot.reply_to(message, "Vui lòng nhập lý do tắt bot. Ví dụ: `/tatbot Bot đang bảo trì.`", parse_mode='Markdown')
        return

    bot_enabled = False
    bot_disable_reason = reason
    bot_disable_admin_id = message.chat.id
    bot.reply_to(message, f"✅ Bot dự đoán đã được tắt bởi Admin `{message.from_user.username or message.from_user.first_name}`.\nLý do: `{reason}`", parse_mode='Markdown')
    sys.stdout.flush()
    
@bot.message_handler(commands=['mokbot'])
def enable_bot_command(message):
    global bot_enabled, bot_disable_reason, bot_disable_admin_id
    if not is_admin(message.chat.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return

    if bot_enabled:
        bot.reply_to(message, "Bot dự đoán đã và đang hoạt động rồi.")
        return

    bot_enabled = True
    bot_disable_reason = "Không có"
    bot_disable_admin_id = None
    bot.reply_to(message, "✅ Bot dự đoán đã được mở lại bởi Admin.")
    sys.stdout.flush()
    
@bot.message_handler(commands=['taocode'])
def generate_code_command(message):
    if not is_admin(message.chat.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return
    
    args = telebot.util.extract_arguments(message.text).split()
    if len(args) < 2 or len(args) > 3: 
        bot.reply_to(message, "Cú pháp sai. Ví dụ:\n"
                              "`/taocode <giá_trị> <ngày/giờ> <số_lượng>`\n"
                              "Ví dụ: `/taocode 1 ngày 5` (tạo 5 code 1 ngày)\n"
                              "Hoặc: `/taocode 24 giờ` (tạo 1 code 24 giờ)", parse_mode='Markdown')
        return
    
    try:
        value = int(args[0])
        unit = args[1].lower()
        quantity = int(args[2]) if len(args) == 3 else 1 
        
        if unit not in ['ngày', 'giờ']:
            bot.reply_to(message, "Đơn vị không hợp lệ. Chỉ chấp nhận `ngày` hoặc `giờ`.", parse_mode='Markdown')
            return
        if value <= 0 or quantity <= 0:
            bot.reply_to(message, "Giá trị hoặc số lượng phải lớn hơn 0.", parse_mode='Markdown')
            return

        generated_codes_list = []
        for _ in range(quantity):
            new_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
            GENERATED_CODES[new_code] = {
                "value": value,
                "type": unit,
                "used_by": None,
                "used_time": None
            }
            generated_codes_list.append(new_code)
        
        save_codes()
        
        response_text = f"✅ Đã tạo thành công {quantity} mã code gia hạn **{value} {unit}**:\n\n"
        response_text += "\n".join([f"`{code}`" for code in generated_codes_list])
        response_text += "\n\n_(Các mã này chưa được sử dụng)_"
        
        bot.reply_to(message, response_text, parse_mode='Markdown')

    except ValueError:
        bot.reply_to(message, "Giá trị hoặc số lượng không hợp lệ. Vui lòng nhập số nguyên.", parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(message, f"Đã xảy ra lỗi khi tạo code: {e}", parse_mode='Markdown')

@bot.message_handler(commands=['maucau'])
def show_cau_patterns_admin(message):
    if not is_admin(message.chat.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return

    args = telebot.util.extract_arguments(message.text).split()
    if not args or args[0].lower() not in GAME_APIS.keys():
        bot.reply_to(message, f"Vui lòng chỉ định game để xem mẫu cầu. Ví dụ: `/maucau luckywin`\nCác game hỗ trợ: {', '.join(GAME_APIS.keys())}", parse_mode='Markdown')
        return
    
    game_name = args[0].lower()
    
    dep_patterns = "\n".join(sorted(list(CAU_PATTERNS[game_name]['dep']))) if CAU_PATTERNS[game_name]['dep'] else "Không có"
    xau_patterns = "\n".join(sorted(list(CAU_PATTERNS[game_name]['xau']))) if CAU_PATTERNS[game_name]['xau'] else "Không có"

    pattern_text = (
        f"📚 **CÁC MẪU CẦU ĐÃ THU THẬP CHO {game_name.upper()}** 📚\n\n"
        "**🟢 Cầu Đẹp:**\n"
        f"```\n{dep_patterns}\n```\n\n"
        "**🔴 Cầu Xấu:**\n"
        f"```\n{xau_patterns}\n```\n"
        "*(Các mẫu cầu này được bot tự động học hỏi theo thời gian.)*"
    )
    bot.reply_to(message, pattern_text, parse_mode='Markdown')

@bot.message_handler(commands=['kiemtra'])
def check_all_users(message):
    if not is_admin(message.chat.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return
    
    total_users = len(user_data)
    active_users = 0
    expired_users = 0
    admin_count = 0
    ctv_count = 0
    banned_count = 0

    for user_id_str, user_info in user_data.items():
        user_id = int(user_id_str)
        if user_info.get('banned', False):
            banned_count += 1
            continue # Don't count banned users as active/expired

        if is_admin(user_id):
            admin_count += 1
        if user_info.get('is_ctv', False) and not is_admin(user_id): # Count CTVs who are not also admins
            ctv_count += 1

        is_sub, _ = check_subscription(user_id)
        if is_sub:
            active_users += 1
        else:
            expired_users += 1
    
    stats_text = (
        f"📊 **THỐNG KÊ NGƯỜI DÙNG VÀ DỰ ĐOÁN** 📊\n\n"
        f"**Tổng số người dùng:** `{total_users}`\n"
        f"**Đang hoạt động:** `{active_users}`\n"
        f"**Đã hết hạn:** `{expired_users}`\n"
        f"**Bị cấm:** `{banned_count}`\n"
        f"**Admin:** `{admin_count}`\n"
        f"**CTV (không phải Admin):** `{ctv_count}`\n\n"
        f"**Thống kê dự đoán của Bot:**\n"
    )

    for game_name, stats in GLOBAL_STATS.items():
        total = stats['total_predictions']
        correct = stats['correct_predictions']
        wrong = stats['wrong_predictions']
        win_rate = (correct / total * 100) if total > 0 else 0
        stats_text += (
            f"**- {game_name.replace('_', ' ').title()}:**\n"
            f"  + Tổng phiên: `{total}`\n"
            f"  + Dự đoán đúng: `{correct}`\n"
            f"  + Dự đoán sai: `{wrong}`\n"
            f"  + Tỷ lệ thắng: `{win_rate:.2f}%`\n"
        )
    
    # Add maintenance status
    stats_text += "\n**Trạng thái bảo trì Game:**\n"
    for game_name, status in MAINTENANCE_STATUS.items():
        if status['is_down']:
            admin_username = ""
            if status['admin_id']:
                try:
                    admin_chat_info = bot.get_chat(status['admin_id'])
                    admin_username = f" (Admin: @{admin_chat_info.username or admin_chat_info.first_name})"
                except Exception:
                    admin_username = " (Admin ID không rõ)"
            stats_text += f"- {game_name.replace('_', ' ').title()}: 🔴 Đang bảo trì. Lý do: `{status['reason']}`{admin_username}\n"
        else:
            stats_text += f"- {game_name.replace('_', ' ').title()}: 🟢 Hoạt động\n"

    stats_text += f"\n**Người dùng có quyền Override Bảo trì:**\n- " + "\n- ".join([f"`{uid}`" for uid in OVERRIDE_MAINTENANCE_USERS]) if OVERRIDE_MAINTENANCE_USERS else "Không có"

    bot.reply_to(message, stats_text, parse_mode='Markdown')

@bot.message_handler(commands=['xoahan'])
def clear_expiry_date(message):
    if not is_admin(message.chat.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return
    
    args = telebot.util.extract_arguments(message.text).split()
    if not args or not args[0].isdigit():
        bot.reply_to(message, "Cú pháp sai. Ví dụ: `/xoahan <id_nguoi_dung>`", parse_mode='Markdown')
        return
    
    target_user_id_str = args[0]
    if target_user_id_str not in user_data:
        bot.reply_to(message, f"Không tìm thấy người dùng có ID `{target_user_id_str}`.")
        return
    
    user_data[target_user_id_str]['expiry_date'] = None
    save_user_data(user_data)
    bot.reply_to(message, f"Đã xóa hạn sử dụng của user ID `{target_user_id_str}`.")
    try:
        bot.send_message(int(target_user_id_str), "❌ Tài khoản của bạn đã bị xóa hạn sử dụng bởi Admin.")
    except Exception:
        pass

@bot.message_handler(commands=['ban'])
def ban_user(message):
    if not is_admin(message.chat.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return
    
    args = telebot.util.extract_arguments(message.text).split(maxsplit=1)
    if not args or not args[0].isdigit():
        bot.reply_to(message, "Cú pháp sai. Ví dụ: `/ban <id_nguoi_dung> [lý do]`", parse_mode='Markdown')
        return
    
    target_user_id_str = args[0]
    reason = args[1] if len(args) > 1 else "Không có lý do cụ thể."

    if target_user_id_str == str(message.chat.id):
        bot.reply_to(message, "Bạn không thể tự cấm chính mình.")
        return
    
    if target_user_id_str not in user_data:
        user_data[target_user_id_str] = {
            'username': "UnknownUser",
            'expiry_date': None,
            'is_ctv': False,
            'banned': True,
            'ban_reason': reason,
            'override_maintenance': False,
            'prediction_settings': {game: False for game in GAME_APIS.keys()} # Turn off all predictions for banned user
        }
    else:
        user_data[target_user_id_str]['banned'] = True
        user_data[target_user_id_str]['ban_reason'] = reason
        # Turn off all predictions for the banned user
        for game_name in GAME_APIS.keys():
            user_data[target_user_id_str]['prediction_settings'][game_name] = False
    
    save_user_data(user_data)
    bot.reply_to(message, f"Đã cấm user ID `{target_user_id_str}`. Lý do: `{reason}`", parse_mode='Markdown')
    try:
        bot.send_message(int(target_user_id_str), f"🚫 Tài khoản của bạn đã bị cấm sử dụng bot bởi Admin. Lý do: `{reason}`", parse_mode='Markdown')
    except Exception:
        pass

@bot.message_handler(commands=['unban'])
def unban_user(message):
    if not is_admin(message.chat.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return
    
    args = telebot.util.extract_arguments(message.text).split()
    if not args or not args[0].isdigit():
        bot.reply_to(message, "Cú pháp sai. Ví dụ: `/unban <id_nguoi_dung>`", parse_mode='Markdown')
        return
    
    target_user_id_str = args[0]
    if target_user_id_str not in user_data:
        bot.reply_to(message, f"Không tìm thấy người dùng có ID `{target_user_id_str}`.")
        return
    
    if not user_data[target_user_id_str].get('banned', False):
        bot.reply_to(message, f"Người dùng ID `{target_user_id_str}` không bị cấm.")
        return

    user_data[target_user_id_str]['banned'] = False
    user_data[target_user_id_str]['ban_reason'] = None
    save_user_data(user_data)
    bot.reply_to(message, f"Đã bỏ cấm user ID `{target_user_id_str}`.")
    try:
        bot.send_message(int(target_user_id_str), "✅ Tài khoản của bạn đã được bỏ cấm.")
    except Exception:
        pass

@bot.message_handler(commands=['baotri'])
def set_maintenance_status(message):
    if not is_admin(message.chat.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return
    
    args = telebot.util.extract_arguments(message.text).split(maxsplit=1)
    if len(args) < 1 or args[0].lower() not in GAME_APIS.keys():
        bot.reply_to(message, f"Cú pháp sai. Ví dụ: `/baotri <tên game> [lý do]`\nCác game hỗ trợ: {', '.join(GAME_APIS.keys())}", parse_mode='Markdown')
        return
    
    game_name = args[0].lower()
    reason = args[1] if len(args) > 1 else "Bảo trì định kỳ."
    
    MAINTENANCE_STATUS[game_name]['is_down'] = True
    MAINTENANCE_STATUS[game_name]['reason'] = reason
    MAINTENANCE_STATUS[game_name]['admin_id'] = message.chat.id
    save_global_stats()
    
    bot.reply_to(message, f"✅ Đã đặt game **{game_name.replace('_', ' ').title()}** vào trạng thái bảo trì.\nLý do: `{reason}`", parse_mode='Markdown')
    # Optionally notify users of maintenance for this game
    # for user_id_str, user_info in list(user_data.items()):
    #     if user_info.get('prediction_settings', {}).get(game_name, False) and not user_info.get('override_maintenance', False):
    #         try:
    #             bot.send_message(int(user_id_str), f"📢 **THÔNG BÁO {game_name.upper()}:** Game hiện đang bảo trì.\nLý do: `{reason}`\nDự đoán sẽ tạm dừng cho đến khi game hoạt động trở lại.", parse_mode='Markdown')
    #         except Exception: pass

@bot.message_handler(commands=['mobaochi'])
def clear_maintenance_status(message):
    if not is_admin(message.chat.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return
    
    args = telebot.util.extract_arguments(message.text).split()
    if not args or args[0].lower() not in GAME_APIS.keys():
        bot.reply_to(message, f"Cú pháp sai. Ví dụ: `/mobaochi <tên game>`\nCác game hỗ trợ: {', '.join(GAME_APIS.keys())}", parse_mode='Markdown')
        return
    
    game_name = args[0].lower()
    
    if not MAINTENANCE_STATUS[game_name]['is_down']:
        bot.reply_to(message, f"Game **{game_name.replace('_', ' ').title()}** hiện không trong trạng thái bảo trì.", parse_mode='Markdown')
        return

    MAINTENANCE_STATUS[game_name]['is_down'] = False
    MAINTENANCE_STATUS[game_name]['reason'] = "Không có"
    MAINTENANCE_STATUS[game_name]['admin_id'] = None
    save_global_stats()
    
    bot.reply_to(message, f"✅ Đã đưa game **{game_name.replace('_', ' ').title()}** trở lại hoạt động bình thường.", parse_mode='Markdown')
    # Optionally notify users
    # for user_id_str, user_info in list(user_data.items()):
    #     if user_info.get('prediction_settings', {}).get(game_name, False):
    #         try:
    #             bot.send_message(int(user_id_str), f"🎉 **THÔNG BÁO {game_name.upper()}:** Game đã hoạt động trở lại! Dự đoán sẽ được tiếp tục.", parse_mode='Markdown')
    #         except Exception: pass

@bot.message_handler(commands=['override'])
def add_override_user(message):
    if not is_admin(message.chat.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return
    
    args = telebot.util.extract_arguments(message.text).split()
    if not args or not args[0].isdigit():
        bot.reply_to(message, "Cú pháp sai. Ví dụ: `/override <id_nguoi_dung>`", parse_mode='Markdown')
        return
    
    target_user_id_str = args[0]
    
    if target_user_id_str not in user_data:
        user_data[target_user_id_str] = {
            'username': "UnknownUser",
            'expiry_date': None,
            'is_ctv': False,
            'banned': False,
            'ban_reason': None,
            'override_maintenance': True,
            'prediction_settings': {game: True for game in GAME_APIS.keys()}
        }
    else:
        user_data[target_user_id_str]['override_maintenance'] = True
    
    OVERRIDE_MAINTENANCE_USERS.add(target_user_id_str)
    save_user_data(user_data)
    save_global_stats()
    bot.reply_to(message, f"Đã cấp quyền override bảo trì cho user ID `{target_user_id_str}`.")
    try:
        bot.send_message(int(target_user_id_str), "✨ Bạn đã được cấp quyền bỏ qua trạng thái bảo trì game.")
    except Exception:
        pass

@bot.message_handler(commands=['unoverride'])
def remove_override_user(message):
    if not is_admin(message.chat.id):
        bot.reply_to(message, "Bạn không có quyền sử dụng lệnh này.")
        return
    
    args = telebot.util.extract_arguments(message.text).split()
    if not args or not args[0].isdigit():
        bot.reply_to(message, "Cú pháp sai. Ví dụ: `/unoverride <id_nguoi_dung>`", parse_mode='Markdown')
        return
    
    target_user_id_str = args[0]
    
    if target_user_id_str in user_data:
        user_data[target_user_id_str]['override_maintenance'] = False
    
    if target_user_id_str in OVERRIDE_MAINTENANCE_USERS:
        OVERRIDE_MAINTENANCE_USERS.remove(target_user_id_str)

    save_user_data(user_data)
    save_global_stats()
    bot.reply_to(message, f"Đã xóa quyền override bảo trì của user ID `{target_user_id_str}`.")
    try:
        bot.send_message(int(target_user_id_str), "❌ Quyền bỏ qua trạng thái bảo trì game của bạn đã bị gỡ bỏ.")
    except Exception:
        pass

# --- Flask Routes cho Keep-Alive ---
@app.route('/')
def home():
    return "Bot is alive and running!"

@app.route('/health')
def health_check():
    return "OK", 200

# --- Khởi tạo bot và các luồng khi Flask app khởi động ---
@app.before_request
def start_bot_threads():
    global bot_initialized
    with bot_init_lock:
        if not bot_initialized:
            print("LOG: Đang khởi tạo luồng bot và dự đoán...")
            sys.stdout.flush()
            # Load initial data
            load_user_data()
            load_cau_patterns()
            load_codes()
            load_global_stats() # Load new global stats and maintenance info

            # Start prediction loop for each game in separate threads
            for game_name, stop_event in prediction_stop_events.items():
                prediction_thread = Thread(target=prediction_loop, args=(game_name, stop_event,))
                prediction_thread.daemon = True 
                prediction_thread.start()
                print(f"LOG: Luồng dự đoán cho {game_name.upper()} đã khởi động.")
            sys.stdout.flush()

            # Start bot polling in a separate thread
            polling_thread = Thread(target=bot.infinity_polling, kwargs={'none_stop': True})
            polling_thread.daemon = True 
            polling_thread.start()
            print("LOG: Luồng Telegram bot polling đã khởi động.")
            sys.stdout.flush()

            # Start keep-alive thread
            keep_alive_thread = Thread(target=keep_alive)
            keep_alive_thread.daemon = True
            keep_alive_thread.start()
            print("LOG: Luồng Keep-Alive đã khởi động.")
            sys.stdout.flush()
            
            bot_initialized = True

# --- Điểm khởi chạy chính cho Gunicorn/Render ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"LOG: Khởi động Flask app trên cổng {port}")
    sys.stdout.flush()
    app.run(host='0.0.0.0', port=port, debug=False)
