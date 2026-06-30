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
            "log_history": [],
            "pending_dice": None  # 確認待ちのダイス情報を格納
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
            
            if stage > 0:
                header = f"━ [{speaker}] ━━━━━━━━━━\n📍 **STAGE {stage}/20 : {event_title}**\n━━━━━━━━━━━━━━━━━━\n"
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
            # !propose_dice Yamada STR 14 鉄の扉を力任せにこじ開ける
            parts = args_str.split(' ', 3)
            if len(parts) >= 4:
                p_name, stat_name, target_val, action_desc = parts[0], parts[1], int(parts[2]), parts[3]
                
                # 確認待ちダイスとしてDBに一時保存
                db["session"]["pending_dice"] = {
                    "player": p_name,
                    "stat": stat_name,
                    "target": target_val,
                    "description": action_desc
                }
                write_db(db)
                
                # プレイヤーへの確認メッセージ
                confirm_msg = (
                    f"⚠️ **【ダイス判定の確認】**\n"
                    f"**{p_name}** が行おうとした「{action_desc}」には成功判定が必要です。\n\n"
                    f"🎲 使用ステータス: **{stat_name}**\n"
                    f"🎯 成功条件: **{target_val} 以下** (1d100)\n\n"
                    f"本当に実行する場合は **`!実行`** とチャットしてください。\n"
                    f"（取りやめる場合は、別の行動を普通にチャットしてください）"
                )
                await channel.send(f"```yaml\n{confirm_msg}\n```")

