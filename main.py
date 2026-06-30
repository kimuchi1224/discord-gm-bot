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

# --- call_gemini_gm 関数のアップデート版 ---
def call_gemini_gm(player_messages, db_snapshot):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("エラー: GEMINI_API_KEY が設定されていません。")
        return ""
        
    client = genai.Client(api_key=api_key)
    
    # 1. 現在のステージ数を取得（デフォルトは1）
    stage = db_snapshot.get("session", {}).get("stage_count", 1)
    
    # 2. ステージ数に応じた「難易度勾配指示（メタ・プロンプト）」の動的生成
    if stage <= 5:
        difficulty_instruction = """
        【現在の難易度：レベル1（基本・チュートリアル）】
        - プレイヤーが直感的、かつストレートな行動（戦う、開けるなど）を行えば、基本的には成功、または順当なダイス判定（成功率高め）で突破させてください。
        - 複雑な裏のギミックや、即死するような罠は絶対に配置しないでください。
        """
    elif stage <= 10:
        difficulty_instruction = """
        【現在の難易度：レベル2（リソース消費とトラップ）】
        - 単純な力押しだけでは解決できない「罠」や「障害」を導入してください。
        - 突破にはHPの減少や、アイテムの消費、あるいはリスクを伴うダイス判定を要求してください。
        ```json
        """
    elif stage <= 19:
        difficulty_instruction = f"""
        【現在の難易度：レベル3（水平思考とスキル応用）】
        - 非常に重要：普通に攻撃したり進もうとすると、絶対に失敗するか大ダメージを受けるイベントを生成してください。
        - 突破には、プレイヤーが持つ特異スキル（現在：{json.dumps(db_snapshot.get('player_character', {}).get('skills', {}), ensure_ascii=False)}）を意外な形で応用するか、部屋の環境を利用した「水平思考（とんち）」が必要です。
        - プレイヤーから機転の利いた提案があれば、!set や !chat を用いて、裏の正解（truth）を書き換えながら、ドラマチックに解決させてください。
        """
    else:
        difficulty_instruction = """
        【現在の難易度：レベル4（ステージ20・第1目標ボス戦）】
        - 20ステージ目の大ボスです。ボスには「特定の弱点」や「特定の行動手順」を裏で設定（truthに明記）してください。
        - プレイヤーがその弱点を見破る、あるいはこれまでの経験を活かした行動をとるまで、ボスは倒せません。総力戦を描写してください。
        """

    # 3. メインのシステム指示書（動的指示を埋め込む）
    system_instruction = f"""
    あなたはテキストローグライクRPGの「ゲームマスター（GM）」兼「システムコントローラー」です。
    あなたは人間に対して自然言語で直接返答してはなりません。あなたの出力は、すべて以下に定義する「!」から始まるコマンド言語のみで構成されなければなりません。

    # 動作原則
    1. 介入の閾値: プレイヤーが単なる雑談をしている場合は、何も出力してはならない（空文字）。プレイヤーが「ゲーム開始」「行動」「探索」「NPCへの会話」を行った時のみコマンドを出力せよ。
    2. PCへの不干渉: プレイヤーキャラクター(PC)のセリフや行動、心理をあなたが勝手に描写してはならない。
    3. ダイスの主権: 行動判定が必要な場合、あなた自身がダイスを振るのではなく、!chatコマンドを用いてプレイヤーに「ダイスロール（例: 1d100<=DEX）」を要求せよ。

    # 出力コマンド仕様
    - !chat <送信先(gmまたはnpc名)> <メッセージ> : DiscordにGMの描写やNPCのセリフを送信する。
    - !set <JSONパス> <値> : JSONデータベースの値を書き換える。
    - !add / !sub <JSONパス> <数値> : 数値の増減。
    - !new <対象> <設定オブジェクト> : current_event を新しいイベント（謎、戦闘、罠、店など）で上書きする。

    # ゲーム進行ルール
    - プレイヤーが「開始」と言ったら、!set でランダムな能力値（STR, INT, DEXなど）と、役職（戦士、魔法使い、僧侶、学者など）に応じた『●を●する能力』という形式の特異スキルを決定し、!chat で世界観を導入せよ。
    - 各イベント生成（!new）時は、必ず具体的な「解法（真相・truth）」を内部データに明記すること。

    {difficulty_instruction}
    """

    user_content = f"""
    --- 現在のデータベース状態 ---
    {json.dumps(db_snapshot, indent=2, ensure_ascii=False)}

    --- プレイヤーの直前の発言（1秒間のバッファ） ---
    {" / ".join(player_messages)}
    """

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=user_content,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.3,
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
