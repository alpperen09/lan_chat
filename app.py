from flask import Flask, render_template, request, session, redirect, url_for
from flask_socketio import SocketIO, send, emit
import datetime
import os

app = Flask(__name__)
app.secret_key = '2134'
socketio = SocketIO(app)

messages = []
active_users = set()
banned_usernames = set()
banned_ips = set()
user_ips = {}
user_sids = {}
badnames = set()

with open(".\\badnames.txt", "a+", encoding="utf-8") as f:
    f.seek(0)
    badnames = set(line.strip().upper() for line in f if line.strip())

def broadcast_users():
    socketio.emit('active_users', list(active_users))

def is_admin(req):
    return req.remote_addr == '127.0.0.1'

@app.route('/')
def index():
    return render_template('login.html')

@app.route('/chat', methods=['POST'])
def chat():
    global badnames

    client_ip = request.remote_addr
    username = str(request.form['username']).strip()

    if client_ip in banned_ips:
        return render_template('login.html', error="Erişiminiz engellenmiştir!")

    if username == '':
        return render_template('login.html', error="Bu kullanıcı adı geçersiz!")

    if username in active_users:
        return render_template('login.html', error="Bu kullanıcı adı zaten kullanımda!")

    if username.upper() in badnames:
        return render_template('login.html', error="Yasaklı kullanıcı adı!")

    if username in banned_usernames:
        return render_template('login.html', error="Bu kullanıcı adı yasaklanmıştır!")

    if len(username) < 2:
        return render_template('login.html', error="Kısa kullanıcı adı!")

    if len(username) > 10:
        return render_template('login.html', error="Uzun kullanıcı adı!")

    session['username'] = username
    session['is_admin'] = is_admin(request)
    active_users.add(username)
    user_ips[username] = client_ip
    broadcast_users()

    print(f"'{username}' giris yapti. (IP: {client_ip}, Admin: {session['is_admin']})")
    return redirect(url_for('chat_page'))


@app.route('/chat_page')
def chat_page():
    if 'username' not in session:
        return redirect(url_for('index'))

    username = session['username']
    if username not in active_users:
        active_users.add(username)
        broadcast_users()

    return render_template('chat.html', username=username, is_admin=session.get('is_admin', False))


@socketio.on('message')
def handle_message(msg):
    username = session.get('username', 'Bilinmeyen')
    is_admin_user = session.get('is_admin', False)

    if msg == '/kill' or msg.startswith('/ban ') or msg.startswith('/unban ') or msg.startswith('/add '):
        if not is_admin_user:
            emit('system_message', {'text': 'Bu komutu kullanma yetkiniz yok.', 'type': 'error'})
            return

        if msg == '/kill':
            socketio.emit('kill')
            import threading
            threading.Timer(1.0, lambda: os._exit(0)).start()
            return

        parts = msg.split(' ', 1)
        command = parts[0]
        target = parts[1].strip() if len(parts) > 1 else ''

        if not target:
            emit('system_message', {'text': 'Hedef belirtilmedi.', 'type': 'error'})
            return

        if command == '/ban':
            if target == username:
                emit('system_message', {'text': 'Kendinizi banlayamazsınız.', 'type': 'error'})
                return

            banned_usernames.add(target)
            word = target.upper()
            if word not in badnames:
                badnames.add(word)
                with open('.\\badnames.txt', 'a', encoding='utf-8') as f:
                    f.write('\n' + word)

            if target in user_ips:
                banned_ips.add(user_ips[target])

            if target in active_users:
                active_users.discard(target)
                target_sid = user_sids.get(target)
                if target_sid:
                    socketio.emit('kicked', {'reason': 'Yasaklandınız.'}, to=target_sid)
                    user_sids.pop(target, None)
                broadcast_users()

            system_text = f'[SİSTEM] {target} yasaklandı.'
            socketio.emit('system_message', {'text': system_text, 'type': 'ban'})
            print(f"Admin '{target}' kullaniciyi banladi.")

        elif command == '/unban':
            if target.lower() == 'all':
                banned_usernames.clear()
                banned_ips.clear()
                badnames.clear()
                with open('.\\badnames.txt', 'w', encoding='utf-8') as f:
                    f.write('')
                socketio.emit('system_message', {'text': '[SİSTEM] Tüm yasaklar kaldırıldı.', 'type': 'unban'})
                print("Admin tum banlari kaldirdi.")
            else:
                banned_usernames.discard(target)
                if target in user_ips:
                    banned_ips.discard(user_ips[target])
                word = target.upper()
                if word in badnames:
                    badnames.discard(word)
                    with open('.\\badnames.txt', 'r', encoding='utf-8') as f:
                        lines = [l.strip() for l in f if l.strip() and l.strip().upper() != word]
                    with open('.\\badnames.txt', 'w', encoding='utf-8') as f:
                        f.write('\n'.join(lines))
                system_text = f'[SİSTEM] {target} yasağı kaldırıldı.'
                socketio.emit('system_message', {'text': system_text, 'type': 'unban'})
                print(f"Admin '{target}' kullanicisinın banini kaldirdi.")

        elif command == '/add':
            word = target.upper()
            if word in badnames:
                emit('system_message', {'text': f'"{word}" zaten listede.', 'type': 'error'})
                return
            badnames.add(word)
            with open('.\\badnames.txt', 'a', encoding='utf-8') as f:
                f.write('\n' + word)
            emit('system_message', {'text': f'[SİSTEM] "{word}" yasaklı isim listesine eklendi.', 'type': 'ban'})
            print(f"Admin '{word}' badnames listesine ekledi.")
            matched = [u for u in active_users if u.upper() == word]
            for u in matched:
                banned_usernames.add(u)
                if u in user_ips:
                    banned_ips.add(user_ips[u])
                active_users.discard(u)
                target_sid = user_sids.get(u)
                if target_sid:
                    socketio.emit('kicked', {'reason': 'Yasaklandınız.'}, to=target_sid)
                    user_sids.pop(u, None)
            if matched:
                broadcast_users()

        return

    full_message = f"{username}: {msg}"
    timestamp = datetime.datetime.now().strftime("%H:%M:%S - ")

    messages.append(full_message)

    with open('history.txt', 'a', encoding='utf-8') as f:
        f.write(timestamp + full_message + '\n')

    send(full_message, broadcast=True)


@socketio.on('connect')
def handle_connect():
    client_ip = request.remote_addr
    if client_ip in banned_ips:
        return False

    username = session.get('username')
    if username:
        user_sids[username] = request.sid

    for message in messages:
        emit('message', message)
    emit('active_users', list(active_users))
    broadcast_users()


@socketio.on('disconnect')
def handle_disconnect():
    username = session.get('username')
    if username in active_users:
        active_users.remove(username)
        broadcast_users()
        print(f"'{username}' cikis yapti")
    user_sids.pop(username, None)


if __name__ == '__main__':
    socketio.run(app, host="0.0.0.0", port=80, debug=True)