# --- Gemini API 接続 ---
def call_gemini_gm(player_messages, db_snapshot):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key: return ""
    client = genai.Client(api_key=api_key)
    
    stage = db_snapshot["session"]["stage_count"]
    
    # 1. 現在のステージ数を取得（デフォルトは1）
    stage = db_snapshot.get("session", {}).get("stage_count", 1)
    
    # 2. ステージ数に応じた「難易度勾配指示（メタ・プロンプト）」の動的生成
    if stage <= 5:
        diff_prompt = """
        【現在の難易度：レベル1（基本・チュートリアル）】
        - プレイヤーが直感的、かつストレートな行動（戦う、開けるなど）を行えば、基本的には成功、または順当なダイス判定（成功率高め）で突破させてください。
        - 複雑な裏のギミックや、即死するような罠は絶対に配置しないでください。
        """
    elif stage <= 10:
        diff_prompt = """
        【現在の難易度：レベル2（リソース消費とトラップ）】
        - 単純な力押しだけでは解決できない「罠」や「障害」を導入してください。
        - 突破にはHPの減少や、アイテムの消費、あるいはリスクを伴うダイス判定を要求してください。
        ```json
        """
    elif stage <= 19:
        diff_prompt = f"""
        【現在の難易度：レベル3（水平思考とスキル応用）】
        - 非常に重要：普通に攻撃したり進もうとすると、絶対に失敗するか大ダメージを受けるイベントを生成してください。
        - 突破には、プレイヤーが持つ特異スキル（現在：{json.dumps(db_snapshot.get('player_character', {}).get('skills', {}), ensure_ascii=False)}）を意外な形で応用するか、部屋の環境を利用した「水平思考（とんち）」が必要です。
        - プレイヤーから機転の利いた提案があれば、!set や !chat を用いて、裏の正解（truth）を書き換えながら、ドラマチックに解決させてください。
        """
    else:
        diff_prompt = """
        【現在の難易度：レベル4（ステージ20・第1目標ボス戦）】
        - 20ステージ目の大ボスです。ボスには「特定の弱点」や「特定の行動手順」を裏で設定（truthに明記）してください。
        - プレイヤーがその弱点を見破る、あるいはこれまでの経験を活かした行動をとるまで、ボスは倒せません。総力戦を描写してください。
        """

    # 3. メインのシステム指示書（動的指示を埋め込む）
    system_instruction = f"""
    あなたはテキストローグライクRPGの「ゲームマスター（GM）」兼「システムコントローラー」です。
    あなたは人間に対して自然言語で直接返答してはなりません。あなたの出力は、すべて以下に定義する「!」から始まるコマンド言語のみで構成されなければなりません。

    # 動作原則
    1. 1ステージは「1つの部屋規模の探索スペース」としてコンパクトに定義せよ。
    2. 新しい部屋（ステージ）に進む際は、!set current_event.title [部屋名] と !set current_event.description [描写] を行い、!set current_event.truth に「裏の正解やギミック、ペナルティ」を明確に記述せよ。
    3. パーティプレイ対応：複数のプレイヤー情報がデータベースにあります。行動を宣言したプレイヤーの名前を正しく認識してジャッジせよ。
    4. 介入の閾値: プレイヤーが単なる雑談をしている場合は、何も出力してはならない（空文字）。プレイヤーが「ゲーム開始」「行動」「探索」「NPCへの会話」を行った時のみコマンドを出力せよ。
    5. PCへの不干渉: プレイヤーキャラクター(PC)のセリフや行動、心理をあなたが勝手に描写してはならない。

    # 重要ルール：ダイス判定の事前確認
    プレイヤーがダイスロールの必要性がある行動（例：攻撃する、罠を解除する、隠し扉を探すなど）を選択した場合、あなた自身が勝手に結果を描写したり、即座にダイスを振らせてはいけません。
    必ず、以下の `!propose_dice` コマンドを使用して、プレイヤーに難易度と条件を提示し、実行するか確認してください。

    # 出力コマンド仕様
    - !chat <送信先(gmまたはnpc名)> <メッセージ> : DiscordにGMの描写やNPCのセリフを送信する。
    - !propose_dice <プレイヤー名> <ステータス> <目標値> <行動内容の要約> : ダイス判定の条件をプレイヤーに提示して確認を求める。目標値はプレイヤーのステータス（6〜18程度）を基準に設定すること。
    - !set <JSONパス> <値> : JSONデータベースの値を書き換える。
    - !add / !sub <JSONパス> <数値> : 数値の増減。
    - !new <対象> <設定オブジェクト> : current_event を新しいイベント（謎、戦闘、罠、店など）で上書きする。

    # ゲーム進行ルール
    - プレイヤーが「開始」と言ったら、!set でランダムな能力値（STR, INT, DEXなど）と、役職（戦士、魔法使い、僧侶、学者など）に応じた『●を●する能力』という形式の特異スキルを決定し、!chat で世界観を導入せよ。
    - 各イベント生成（!new）時は、必ず具体的な「解法（真相・truth）」を内部データに明記すること。

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
    
    # ── システム直轄コマンド ──
    if msg == "!reset":
        init_db(force=True)
        await message.channel.send("🧹 `System: ゲームデータを完全にリセットしました。ロビーに戻ります。`")
        return

    db = get_db_snapshot()
    status = db["session"]["status"]

    # 確定ダイスの実行コマンド
    if msg == "!実行" and status == "playing":
        pending = db["session"].get("pending_dice")
        if not pending:
            await message.channel.send("⚠️ `System: 現在確認待ちのダイス判定はありません。`")
            return
        
        # ダイスを振る
        roll = random.randint(1, 100)
        target = pending["target"]
        is_success = roll <= target
        result_str = "【成功】" if is_success else "【失敗】"
        
        dice_msg = f"🎲 **{pending['player']} の {pending['stat']} 判定** (目標値: {target})\n出目: **{roll}** ➡️ **{result_str}**"
        await message.channel.send(f"```diff\n{'+ ' if is_success else '- '}{dice_msg}\n```")
        
        # 確認待ち状態をクリア
        db["session"]["pending_dice"] = None
        write_db(db)
        
        # 結果をLLMにフィードバック
        llm_output = call_gemini_gm([f"システム通知: プレイヤー {pending['player']} が提案を承認し、ダイスを実行しました。「{pending['description']}」の判定結果は 出目{roll} で {result_str} でした。この結果に基づくその後の部屋の展開・描写を出力してください。"], db)
        if llm_output:
            await execute_commands(llm_output, message.channel)
        return

    if msg.startswith("!キャラ作成"):
        if status != "setup" and status != "character_creation":
            await message.channel.send("⚠️ `System: 現在はキャラクター作成フェーズではありません。`")
            return
        job = msg.replace("!キャラ作成", "").strip()
        if not job: job = "冒険者"
        
        stats = {"HP": 20, "STR": random.randint(6, 18), "INT": random.randint(6, 18), "DEX": random.randint(6, 18)}
        db["players"][p_name] = {"class": job, "skills": {"name": "未覚醒", "effect": "ゲーム開始時にLLMにより決定されます"}, "stats": stats}
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
        write_db(db)
        await message.channel.send("⚔️ `System: 運命の歯車が回り出した。ゲームを開始します…`")
        message_queue.append(f"システム通知: ゲームが本番開始されました。登録されている全プレイヤーの役職に応じた固有スキル（●を●する技術/武器/魔法など）を確定させて !set で保存し、ステージ1の最初の部屋の描写を始めてください。")
        asyncio.create_task(process_queue(message.channel))
        return

    # ── 通常のゲームプレイ ──
    if status == "playing":
        # 新しい通常発言があった場合、確認待ちのダイスは自動でキャンセル（上書き）される
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
