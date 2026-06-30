import discord
import asyncio
import json
import re
import os
from flask import Flask
from threading import Thread
# Google GenAI SDKのインポート
from google import genai
from google.genai import types

# --- Flask Webサーバー（Renderのスリープ防止用） ---
app = Flask('')

@app.route('/')
def home():
    return "GM Bot is Alive and Learning!"

def run_web():
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
                "class": "未設定",
                "skills": {"name": "未設定", "effect": "未設定"},
                "stats": {"HP": 20, "STR": 10, "INT": 10, "DEX": 10}
            },
            "current_event": {
                "type": "none",
                "title": "始まりの地",
                "description": "まだ冒険は始まっていない。『!ゲーム開始』とチャットしてキャラクターを作成しよう。",
                "truth": "なし",
                "status": "resolved"
            }
        }
        with open(DB_FILE, 'w', encoding='utf-8') as f:
            json.dump(initial_data, f, indent=2, ensure_ascii=False)

init_db()

def get_db_snapshot():
    with open(DB_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

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

# --- 本物のGemini APIエンジン ---
def call_gemini_gm(player_messages, db_snapshot):
    # 環境変数からAPIキーを取得してクライアント初期化
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("エラー: GEMINI_API_KEY が設定されていません。")
        return ""
        
    client = genai.Client(api_key=api_key)
    
    # AIへの超厳格なシステム指示書（プロンプト）
    system_instruction = """
    あなたはテキストローグライクRPGの「ゲームマスター（GM）」兼「システムコントローラー」です。
    あなたは人間に対して自然言語で直接返答してはなりません。あなたの出力は、すべて以下に定義する「!」から始まるコマンド言語のみで構成されなければなりません。

    # 動作原則
    1. 介入の閾値: プレイヤーが単なる雑談をしている場合は、何も出力してはならない（空文字を返せ）。プレイヤーが「ゲーム開始」「行動」「探索」「NPCへの会話」を行った時のみコマンドを出力せよ。
    2. PCへの不干渉: プレイヤーキャラクター(PC)のセリフや行動、心理をあなたが勝手に描写してはならない。
    3. ダイスの主権: 行動判定が必要な場合、あなた自身がダイスを振るのではなく、!chatコマンドを用いてプレイヤーに「ダイスロール（例: 1d100<=DEX）」を要求せよ。

    # 出力コマンド仕様
    - !chat <送信先(gmまたはnpc名)> <メッセージ> : DiscordにGMの描写やNPCのセリフを送信する。
    - !set <JSONパス> <値> : JSONデータベースの値を書き換える。
    - !add / !sub <JSONパス> <数値> : 数値の増減。

    # イベントルール
    - プレイヤーが「開始」と言ったら、!set でランダムな能力値（STR, INT, DEXなど）と役職に応じた特異スキルを決定し、!chat で世界観を導入せよ。
    - 各イベントでは具体的な「解法（真相）」を想定し、プレイヤーの行動がそれに合致しているか、あるいは妥当な水平思考かを厳格にジャッジせよ。
    - 難易度勾配: 現在のstage_count（1〜100）に応じて、進むほど複雑なギミックを要求せよ。
    """

    # LLMに渡す現在のコンテキストの構築
    user_content = f"""
    --- 現在のデータベース状態 ---
    {json.dumps(db_snapshot, indent=2, ensure_ascii=False)}

    --- プレイヤーの直前の発言（1秒間のバッファ） ---
    {" / ".join(player_messages)}
    """

    try:
        # 高速・安価・プログラミングに強い gemini-2.5-flash を使用
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=user_content,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.2, # ハルシネーションを抑えるため低めに設定
            ),
        )
        return response.text if response.text else ""
    except Exception as e:
        print(f"Gemini API エラー: {e}")
        return ""

# --- メッセージキュー処理 ---
async def process_queue(channel):
    global message_queue, is_processing
    is_processing = True
    await asyncio.sleep(1.0) # 1秒バッファリング
    
    current_batch = message_queue.copy()
    message_queue.clear()
    is_processing = False
    
    # 最新のDB状態を取得
    db_snapshot = get_db_snapshot()
    
    # Gemini APIを呼び出してコマンドを取得
    llm_output = call_gemini_gm(current_batch, db_snapshot)
    
    if llm_output:
        await execute_commands(llm_output, channel)

# --- Discord Bot イベントハンドラ ---
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f"🤖 AI GM Bot 起動完了: {client.user}")

@client.event
async def on_message(message):
    if message.author == client.user:
        return
    message_queue.append(message.content)
    if not is_processing:
        asyncio.create_task(process_queue(message.channel))

if __name__ == "__main__":
    keep_alive()
    TOKEN = os.environ.get("DISCORD_TOKEN")
    if TOKEN:
        client.run(TOKEN)
    else:
        print("エラー: 環境変数 'DISCORD_TOKEN' が設定されていません。")
