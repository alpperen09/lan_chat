import os
import sys
import json
import uuid
import time
import base64
import shutil
import ssl
import hmac
import hashlib
import datetime
import threading
import urllib.request
from flask import (Flask, render_template, request,
                   session, send_file, jsonify)
from flask_socketio import SocketIO, emit

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

MAX_CHARS_MB = 100
MAX_CHARS = 50_000
MAX_FILE_SIZE = MAX_CHARS_MB * 1024 * 1024
MAX_GIF_SIZE = 5 * 1024 * 1024

MESSAGE_COOLDOWN = 1.0
MEDIA_COOLDOWN = 3.0
REACT_COOLDOWN = 0.5
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW = 10.0
GIF_MAX_PER_MINUTE = 10

APP_DIR = os.path.abspath(os.path.dirname(__file__))
RUNTIME_DIR = os.path.join(APP_DIR, 'runtime')
HISTORY_FILE = os.path.join(RUNTIME_DIR, 'history.txt')
CONFIG_FILE = os.path.join(RUNTIME_DIR, 'config.json')
FILES_DIR = os.path.join(RUNTIME_DIR, 'files')
GIFS_DIR = os.path.join(APP_DIR, 'static/gifs')

os.makedirs(FILES_DIR, exist_ok=True)
os.makedirs(GIFS_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = '2134'
app.config['MAX_CONTENT_LENGTH'] = int(MAX_FILE_SIZE * 1.4)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000
socketio = SocketIO(app, max_http_buffer_size=int(MAX_FILE_SIZE * 1.4))

messages = []
active_users = set()
banned_usernames = set()
banned_ips = set()
banned_fingerprints = set()
user_ips = {}
user_sids = {}
user_fingerprints = {}
badnames = set()
user_last_message_time = {}
user_last_media_time = {}
user_last_react_time = {}
ip_login_attempts = {}
ip_gif_uploads = {}
server_stopped = False
reactions = {}
pinned_messages = set()
notifications_muted = False


def get_signature(data):
    return hmac.new(
        app.secret_key.encode(),
        data.encode(),
        hashlib.sha256
    ).hexdigest()


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            try:
                return json.load(f)
            except (json.JSONDecodeError, Exception):
                return {}
    return {}


def save_config():
    data = {
        "bad_words": list(badnames),
        "banned_fingerprints": list(banned_fingerprints),
        "banned_ips": list(banned_ips)
    }
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


config = load_config()
badnames = set(w.upper() for w in config.get("bad_words", []))
banned_fingerprints = set(config.get("banned_fingerprints", []))
banned_ips = set(config.get("banned_ips", []))


def broadcast_users():
    socketio.emit('active_users', {'users': list(active_users)})


def is_admin(req):
    return req.remote_addr == '127.0.0.1'


def get_server_stats():
    img_messages = [m for m in messages if m.get('type') == 'image']
    img_size = sum(len(m.get('data', '')) * 3 // 4 for m in img_messages)
    file_messages = [m for m in messages if m.get('type') == 'file']
    return {
        'message_count': len([m for m in messages if m.get('type') not in ('image', 'file')]),
        'char_count': sum(len(m.get('text', '')) for m in messages),
        'img_count': len(img_messages),
        'img_size': img_size,
        'file_count': len(file_messages)
    }


def check_login_rate(ip):
    now = time.time()
    attempts = ip_login_attempts.get(ip, [])
    attempts = [t for t in attempts if now - t < LOGIN_WINDOW]
    if len(attempts) >= LOGIN_MAX_ATTEMPTS:
        ip_login_attempts[ip] = attempts
        return False
    attempts.append(now)
    ip_login_attempts[ip] = attempts
    return True


def check_gif_rate(ip):
    now = time.time()
    uploads = ip_gif_uploads.get(ip, [])
    uploads = [t for t in uploads if now - t < 60.0]
    if len(uploads) >= GIF_MAX_PER_MINUTE:
        ip_gif_uploads[ip] = uploads
        return False
    uploads.append(now)
    ip_gif_uploads[ip] = uploads
    return True


@app.route('/')
def index():
    client_ip = request.remote_addr
    if client_ip in banned_ips:
        return render_template('blocked.html'), 403

    username = session.get('username', '')
    fp = session.get('fingerprint', '')

    if fp and fp in banned_fingerprints:
        session.clear()
        return render_template('blocked.html'), 403

    return render_template(
        'chat.html',
        username=username,
        is_admin=is_admin(request),
        max_file_size=MAX_FILE_SIZE
    )


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'ok': True})


