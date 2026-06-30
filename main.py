import discord
import asyncio
import json
import re
import os
import random
from flask import Flask
from threading import Thread
from google import genai
from google.genai import types

# --- Flask Webサーバー ---
app = Flask('')
@app.route('/')
def home(): return "GM Bot is Online!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    Thread(target=run_web).start()

# --- JSONデータベース制御 ---
DB_FILE = "database.json"
message_queue = []
is_processing = False

def get_default_db():
    return {
        "session": {
            "stage_count": 0,
            "status": "setup", # setup -> character_creation -> playing -> gameover/clear
            "log_history": []
        },
        "players": {},
        "current_event": {
            "title": "ロビー",
            "description": "ゲームが初期化されました。`!キャラ作成 [希望の役職]` と発言して参加してください。（例: !キャラ作成 魔法使い）\n全員の作成が終わったら `!ゲーム開始` と発言してください。",
            "truth": "なし",
            "status": "resolved"
        }
    }

def init_db(force=False):
    if force or not os.path.exists(DB_FILE):
        with open(DB_FILE, 'w', encoding='utf-8') as f:
            json.dump(get_default_db(), f, indent=2, ensure_ascii=False)

init_db()

def get_db_snapshot():
    with open(DB_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def write_db(data):
    with open(DB_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# --- コマンド実行エンジン ---
async def execute_commands(commands_text, channel):
    lines = commands_text.strip().split('\n')
    db = get_db_snapshot()
    stage = db["session"]["stage_count"]
    event_title = db["current_event"].get("title", "不明な部屋")
    
    for line in lines:
        line = line.strip()
        if not line.startswith('!'):
            continue
            
        print(f"[System Command] {line}")
        match = re.match(r'!([a-z_]+)\s+(.*)', line)
        if not match:
            continue
        command, args_str = match.groups()
        
        if command == "chat":
            sub_args = args_str.split(' ', 1)
            speaker = sub_args[0].upper()
            msg = sub_args[1] if len(sub_args) > 1 else ""
            
            # ステージ可視化ヘッダーを自動付与
            if stage > 0:
                header = f"━ [{speaker}] ━━━━━━━━━━\n📍 **STAGE {stage}/20 : {event_title}**\n━━━━━━━━━━━━━━━━━━\n"
            else:
                header = f"━ [{speaker}] ━━━━━━━━━━\n"
            await channel.send(f"{header}{msg}")
            
        elif command in ["set", "add", "sub"]:
            # 内部データの更新処理（ドット記法簡易版）
            sub_args = args_str.split(' ', 1)
            path, val = sub_args[0], sub_args[1]
            if val.isdigit(): val = int(val)
            
            keys = path.split('.')
            current = db
            for key in keys[:-1]:
                current = current.setdefault(key, {})
            
            if command == "set": current[keys[-1]] = val
            elif command == "add": current[keys[-1]] = current.get(keys[-1], 0) + val
            elif command == "sub": current[keys[-1]] = current.get(keys[-1], 0) - val
            write_db(db)
            
        elif command == "req_dice":
            # !req_dice Yamada STR 14 のような形式
            parts = args_str.split(' ')
            if len(parts) >= 3:
                p_name, stat_name, target_val = parts[0], parts[1], int(parts[2])
                roll = random.randint(1, 100)
                is_success = roll <= target_val
                result_str = "【成功】" if is_success else "【失敗】"
                
                dice_msg = f"🎲 **{p_name} の {stat_name} 判定** (目標値: {target_val})\n出目: **{roll}** ➡️ **{result_str}**"
                await channel.send(f"```diff\n{'+ ' if is_success else '- '}{dice_msg}\n```")
                
                # ダイス結果を即座にLLMに伝えて次の描写を要求するトリガー
                db = get_db_snapshot()
                llm_output = call_gemini_gm([f"システム通知: {p_name}の{stat_name}判定ダイスの結果は {roll} で {result_str} でした。この結果に基づくゲーム展開を!chat等で出力してください。"], db)
                if llm_output:
                    await execute_commands(llm_output, channel)

# --- Gemini API 接続 ---
def call_gemini_gm(player_messages, db_snapshot):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key: return ""
    client = genai.Client(api_key=api_key)
    
    stage = db_snapshot["session"]["stage_count"]
    
    # 難易度勾配の定義
    if stage <= 5:
        diff_prompt = "難易度低。ストレートな行動で突破可能。即死トラップ厳禁。"
    elif stage <= 10:
        diff_prompt = "難易度中。罠やHP/アイテムリソースを消費するギミックを導入。"
    else:
        diff_prompt = "難易度高。普通に戦うと全滅する。プレイヤーの特異スキルの応用や、部屋の環境を利用した水平思考（とんち）を要求せよ。"

    system_instruction = f"""
    あなたはテキストローグライクRPGのゲームマスター(GM)です。返答は「!」から始まるシステムコマンドのみで行ってください。

    # ルール
    1. 1ステージは「1つの部屋規模の探索スペース」としてコンパクトに定義せよ。
    2. 新しい部屋（ステージ）に進む際は、!set current_event.title [部屋名] と !set current_event.description [描写] を行い、!set current_event.truth に「裏の正解やギミック、ペナルティ」を明確に記述せよ。
    3. 行動判定が必要な場合、あなた自身がダイスを振るのではなく、必ず「!req_dice [プレイヤー名] [ステータス名] [目標値]」コマンドを出力してシステムにダイスを要求せよ。目標値はプレイヤーのステータス（1〜20程度）を基準に決定せよ。
    4. パーティプレイ対応：複数のプレイヤー情報がデータベースにあります。行動を宣言したプレイヤーの名前を正しく認識してジャッジせよ。

    # 出力コマンド
    - !chat gm <メッセージ> : GMの描写やNPCのセリフ
    - !req_dice <プレイヤー名> <ステータス> <目標値> : ダイス判定をシステムに要求
    - !set / !add / !sub <パス> <値> : データの更新
    
    現在の難易度方針: {diff_prompt}
    """

    user_content = f"--- DB ---\n{json.dumps(db_snapshot, indent=2, ensure_ascii=False)}\n\n--- プレイヤー発言 ---\n{'/'.join(player_messages)}"

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=user_content,
            config=types.GenerateContentConfig(system_instruction=system_instruction, temperature=0.3),
        )
        return response.text if response.text else ""
    except Exception as e:
        print(f"Gemini Error: {e}"); return ""

# --- メッセージキュー処理 ---
async def process_queue(channel):
    global message_queue, is_processing
    is_processing = True
    await asyncio.sleep(1.0)
    
    current_batch = message_queue.copy()
    message_queue.clear()
    is_processing = False
    
    db = get_db_snapshot()
    llm_output = call_gemini_gm(current_batch, db)
    if llm_output:
        await execute_commands(llm_output, channel)

# --- Discord イベントハンドラ ---
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready(): print(f"🤖 AI GM Bot Online: {client.user}")

@client.event
async def on_message(message):
    if message.author == client.user: return
    
    msg = message.content.strip()
    p_name = message.author.name
    
    # ── システム直轄コマンド（LLMを介さない決定論的処理） ──
    
    # 1. 全初期化・リスタート
    if msg == "!reset":
        init_db(force=True)
        await message.channel.send("🧹 `System: ゲームデータを完全にリセットしました。ロビーに戻ります。`")
        return

    db = get_db_snapshot()
    status = db["session"]["status"]

    # 2. キャラクター作成フェーズ
    if msg.startswith("!キャラ作成"):
        if status != "setup" and status != "character_creation":
            await message.channel.send("⚠️ `System: 現在はキャラクター作成フェーズではありません。`")
            return
        
        job = msg.replace("!キャラ作成", "").strip()
        if not job: job = "冒険者"
        
        # パラメータのランダム決定 (3d6方式換算、1〜18)
        stats = {
            "HP": 20,
            "STR": random.randint(6, 18),
            "INT": random.randint(6, 18),
            "DEX": random.randint(6, 18)
        }
        
        db["players"][p_name] = {
            "class": job,
            "skills": {"name": "未覚醒", "effect": "ゲーム開始時にLLMにより決定されます"},
            "stats": stats
        }
        db["session"]["status"] = "character_creation"
        write_db(db)
        
        await message.channel.send(f"🎲 **{p_name}** が **{job}** としてエントリーしました！\n能力値: `STR:{stats['STR']} / INT:{stats['INT']} / DEX:{stats['DEX']}`")
        return

    # 3. ゲーム本編の開始
    if msg == "!ゲーム開始":
        if status != "character_creation":
            await message.channel.send("⚠️ `System: 参加者が1人以上キャラ作成を完了した状態で !ゲーム開始 を宣言してください。`")
            return
        
        db["session"]["status"] = "playing"
        db["session"]["stage_count"] = 1
        write_db(db)
        
        await message.channel.send("⚔️ `System: 運命の歯車が回り出した。ゲームを開始します…`")
        # LLMに最初の部屋と各自の特異スキルを生成させるトリガーを引く
        message_queue.append(f"システム通知: ゲームが本番開始されました。登録されている全プレイヤーの役職に応じた固有スキル（●を●する技術/武器/魔法など）を確定させて !set で保存し、ステージ1の最初の部屋の描写を始めてください。")
        asyncio.create_task(process_queue(message.channel))
        return

    # ── 通常のゲームプレイ（LLMによるアドリブジャッジ） ──
    if status == "playing":
        # 誰が発言したかを明記してキューに格納
        formatted_msg = f"[{p_name}]: {msg}"
        message_queue.append(formatted_msg)
        if not is_processing:
            asyncio.create_task(process_queue(message.channel))

if __name__ == "__main__":
    keep_alive()
    client.run(os.environ.get("DISCORD_TOKEN"))
