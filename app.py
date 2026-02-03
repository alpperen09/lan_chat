from flask import Flask, render_template, request, session, redirect, url_for
from flask_socketio import SocketIO, send, emit
import datetime

app = Flask(__name__)
app.secret_key = '2134'
socketio = SocketIO(app)

messages = []
active_users = set()


def broadcast_users():
    socketio.emit('active_users', list(active_users))


@app.route('/')
def index():
    return render_template('login.html')


@app.route('/chat', methods=['POST'])
def chat():
    username = request.form['username']

    if username in active_users:
        return render_template(
            'login.html',
            error="Bu kullanıcı adı zaten kullanımda!"
        )

    session['username'] = username
    active_users.add(username)
    broadcast_users()

    print(f"'{username}' giris yapti.")
    return redirect(url_for('chat_page'))


@app.route('/chat_page')
def chat_page():
    if 'username' not in session:
        return redirect(url_for('index'))
    
    username = session['username']
    if username not in active_users:
        active_users.add(username)
        broadcast_users()

    return render_template('chat.html', username=username)


@socketio.on('message')
def handle_message(msg):
    username = session.get('username', 'Bilinmeyen')
    full_message = f"{username}: {msg}"
    timestamp = datetime.datetime.now().strftime("%H:%M:%S - ")

    messages.append(full_message)

    with open('history.txt', 'a', encoding='utf-8') as f:
        f.write(timestamp + full_message + '\n')

    send(full_message, broadcast=True)


@socketio.on('connect')
def handle_connect():
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


if __name__ == '__main__':
    socketio.run(app, host="0.0.0.0", port=80, debug=True)