@app.route('/gifs')
def list_gifs():
    try:
        valid_exts = ('.gif', '.webp', '.png', '.jpg', '.jpeg')
        print(f"DEBUG: GIFS_DIR = {GIFS_DIR}")
        print(f"DEBUG: GIFS_DIR exists = {os.path.exists(GIFS_DIR)}")
        files = [f for f in os.listdir(GIFS_DIR) if f.lower().endswith(valid_exts)]
        files.sort()
        print(f"DEBUG: Found GIF files: {files}")
        return jsonify({'gifs': files})
    except Exception as e:
        print(f"ERROR in list_gifs: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'gifs': []})


@app.route('/gifs/add', methods=['POST'])
def add_gif():
    if not is_admin(request):
        username = session.get('username', '')
        if not username:
            return jsonify({'ok': False, 'error': 'Giriş yapılmamış.'}), 403

    client_ip = request.remote_addr
    if not is_admin(request) and not check_gif_rate(client_ip):
        return jsonify({'ok': False, 'error': 'Çok fazla GIF yüklendi.'}), 429

    data = request.get_json(force=True)
    source = data.get('source', '').strip()
    b64data = data.get('data', '')
    ext_hint = data.get('ext', 'gif')

    if b64data:
        try:
            ext = ext_hint.lower() if ext_hint in ('gif', 'webp', 'png', 'jpg', 'jpeg') else 'gif'
            filename = f"{uuid.uuid4()}.{ext}"
            dest = os.path.join(GIFS_DIR, filename)
            raw = base64.b64decode(b64data)
            if len(raw) > MAX_GIF_SIZE:
                return jsonify({'ok': False, 'error': 'GIF 5MB sınırını aşıyor.'})
            with open(dest, 'wb') as f:
                f.write(raw)
            return jsonify({'ok': True, 'filename': filename})
        except Exception as e:
            return jsonify({'ok': False, 'error': f'Kaydetme hatası: {e}'})

    if not source:
        return jsonify({'ok': False, 'error': 'Kaynak belirtilmedi.'})

    if source.startswith(('http://', 'https://')):
        try:
            ext = source.split('?')[0].rsplit('.', 1)[-1].lower()
            if ext not in ('gif', 'webp', 'png', 'jpg', 'jpeg'):
                ext = 'gif'
            filename = f"{uuid.uuid4()}.{ext}"
            dest = os.path.join(GIFS_DIR, filename)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(source, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
            })
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                raw = resp.read(MAX_GIF_SIZE + 1)
            if len(raw) > MAX_GIF_SIZE:
                return jsonify({'ok': False, 'error': 'GIF 5MB sınırını aşıyor.'})
            with open(dest, 'wb') as f:
                f.write(raw)
            return jsonify({'ok': True, 'filename': filename})
        except Exception as e:
            return jsonify({'ok': False, 'error': f'İndirme hatası: {str(e)}'})
    else:
        local_path = source.replace('/', os.sep).replace('\\', os.sep)
        if not os.path.exists(local_path):
            return jsonify({'ok': False, 'error': 'Dosya bulunamadı.'})
        if os.path.getsize(local_path) > MAX_GIF_SIZE:
            return jsonify({'ok': False, 'error': 'GIF 5MB sınırını aşıyor.'})
        ext = local_path.rsplit('.', 1)[-1].lower()
        if ext not in ('gif', 'webp', 'png', 'jpg', 'jpeg'):
            return jsonify({'ok': False, 'error': 'Desteklenmeyen dosya formatı.'})
        filename = f"{uuid.uuid4()}.{ext}"
        dest = os.path.join(GIFS_DIR, filename)
        shutil.copy2(local_path, dest)
        return jsonify({'ok': True, 'filename': filename})


@app.route('/gifs/<filename>')
def serve_gif(filename):
    filename = os.path.basename(filename)
    path = os.path.join(GIFS_DIR, filename)
    if not os.path.exists(path):
        return 'Bulunamadı.', 404
    return send_file(path)


@app.route('/download/<file_id>')
def download_file(file_id):
    file_id = os.path.basename(file_id)
    meta = next((m for m in messages if m.get('type') == 'file'
                 and m.get('file_id') == file_id), None)
    if not meta:
        return 'Dosya bulunamadı.', 404
    file_path = os.path.join(FILES_DIR, file_id)
    if not os.path.exists(file_path):
        return 'Dosya bulunamadı.', 404
    return send_file(file_path, as_attachment=True, download_name=meta['filename'])


