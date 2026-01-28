from flask import Flask, render_template, request, session, redirect, url_for
from flask_socketio import SocketIO, send, emit
import datetime

app = Flask(__name__)
app.secret_key = '2134'
socketio = SocketIO(app)

messages = []

@app.route('/')
def index():
    return render_template('login.html')

@app.route('/chat', methods=['POST'])
def chat():
    username = request.form['username']
    session['username'] = username
    print(f"'{username}' giriş yaptı.")
    return redirect(url_for('chat_page', username=username))

@app.route('/chat_page')
def chat_page():
    username = session['username']
    return render_template('chat.html', username=username)

@socketio.on('message')
def handle_message(msg):
    username = session.get('username', 'Bilinmeyen')
    full_message = f"{username}: {msg}"
    print(datetime.datetime.now().strftime("%H:%M:%S - ") + full_message)

    messages.append(full_message)

    with open('history.txt', 'a', encoding='utf-8') as file:
        file.write(datetime.datetime.now().strftime("%H:%M:%S - ") + full_message + '\n')

    send(full_message, broadcast=True)

@socketio.on('connect')
def handle_connect():
    username = session.get('username')
    if username:
        for message in messages:
            emit('message', message)

@socketio.on('image')
def handle_image(image_data):
    emit('image', {'username': session['username'], 'image': image_data}, broadcast=True)

if __name__ == '__main__':
    # socketio.run(app, host="192.168.1.105", port=2626, debug=True)
    socketio.run(app, host="0.0.0.0", port=80, debug=True)
