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
            "status": "setup",
            "turn_left": 3,  # 各部屋での残り行動回数（タイムリミット）
            "log_history": [],
            "pending_dice": None
        },
        "players": {},
        "current_event": {
            "title": "ロビー",
            "description": "ゲームが初期化されました。`!キャラ作成 [希望の役職]` と発言して参加してください。\n全員の作成が終わったら `!ゲーム開始` と発言してください。",
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
    # LLMが勝手にダイス判定の結果を描写しようとした場合のハルシネーション迎撃
    if "🎲" in commands_text or "判定" in commands_text and "目標値" in commands_text:
        if "!propose_dice" not in commands_text:
            print("[Warning] LLMの不正ダイス出力を検知。ブロックしました。")
            commands_text = "!chat gm ⚠️ (GMが思考エラーを起こしました。行動を再宣言してください)"

    lines = commands_text.strip().split('\n')
    db = get_db_snapshot()
    stage = db["session"]["stage_count"]
    event_title = db["current_event"].get("title", "不明な部屋")
    turn_left = db["session"].get("turn_left", 3)
    
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
            
            # ── HP / MP・SP の状態バーを自動生成 ──
            status_bars = []
            for p_key, p_val in db["players"].items():
                p_stats = p_val["stats"]
                resource_name = "MP" if "魔法" in p_val["class"] or "神官" in p_val["class"] else "SP"
                status_bars.append(f"👤 **{p_key}** [{p_val['class']}] HP:{p_stats['HP']}/20 | {resource_name}:{p_stats.get(resource_name, 10)}/10")
            status_str = "\n".join(status_bars)

            if stage > 0:
                header = (
                    f"━ [{speaker}] ━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📍 **STAGE {stage}/20 : {event_title}** (⏳部屋の残り活動限界: **{turn_left}** 回)\n"
                    f"{status_str}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                )
            else:
                header = f"━ [{speaker}] ━━━━━━━━━━\n"
            await channel.send(f"{header}{msg}")
            
        elif command in ["set", "add", "sub"]:
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
            
        elif command == "propose_dice":
            parts = args_str.split(' ', 3)
            if len(parts) >= 4:
                p_name, stat_name, diff_level, action_desc = parts[0], parts[1].upper(), parts[2].lower(), parts[3]
                
                player_data = db["players"].get(p_name, {})
                base_stat = player_data.get("stats", {}).get(stat_name, 10)
                
                # 1d100に完全準拠した難易度自動計算（もうGMに数字は決めさせない）
                if diff_level == "easy":
                    target_val = min(base_stat * 5, 95)
                    diff_str = "簡単 (能力値×5)"
                elif diff_level == "hard":
                    target_val = max(base_stat * 1, 5)
                    diff_str = "困難 (能力値×1)"
                else:
                    target_val = max(min(base_stat * 3, 90), 10)
                    diff_str = "普通 (能力値×3)"
                
                db["session"]["pending_dice"] = {
                    "player": p_name,
                    "stat": stat_name,
                    "target": target_val,
                    "description": action_desc
                }
                write_db(db)
                
                confirm_msg = (
                    f"⚠️ **【ダイス判定の確認】**\n"
                    f"**{p_name}** の「{action_desc}」の判定を提案します。\n\n"
                    f"🎲 使用ステータス: **{stat_name}** (現在値: {base_stat})\n"
                    f"🧭 判定難易度: {diff_str}\n"
                    f"🎯 成功条件: **{target_val} 以下** (1d100)\n\n"
                    f"本当に実行する場合は **`!実行`** とチャットしてください。\n"
                    f"交渉や創意工夫のロールプレイでアプローチを変えれば、難易度が下がる（例: normal ➡️ easy）可能性があります！"
                )
                await channel.send(f"```yaml\n{confirm_msg}\n```")

# --- Gemini API 接続 ---
def call_gemini_gm(player_messages, db_snapshot):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key: return ""
    client = genai.Client(api_key=api_key)
    
    stage = db_snapshot.get("session", {}).get("stage_count", 1)
    
    if stage <= 5:
        diff_prompt = "【現在の難易度：レベル1】easyまたはnormal中心。基本行動（観察など）はダイス不要。"
    elif stage <= 10:
        diff_prompt = "【現在の難易度：レベル2】罠やギミック。判定は主にnormal。"
    else:
        diff_prompt = "【現在の難易度：レベル3】hard中心。特異スキルの応用やとんちでeasyに緩和せよ。"

    system_instruction = f"""
    あなたはテキストローグライクRPGの「ゲームマスター（GM）」兼「システムコントローラー」です。
    あなたの出力は、すべて「!」から始まるコマンド言語のみで構成されなければなりません。

    # 絶対厳守ルール：ダイス判定の委任
    1. あなたは自身でダイスを振った結果（例: 出目、成功、失敗というテキスト）を絶対に直接描写してはなりません。
    2. 判定が必要な場合は、必ず `!propose_dice <プレイヤー名> <STR/INT/DEX> <easy/normal/hard> <行動の要約>` コマンドのみを出力し、システムの確認ダイアログに処理を委ねてください。

    # リソースと行動制限ルール
    1. プレイヤーが強力な「スキル」や「魔法」を使用した場合、必ずその描写と共に、!sub を用いて対象の MP または SP を 2〜3 ポイント消費させてください（例: !sub players.asanebou_benk.stats.MP 3）。
    2. プレイヤーが罠にかかったり、ダメージを受けた場合は、必ず !sub を用いて HP を消費させてください。
    3. 1ステージは「1つの部屋規模」です。プレイヤーが何か具体的な行動（探索、スキル使用、扉を開けるなど）を決定するたびに、!sub session.turn_left 1 を実行して部屋のタイムリミットを削ってください。もし turn_left が 0 になっている場合は、次の!chatで環境ダメージや敵の襲撃などのペナルティイベントを発生させてください。

    # 出力コマンド仕様
    - !chat gm <メッセージ> : GMの描写。
    - !propose_dice <プレイヤー名> <ステータス> <easy/normal/hard> <行動の要約> : ダイス確認ダイアログの生成。
    - !set / !add / !sub <JSONパス> <値> : データの更新。

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
        print(f"Gemini Error: {e}")
        return ""

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
    
    if msg == "!reset":
        init_db(force=True)
        await message.channel.send("🧹 `System: ゲームデータを完全にリセットしました。ロビーに戻ります。`")
        return

    db = get_db_snapshot()
    status = db["session"]["status"]

    if msg == "!実行" and status == "playing":
        pending = db["session"].get("pending_dice")
        if not pending:
            await message.channel.send("⚠️ `System: 現在確認待ちのダイス判定はありません。`")
            return
        
        roll = random.randint(1, 100)
        target = pending["target"]
        is_success = roll <= target
        result_str = "【成功】" if is_success else "【失敗】"
        
        dice_msg = f"🎲 **{pending['player']} の {pending['stat']} 判定** (目標値: {target})\n出目: **{roll}** ➡️ **{result_str}**"
        await message.channel.send(f"```diff\n{'+ ' if is_success else '- '}{dice_msg}\n```")
        
        db["session"]["pending_dice"] = None
        write_db(db)
        
        if is_success:
            # 成功したら行動ターンを1減らす
            db["session"]["turn_left"] = max(db["session"].get("turn_left", 3) - 1, 0)
            write_db(db)
            llm_output = call_gemini_gm([f"システム通知: プレイヤー {pending['player']} の「{pending['description']}」の判定結果は 出目{roll} で 【成功】 でした。物語を進める描写を出力してください。"], db)
            if llm_output:
                await execute_commands(llm_output, message.channel)
        else:
            # 失敗しても行動ターンは1減る
            db["session"]["turn_left"] = max(db["session"].get("turn_left", 3) - 1, 0)
            write_db(db)
            fail_warn = (
                f"❌ **判定失敗...**\n"
                f"{pending['player']} の「{pending['description']}」は失敗に終わった。\n"
                f"部屋の残り活動限界が減少した！別の方法を試すか、交渉などのロールプレイで打開策を考えてください。"
            )
            await message.channel.send(f"```diff\n- {fail_warn}\n```")
            
            # ターン減少をLLMに同期させるための空たたき
            llm_output = call_gemini_gm([f"システム通知: {pending['player']}の行動は失敗し、部屋の残り活動限界が {db['session']['turn_left']} になりました。現在の部屋の緊迫感を!chatで描写してください。"], db)
            if llm_output:
                await execute_commands(llm_output, message.channel)
        return

    if msg.startswith("!キャラ作成"):
        if status != "setup" and status != "character_creation":
            await message.channel.send("⚠️ `System: 現在はキャラクター作成フェーズではありません。`")
            return
        job = msg.replace("!キャラ作成", "").strip()
        if not job: job = "冒険者"
        
        # HP20, MP/SPは10で一律固定
        resource_name = "MP" if "魔法" in job or "神官" in job else "SP"
        stats = {"HP": 20, resource_name: 10, "STR": random.randint(6, 18), "INT": random.randint(6, 18), "DEX": random.randint(6, 18)}
        db["players"][p_name] = {"class": job, "skills": {"name": "未覚醒", "effect": "開始時に決定"}, "stats": stats}
        db["session"]["status"] = "character_creation"
        write_db(db)
        await message.channel.send(f"🎲 **{p_name}** が **{job}** としてエントリーしました！\n能力値: `STR:{stats['STR']} / INT:{stats['INT']} / DEX:{stats['DEX']}`")
        return

    if msg == "!ゲーム開始":
        if status != "character_creation":
            await message.channel.send("⚠️ `System: 参加者が1人以上キャラ作成を完了した状態で !ゲーム開始 を宣言してください。`")
            return
        db["session"]["status"] = "playing"
        db["session"]["stage_count"] = 1
        db["session"]["turn_left"] = 3 # 1部屋あたり3行動のリミット
        write_db(db)
        await message.channel.send("⚔️ `System: 運命の歯車が回り出した。ゲームを開始します…`")
        message_queue.append(f"システム通知: ゲームが開始されました。全プレイヤーの固有スキル（魔法使いなら『ファイアボール（消費: 3 MP）』、戦士なら『渾身の一撃（消費: 3 SP）』のようなリソース消費型）を!setで保存し、ステージ1の最初の部屋の描写を始めてください。")
        asyncio.create_task(process_queue(message.channel))
        return

    if status == "playing":
        if db["session"]["pending_dice"]:
            db["session"]["pending_dice"] = None
            write_db(db)
            
        formatted_msg = f"[{p_name}]: {msg}"
        message_queue.append(formatted_msg)
        if not is_processing:
            asyncio.create_task(process_queue(message.channel))

if __name__ == "__main__":
    keep_alive()
    client.run(os.environ.get("DISCORD_TOKEN"))