@app.route('/view/<file_id>')
def view_file(file_id):
    file_id = os.path.basename(file_id)
    meta = next((m for m in messages if m.get('type') == 'file'
                 and m.get('file_id') == file_id), None)
    if not meta:
        return 'Dosya bulunamadı.', 404
    file_path = os.path.join(FILES_DIR, file_id)
    if not os.path.exists(file_path):
        return 'Dosya bulunamadı.', 404
    return send_file(file_path, as_attachment=False, download_name=meta['filename'])


@app.route('/game')
def game():
    return render_template('game.html')


@socketio.on('set_username')
def handle_set_username(data):
    global server_stopped
    client_ip = request.remote_addr

    if client_ip in banned_ips:
        emit('username_result', {'ok': False, 'error': 'Erişiminiz engellenmiştir!'})
        return

    if not check_login_rate(client_ip):
        emit('username_result', {'ok': False, 'error': 'Çok fazla giriş denemesi.'})
        return

    if server_stopped:
        emit('username_result', {'ok': False, 'error': 'Sunucu yeni girişlere kapalı.'})
        return

    username = str(data.get('username', '')).strip()
    fingerprint = str(data.get('fingerprint', '')).strip()

    if not fingerprint or fingerprint in banned_fingerprints:
        emit('username_result', {'ok': False, 'error': 'Erişiminiz engellenmiştir!'})
        return

    if username == '':
        emit('username_result', {'ok': False, 'error': 'Kullanıcı adı boş olamaz!'})
        return

    if not (2 <= len(username) <= 16):
        emit('username_result', {'ok': False, 'error': 'Kullanıcı adı geçersiz uzunlukta!'})
        return

    if username in active_users:
        emit('username_result', {'ok': False, 'error': 'Bu kullanıcı adı kullanımda!'})
        return

    if username.upper() in badnames:
        emit('username_result', {'ok': False, 'error': 'Yasaklı kullanıcı adı!'})
        return

    if username in banned_usernames:
        emit('username_result', {'ok': False, 'error': 'Bu kullanıcı adı yasaklanmıştır!'})
        return

    active_ips = {ip for u, ip in user_ips.items() if u in active_users}
    if client_ip in active_ips:
        emit('username_result', {'ok': False, 'error': 'Bu IP ile zaten giriş yapılmış!'})
        return

    session['username'] = username
    session['fingerprint'] = fingerprint
    session['is_admin'] = is_admin(request)

    active_users.add(username)
    user_ips[username] = client_ip
    user_sids[username] = request.sid
    user_fingerprints[username] = fingerprint

    broadcast_users()
    sig = get_signature(username)
    emit('username_result', {
        'ok': True,
        'username': username,
        'signature': sig,
        'is_admin': session['is_admin']
    })


@socketio.on('rename_user')
def handle_rename_user(data):
    old_username = session.get('username', '')
    if not old_username:
        emit('rename_result', {'ok': False, 'error': 'Oturum bulunamadı.'})
        return

    new_username = str(data.get('username', '')).strip()
    fingerprint = str(data.get('fingerprint', '')).strip()

    if new_username == old_username:
        emit('rename_result', {'ok': False, 'error': 'Yeni ad eskiyle aynı.'})
        return

    if not (2 <= len(new_username) <= 16):
        emit('rename_result', {'ok': False, 'error': 'Geçersiz kullanıcı adı uzunluğu!'})
        return

    if new_username in active_users or new_username.upper() in badnames or new_username in banned_usernames:
        emit('rename_result', {'ok': False, 'error': 'Kullanıcı adı kullanılamaz!'})
        return

    active_users.discard(old_username)
    active_users.add(new_username)

    user_ips[new_username] = user_ips.pop(old_username, request.remote_addr)
    user_sids[new_username] = user_sids.pop(old_username, request.sid)
    user_fingerprints[new_username] = user_fingerprints.pop(old_username, fingerprint)

    if old_username in user_last_message_time:
        user_last_message_time[new_username] = user_last_message_time.pop(old_username)
    if old_username in user_last_media_time:
        user_last_media_time[new_username] = user_last_media_time.pop(old_username)

    for m in messages:
        if m.get('username') == old_username:
            m['username'] = new_username

    session['username'] = new_username
    broadcast_users()

    new_sig = get_signature(new_username)
    emit('rename_result', {
        'ok': True,
        'old_username': old_username,
        'new_username': new_username,
        'signature': new_sig
    })
    socketio.emit('user_renamed', {'old_username': old_username, 'new_username': new_username})


