import discord
import asyncio
import json
import re
import os
from flask import Flask
from threading import Thread

# --- Flask Webサーバー（Renderのスリープ防止用） ---
app = Flask('')

@app.route('/')
def home():
    return "GM Bot is Alive!"

def run_web():
    # Renderは自動的にPORT環境変数を割り振るため、それを読み込む（デフォルトは8080）
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_web)
    t.start()

# --- JSONデータベース初期化 ---
DB_FILE = "database.json"
message_queue = []
is_processing = False

def init_db():
    if not os.path.exists(DB_FILE):
        initial_data = {
            "session": {"stage_count": 1, "log_history": ["ゲームが開始された。"]},
            "player_character": {
                "name": "未設定",
                "stats": {"HP": 20, "STR": 10}
            }
        }
        with open(DB_FILE, 'w', encoding='utf-8') as f:
            json.dump(initial_data, f, indent=2, ensure_ascii=False)

init_db()

# --- JSON操作関数（ドット記法対応） ---
def update_json_value(path, value, operator="set"):
    with open(DB_FILE, 'r+', encoding='utf-8') as f:
        data = json.load(f)
        keys = path.split('.')
        current = data
        for key in keys[:-1]:
            current = current.setdefault(key, {})
        last_key = keys[-1]
        
        if str(value).isdigit():
            value = int(value)

        if operator == "set":
            current[last_key] = value
        elif operator == "add":
            current[last_key] = current.get(last_key, 0) + value
        elif operator == "sub":
            current[last_key] = current.get(last_key, 0) - value

        f.seek(0)
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.truncate()

# --- コマンド実行エンジン ---
async def execute_commands(commands_text, channel):
    lines = commands_text.strip().split('\n')
    for line in lines:
        line = line.strip()
        if not line.startswith('!'):
            continue
            
        print(f"[Command] 実行: {line}")
        match = re.match(r'!([a-z]+)\s+(.*)', line)
        if not match:
            continue
            
        command, args_str = match.groups()
        
        if command == "chat":
            sub_args = args_str.split(' ', 1)
            speaker = sub_args[0]
            msg = sub_args[1] if len(sub_args) > 1 else ""
            await channel.send(f"**[{speaker.upper()}]**: {msg}")
            
        elif command in ["set", "add", "sub"]:
            sub_args = args_str.split(' ', 1)
            path = sub_args[0]
            val = sub_args[1] if len(sub_args) > 1 else 0
            update_json_value(path, val, operator=command)
            await channel.send(f"⚙️ `System: DBの {path} を {command} ({val}) しました。`")

# --- 擬似LLM（テスト用ダミー） ---
def mock_llm_engine(player_messages):
    combined = " ".join(player_messages)
    if "開始" in combined:
        return "!set player_character.name 勇者アルフレッド\n!chat gm ゲームが開始された。\n!chat gm ステージ 1 に進みます。"
    elif "攻撃" in combined:
        return "!sub player_character.stats.HP 3\n!chat gm モンスターの反撃！アルフレッドは3ダメージを受けた！"
    elif "回復" in combined:
        return "!add player_character.stats.HP 5\n!chat gm 聖なる光が包む。HPが5回復した。"
    return ""

# --- メッセージキュー処理 ---
async def process_queue(channel):
    global message_queue, is_processing
    is_processing = True
    await asyncio.sleep(1.0) # 1秒バッファリング
    
    current_batch = message_queue.copy()
    message_queue.clear()
    is_processing = False
    
    llm_output = mock_llm_engine(current_batch)
    if llm_output:
        await execute_commands(llm_output, channel)

# --- Discord Bot イベントハンドラ ---
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f"🤖 GM Bot 起動完了: {client.user}")

@client.event
async def on_message(message):
    if message.author == client.user:
        return
    message_queue.append(message.content)
    if not is_processing:
        asyncio.create_task(process_queue(message.channel))

# --- 起動処理 ---
if __name__ == "__main__":
    # Webサーバーを別スレッドで起動
    keep_alive()
    
    # Renderの環境変数からトークンを取得してBotを起動
    TOKEN = os.environ.get("DISCORD_TOKEN")
    if TOKEN:
        client.run(TOKEN)
    else:
        print("エラー: 環境変数 'DISCORD_TOKEN' が設定されていません。")