@socketio.on('message')
def handle_message(data):
    global messages, server_stopped, notifications_muted
    username = session.get('username', '')
    is_admin_user = session.get('is_admin', False)

    if not username:
        emit('needs_username')
        return

    msg = str(data.get('text', '')) if isinstance(data, dict) else str(data or '')
    client_sig = (data.get('signature') or '') if isinstance(data, dict) else ''

    expected_sig = get_signature(username)
    if not hmac.compare_digest(expected_sig, client_sig):
        emit('system_message', {'text': 'Güvenlik ihlali: Geçersiz imza! (kullanıcı adı değiştirmeyi dene)', 'type': 'error'})
        return

    msg_stripped = msg.strip()

    if msg_stripped.lower() == '/cls':
        emit('clear_own_screen')
        emit('system_message', {'text': 'Ekranınız temizlendi.', 'type': 'error'})
        return

    if msg_stripped.lower() == '/help':
        help_text = 'Komutlar => /cls'
        if is_admin_user:
            help_text = 'Komutlar => /cls, /clear, /ban <kullanici>, /unban <kullanici>, ' \
                        '/unban all, /bad <kelime>, /broadcast <mesaj>, /mute, /stop, /kill'
        emit('system_message', {'text': help_text, 'type': 'error'})
        return

    parts = msg_stripped.split(' ', 1)
    command = parts[0].lower()
    target = parts[1].strip() if len(parts) > 1 else ''

    admin_command_list = {
        '/kill', '/clear', '/stop', '/ban',
        '/unban', '/bad', '/broadcast', '/mute'
    }

    if command in admin_command_list:
        if not is_admin_user:
            emit('system_message', {'text': 'Yetkiniz yok.', 'type': 'error'})
            return

        if command == '/kill':
            socketio.emit('kill')
            threading.Timer(1.0, lambda: os._exit(0)).start()
            return

        if command == '/stop':
            server_stopped = not server_stopped
            durum = 'durduruldu' if server_stopped else 'açıldı'
            socketio.emit('system_message', {
                'text': f'[SISTEM] Sunucu girişi {durum}.',
                'type': 'error'
            })
            return

        if command == '/mute':
            notifications_muted = not notifications_muted
            durum = 'kapatıldı' if notifications_muted else 'açıldı'
            socketio.emit('set_mute', {'muted': notifications_muted})
            socketio.emit('system_message', {
                'text': f'[SISTEM] Bildirim sesleri {durum}.',
                'type': 'error'
            })
            return

        if command == '/clear':
            messages.clear()
            socketio.emit('all_messages_clear')
            socketio.emit('server_stats', get_server_stats())
            return

        if not target:
            emit('system_message', {'text': 'Hedef belirtilmedi.', 'type': 'error'})
            return

        if command == '/ban':
            if target == username:
                emit('system_message', {'text': 'Kendinizi banlayamazsınız.', 'type': 'error'})
                return
            banned_usernames.add(target)
            if target.upper() not in badnames:
                badnames.add(target.upper())
            if target in user_ips:
                banned_ips.add(user_ips[target])
            if target in user_fingerprints:
                banned_fingerprints.add(user_fingerprints[target])
            save_config()
            if target in active_users:
                active_users.discard(target)
                target_sid = user_sids.get(target)
                if target_sid:
                    socketio.emit('kicked', {'reason': 'Yasaklandınız.'}, to=target_sid)
                    user_sids.pop(target, None)
                broadcast_users()
            socketio.emit('system_message', {'text': f'[SISTEM] {target} yasaklandı.', 'type': 'ban'})

        elif command == '/unban':
            if target.lower() == 'all':
                banned_usernames.clear()
                banned_ips.clear()
                badnames.clear()
                banned_fingerprints.clear()
                socketio.emit('system_message', {'text': '[SISTEM] Tüm yasaklar kaldırıldı.', 'type': 'unban'})
            else:
                banned_usernames.discard(target)
                if target in user_ips:
                    banned_ips.discard(user_ips[target])
                badnames.discard(target.upper())
                if target in user_fingerprints:
                    banned_fingerprints.discard(user_fingerprints[target])
                socketio.emit('system_message', {'text': f'[SISTEM] {target} yasağı kaldırıldı.', 'type': 'unban'})
            save_config()

        elif command == '/bad':
            word = target.upper()
            if word in badnames:
                emit('system_message', {'text': f'"{word}" zaten listede.', 'type': 'error'})
                return
            badnames.add(word)
            save_config()
            emit('system_message', {'text': f'[SISTEM] "{word}" listeye eklendi.', 'type': 'ban'})
            matched = [u for u in list(active_users) if u.upper() == word]
            for u in matched:
                banned_usernames.add(u)
                active_users.discard(u)
                target_sid = user_sids.get(u)
                if target_sid:
                    socketio.emit('kicked', {'reason': 'Yasaklandınız.'}, to=target_sid)
                    user_sids.pop(u, None)
            if matched:
                broadcast_users()

        elif command == '/broadcast':
            socketio.emit('system_message', {'text': f'[DUYURU] {target}', 'type': 'broadcast'})
        return

    current_time = time.time()
    last_time = user_last_message_time.get(username, 0)
    last_media = user_last_media_time.get(username, 0)
    if current_time - last_time < MESSAGE_COOLDOWN or current_time - last_media < MESSAGE_COOLDOWN:
        emit('system_message', {'text': 'Lütfen 1 saniye bekleyin.', 'type': 'error'})
        return
    user_last_message_time[username] = current_time

    msg_upper = msg_stripped.upper()
    for word in badnames:
        if word in msg_upper:
            emit('system_message', {'text': 'Yasaklı kelime içeriyor.', 'type': 'error'})
            return

    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    message_dict = {
        'id': str(uuid.uuid4()),
        'username': username,
        'text': msg_stripped,
        'timestamp': timestamp
    }
    messages.append(message_dict)
    while messages and sum(len(m.get('text', '')) for m in messages) > MAX_CHARS:
        messages.pop(0)

    client_ip = request.remote_addr
    with open(HISTORY_FILE, 'a', encoding='utf-8') as f:
        f.write(f"{timestamp} - {client_ip} - {username}: {msg_stripped}\n")

    emit('message', message_dict, broadcast=True)
    socketio.emit('server_stats', get_server_stats())


@socketio.on('image')
def handle_image(data):
    username = session.get('username', '')
    if not username:
        emit('needs_username')
        return

    current_time = time.time()
    last_time = user_last_message_time.get(username, 0)
    last_media = user_last_media_time.get(username, 0)
    if current_time - last_time < MEDIA_COOLDOWN or current_time - last_media < MEDIA_COOLDOWN:
        emit('system_message', {'text': 'Lütfen 3 saniye bekleyin.', 'type': 'error'})
        return
    user_last_media_time[username] = current_time

    image_data = data.get('data', '')
    mime_type = data.get('mime_type', 'image/png')

    try:
        raw = base64.b64decode(image_data)
        if len(raw) > MAX_FILE_SIZE:
            emit('system_message', {'text': 'Görsel boyutu çok büyük.', 'type': 'error'})
            return
    except Exception:
        emit('system_message', {'text': 'Geçersiz görsel verisi.', 'type': 'error'})
        return

    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    message_dict = {
        'id': str(uuid.uuid4()),
        'username': username,
        'type': 'image',
        'data': image_data,
        'mime_type': mime_type,
        'timestamp': timestamp
    }
    messages.append(message_dict)
    while messages and sum(len(m.get('text', '')) for m in messages) > MAX_CHARS:
        messages.pop(0)

    emit('image', message_dict, broadcast=True)
    socketio.emit('server_stats', get_server_stats())


@socketio.on('file')
def handle_file(data):
    username = session.get('username', '')
    if not username:
        emit('needs_username')
        return

    current_time = time.time()
    if current_time - user_last_media_time.get(username, 0) < MEDIA_COOLDOWN:
        emit('system_message', {'text': 'Lütfen 3 saniye bekleyin.', 'type': 'error'})
        return
    user_last_media_time[username] = current_time

    filename = data.get('filename', '').strip()
    file_data = data.get('data', '')
    mime_type = data.get('mime_type', 'application/octet-stream')

    if not filename:
        emit('system_message', {'text': 'Geçersiz dosya adı.', 'type': 'error'})
        return

    try:
        raw = base64.b64decode(file_data)
        if len(raw) > MAX_FILE_SIZE:
            emit('system_message', {'text': 'Dosya çok büyük.', 'type': 'error'})
            return
    except Exception:
        emit('system_message', {'text': 'Veri hatası.', 'type': 'error'})
        return

    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'bin'
    file_id = f"{uuid.uuid4()}.{ext}"
    file_path = os.path.join(FILES_DIR, file_id)
    with open(file_path, 'wb') as f:
        f.write(raw)

    message_dict = {
        'id': str(uuid.uuid4()),
        'username': username,
        'type': 'file',
        'filename': filename,
        'file_id': file_id,
        'file_size': len(raw),
        'mime_type': mime_type,
        'timestamp': datetime.datetime.now().strftime("%H:%M:%S")
    }
    messages.append(message_dict)
    emit('file', message_dict, broadcast=True)
    socketio.emit('server_stats', get_server_stats())


@socketio.on('react')
def handle_react(data):
    username = session.get('username', '')
    if not username:
        return

    current_time = time.time()
    if current_time - user_last_react_time.get(username, 0) < REACT_COOLDOWN:
        return
    user_last_react_time[username] = current_time

    msg_id = data.get('msg_id', '')
    emoji = data.get('emoji', '')

    if not msg_id or not emoji:
        return

    reactions.setdefault(msg_id, {}).setdefault(emoji, set())
    users = reactions[msg_id][emoji]

    if username in users:
        users.discard(username)
    else:
        users.add(username)

    if not users:
        del reactions[msg_id][emoji]
    if not reactions.get(msg_id):
        reactions.pop(msg_id, None)

    payload = {
        'msg_id': msg_id,
        'reactions': {em: list(u) for em, u in reactions.get(msg_id, {}).items()}
    }
    socketio.emit('reaction_update', payload)


@socketio.on('pin_message')
def handle_pin_message(data):
    if not session.get('is_admin', False):
        emit('pin_result', {'ok': False, 'error': 'Sadece admin sabitleme yapabilir.'})
        return

    msg_id = data.get('msg_id', '')
    if not msg_id:
        emit('pin_result', {'ok': False, 'error': 'Geçersiz mesaj ID.'})
        return

    found = False
    for msg in messages:
        if msg.get('id') == msg_id:
            found = True
            break

    if not found:
        emit('pin_result', {'ok': False, 'error': 'Mesaj bulunamadı.'})
        return

    if msg_id in pinned_messages:
        pinned_messages.discard(msg_id)
        is_pinned = False
    else:
        pinned_messages.add(msg_id)
        is_pinned = True

    emit('pin_result', {'ok': True, 'msg_id': msg_id, 'is_pinned': is_pinned})


@socketio.on('connect')
def handle_connect():
    client_ip = request.remote_addr
    if client_ip in banned_ips:
        return False

    fp = session.get('fingerprint', '')
    if fp and fp in banned_fingerprints:
        return False

    username = session.get('username')
    if username:
        user_sids[username] = request.sid
        if username not in active_users:
            active_users.add(username)
            user_ips[username] = client_ip
            if fp:
                user_fingerprints[username] = fp

    for message in messages:
        m_type = message.get('type')
        if m_type == 'image':
            emit('image', message)
        elif m_type == 'file':
            emit('file', message)
        else:
            emit('message', message)

    all_reactions = {
        mid: {em: list(u) for em, u in emojis.items()}
        for mid, emojis in reactions.items()
    }
    emit('all_reactions', all_reactions)
    emit('all_pinned_messages', {'pinned_messages': list(pinned_messages)})
    emit('active_users', {'users': list(active_users)})
    broadcast_users()

    if is_admin(request):
        emit('server_stats', get_server_stats())
    emit('set_mute', {'muted': notifications_muted})


@socketio.on('disconnect')
def handle_disconnect():
    username = session.get('username')
    if username in active_users:
        active_users.remove(username)
    user_sids.pop(username, None)

    if username and username not in banned_usernames:
        user_ips.pop(username, None)
        if username in user_fingerprints and user_fingerprints[username] not in banned_fingerprints:
            user_fingerprints.pop(username, None)

    for d in [user_last_message_time, user_last_media_time, user_last_react_time]:
        d.pop(username, None)

    broadcast_users()


if __name__ == '__main__':
    socketio.run(app, host="0.0.0.0", port=80, debug=True)