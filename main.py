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

# 職業と固定スキルの定義テーブル（Geminiの捏造を防止）
JOB_PRESETS = {
    "戦士": {"skill_name": "渾身の一撃", "effect": "大ダメージの物理攻撃（消費: 3 SP）", "resource": "SP"},
    "魔法使い": {"skill_name": "ファイアボール", "effect": "激しい炎を放つ魔法（消費: 3 MP）", "resource": "MP"},
    "盗賊": {"skill_name": "隠密・罠解除", "effect": "罠の発見や解除の難易度を下げる（消費: 2 SP）", "resource": "SP"},
    "神官": {"skill_name": "ヒール", "effect": "味方一人のHPを5回復する（消費: 3 MP）", "resource": "MP"}
}

def get_default_db():
    return {
        "session": {
            "stage_count": 0,
            "status": "setup",
            "turn_left": 4,
            "log_history": [],  # GMの描写履歴を保存するバッファ
            "pending_dice": None
        },
        "players": {},
        "current_event": {
            "title": "ロビー",
            "description": "ゲームが初期化されました。`!キャラ作成 [希望の役職] [スキル:技名]` と発言して参加してください。\n全員の作成が終わったら `!ゲーム開始` と発言してください。",
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
    if "🎲" in commands_text or "判定" in commands_text and "目標値" in commands_text:
        if "!propose_dice" not in commands_text:
            print("[Warning] LLMの不正ダイス出力を検知。ブロックしました。")
            commands_text = "!chat gm ⚠️ (思考エラーを検知しました。プレイヤーは行動を再宣言してください)"

    if "!chat" not in commands_text and "!propose_dice" not in commands_text:
        commands_text += f"\n!chat gm …不気味な静寂が満ちている。(次の行動をどうぞ)"

    lines = commands_text.strip().split('\n')
    db = get_db_snapshot()
    stage = db["session"]["stage_count"]
    event_title = db["current_event"].get("title", "不明な部屋")
    turn_left = db["session"].get("turn_left", 4)
    
    for line in lines:
        try:
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
                
                if speaker == "GM":
                    history = db["session"].get("log_history", [])
                    # データベース破損対策: 文字列になっていた場合は強制的にリストへ修復
                    if isinstance(history, str):
                        history = [history]
                    elif not isinstance(history, list):
                        history = []
                        
                    history.append(msg)
                    if len(history) > 10:
                        history.pop(0)
                    db["session"]["log_history"] = history
                    write_db(db)

                status_bars = []
                for p_key, p_val in db["players"].items():
                    p_stats = p_val["stats"]
                    res_type = p_val["skills"]["resource"]
                    # インベントリ表示用の補助表示
                    inv = p_val.get("inventory", [])
                    inv_str = f" | 💼:{', '.join(inv)}" if inv else ""
                    status_bars.append(f"👤 **{p_key}** [{p_val['class']}] HP:{p_stats['HP']}/20 | {res_type}:{p_stats.get(res_type, 10)}/10{inv_str}\n   ↳ 技: **{p_val['skills']['name']}** ({p_val['skills']['effect']})")
                status_str = "\n".join(status_bars)

                if stage > 0:
                    header = (
                        f"━ [{speaker}] ━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📍 **STAGE {stage}/20 : {event_title}** (⏳部屋のリミット: **{turn_left}** 行動)\n"
                        f"{status_str}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    )
                else:
                    header = f"━ [{speaker}] ━━━━━━━━━━\n"
                await channel.send(f"{header}{msg}")
                
            elif command in ["set", "add", "sub"]:
                sub_args = args_str.split(' ', 1)
                path, val = sub_args[0], sub_args[1]
                
                # ダブルクォーテーションやシングルクォーテーションを自動サニタイズ
                if val.startswith('"') and val.endswith('"'):
                    val = val[1:-1]
                elif val.startswith("'") and val.endswith("'"):
                    val = val[1:-1]
                
                # 数値型へ変換可能なら変換
                if val.isdigit(): 
                    val = int(val)
                
                # 配列インデックス指示(例: log_history[2])などの表記を安全に除去
                path = re.sub(r'\[\d+\]', '', path)
                
                keys = path.split('.')
                current = db
                for key in keys[:-1]:
                    current = current.setdefault(key, {})
                
                target_key = keys[-1]
                
                if command == "set": 
                    current[target_key] = val
                elif command == "add":
                    # 対象がリスト、または追加するオブジェクトが文字列（非数値）である場合は、リストへ追加（append）として安全に処理
                    if isinstance(current.get(target_key), list):
                        current[target_key].append(val)
                    elif target_key == "inventory" or not isinstance(val, int):
                        if target_key not in current or not isinstance(current[target_key], list):
                            current[target_key] = []
                        current[target_key].append(val)
                    else:
                        current[target_key] = current.get(target_key, 0) + val
                elif command == "sub": 
                    if isinstance(val, int):
                        current[target_key] = current.get(target_key, 0) - val
                write_db(db)
                
            elif command == "propose_dice":
                parts = args_str.split(' ', 3)
                if len(parts) >= 4:
                    p_names_str, stat_name, diff_level, action_desc = parts[0], parts[1].upper(), parts[2].lower(), parts[3]
                    
                    action_desc = action_desc.replace('"', '').replace("'", "").strip()
                    p_names = [p.strip() for p in p_names_str.split(',')]
                    base_stat = 0
                    valid_p_names = []
                    
                    for p in p_names:
                        if p in db["players"]:
                            valid_p_names.append(p)
                            p_stat = db["players"][p]["stats"].get(stat_name, 10)
                            if isinstance(p_stat, (int, float)) and p_stat > base_stat:
                                base_stat = p_stat
                    
                    if not valid_p_names:
                        valid_p_names = list(db["players"].keys())[:1]
                        base_stat = 10
                    if stat_name not in ["STR", "INT", "DEX"]:
                        stat_name = "STR"
                    
                    coop_bonus = 0
                    if len(valid_p_names) > 1:
                        coop_bonus = 3 
                        base_stat += coop_bonus
                    
                    if diff_level == "easy":
                        target_val = min(base_stat * 5, 95)
                        diff_str = "簡単 (能力値×5)"
                    elif diff_level == "hard":
                        target_val = max(base_stat * 1, 5)
                        diff_str = "困難 (能力値×1)"
                    else:
                        target_val = max(min(base_stat * 3, 90), 10)
                        diff_str = "普通 (能力値×3)"
                    
                    display_p_name = ", ".join(valid_p_names)
                    db["session"]["pending_dice"] = {
                        "player": display_p_name,
                        "stat": stat_name,
                        "target": target_val,
                        "description": action_desc
                    }
                    write_db(db)
                    
                    coop_str = f"🤝 **【協力行動ボーナス適用！】** (能力値ベースに補正 +{coop_bonus})\n" if coop_bonus > 0 else ""
                    confirm_msg = (
                        f"⚠️ **【ダイス判定の確認】**\n"
                        f"**{display_p_name}** の「{action_desc}」の判定を提案します。\n\n"
                        f"{coop_str}"
                        f"🎲 使用ステータス: **{stat_name}** (基準能力値: {base_stat})\n"
                        f"🧭 判定難易度: {diff_str}\n"
                        f"🎯 成功条件: **{target_val} 以下** (1d100)\n\n"
                        f"本当に実行する場合は **`!実行`** とチャットしてください。\n"
                        f"さらにアプローチを工夫すれば、自動成功に切り替わる可能性もあります！"
                    )
                    await channel.send(f"```yaml\n{confirm_msg}\n```")
        except Exception as line_error:
            # 各システムコマンド実行時のエラーをトラップし、システムクラッシュを完全に回避
            print(f"[System Error] Failed to execute '{line}': {line_error}")
            await channel.send(f"⚠️ `System Error: コマンド '{line}' の実行中に問題が発生しました。そのままゲームを続けてください。`")

# --- Gemini API 接続 ---
def call_gemini_gm(player_messages, db_snapshot):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key: return ""
    client = genai.Client(api_key=api_key)
    
    stage = db_snapshot.get("session", {}).get("stage_count", 1)
    
    if stage <= 5:
        diff_prompt = "【難易度: レベル1】easy/normal中心。探索可能な具体的オブジェクト（例: 『埃をかぶった木箱』『白骨化した死体』『怪しい壁の隙間』など）を最低3つ常に配置し、調べれば確実に手がかりやアイテムが出るようにせよ。"
    elif stage <= 10:
        diff_prompt = "【難易度: レベル2】罠や戦闘。リソースを削るギミックを導入。"
    else:
        diff_prompt = "【難易度: レベル3】hard中心。特異スキルを機転を利かせて応用させよ。"

    system_instruction = f"""
    あなたは本格派テキストローグライクRPGの「ゲームマスター（GM）」です。
    あなたの出力は、すべて「!」から始まるコマンド仕様のみで構成されなければなりません。
    必ずプレイヤーへの描写として `!chat gm <描写内容>` を出力に含めてください。メッセージを空にしてはなりません。

    # 絶対厳守ルール：一貫性と状態管理の復元
    1. あなたが過去に出力した描写、開示したアイテム、部屋に存在するオブジェクトの情報は、渡される `log_history` および `current_event` に完全に同期していなければなりません。「さっき見つかったと描写したアイテム」を、次のターンで「存在しない」などと言って矛盾を起こすことは絶対に許されません。
    2. 新しい部屋に進んだ時は必ず `!set current_event.title <部屋名>`, `!set current_event.description <状況説明>`, `!set current_event.truth <部屋の隠された真相や隠しアイテムの場所>` を実行し、データを同期してください。
    3. リスクのない単なる「部屋の観察」「落ちているものを調べる」行動には、絶対にダイスを要求せず、!chat gm で結果を即座に開示してください。
    4. プレイヤー全員が一通り行動を終えた、または大きなアクションを1回起こしたと判断した場合、必ず `!sub session.turn_left 1` を出力して部屋のリミットを1減らしてください。システム側は自動でターンを減らしません。あなた自身がカウントをコントロールしてください。
    5. ターンリミット（session.turn_left）が 0 になった場合、!chat gm で「部屋の罠の発動」や「モンスターの奇襲」を発生させ、プレイヤーのHPに固定ダメージ（!sub players.名前.stats.HP 3 など）を与えた上で、強制的に状況を変化させて物語を進めてください。手詰まりのまま放置してはなりません。

    # 重要ルール：ダイス判定の事前確認
    1. プレイヤーがダイスロールの必要性がある行動（例：攻撃する、罠を解除する、隠し扉を探すなど）を選択した場合、あなた自身が勝手に結果を描写したり、即座にダイスを振らせてはいけません。
    2. 必ず、以下の `!propose_dice` コマンドを使用して、プレイヤーに難易度と条件を提示し、実行するか確認してください。
       書式: `!propose_dice <プレイヤー名> <STR|INT|DEX> <easy|normal|hard> <行動の短い要約>`
    3. 第4引数の「行動の短い要約」には、絶対に引用符（"や'）を含めたり、成功・失敗時の描写を詰め込んだりしないでください。シンプルに「瓦礫をどかす」のように1フレーズで書くこと。
    4. 複数人で協力している場合は、プレイヤー名をカンマで繋いでください（例: `asanebou_benk,kimuchi_1224`）。
    5. ステータス名には必ず `STR`, `INT`, `DEX` のいずれか3文字のみを指定してください。プレイヤー名などを入れてはなりません。

    # 交渉・アイデアへの柔軟な裁量（重要）
    - プレイヤーが「二人で協力する」「道具を使う」「もっともな作戦を提案する」など、難易度が下がるべき交渉や提案をしてきた場合、再度ダイスを振らせるのではなく、**ダイスを免除して即座に自動成功**として扱い、`!chat gm` で気持ちよく成功描写を行ってストーリーを進めて構いません。
    - プレイヤーとのチャットによる「難易度緩和の問答」に何度も付き合ってゲームを停滞させないよう、スマートに自動成功へ導いてください。

    # プレイヤーの職業とスキルの一貫性
    - データベース（DB）に記載されている各プレイヤーの `class` と `skills` を絶対に勝手に変更したり、別の名前に書き換えて描写したりしないでください。DBの情報が絶対の正義です。
    - プレイヤーが固有スキル・魔法を使用した場合、必ず !sub を用いて、DBに記載されている正しいリソース（MPまたはSP）を消費させてください。

    現在の難易度方針: {diff_prompt}
    """

    user_content = f"--- DB STATUS ---\n{json.dumps(db_snapshot, indent=2, ensure_ascii=False)}\n\n--- プレイヤー発言 ---\n{'/'.join(player_messages)}"

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=user_content,
            config=types.GenerateContentConfig(system_instruction=system_instruction, temperature=0.3),
        )
        return response.text if response.text else ""
    except Exception as e:
        print(f"Gemini API Error: {e}")
        err_msg = str(e)
        if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
            return "!chat system 🚨 **【APIエラー: 429】** Googleの無料枠上限、またはクレジット残高が枯渇しました。"
        elif "503" in err_msg or "Service Unavailable" in err_msg:
            return "!chat system 🚨 **【APIエラー: 503】** Googleサーバーが一時的に過負荷です。少し待って再試行してください。"
        else:
            return f"!chat system 🚨 **【システムエラー】**\n`{err_msg}`"

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
        await message.channel.send("🧹 `System: ゲームデータを完全にリセットしました。`")
        return
    if msg == "!db":
        if os.path.exists(DB_FILE):
            # チャットにjsonファイルとしてアップロードして送信
            await message.channel.send(
                content="📂 **現在の database.json の中身です：**",
                file=discord.File(DB_FILE)
            )
        else:
            await message.channel.send("⚠️ `System: データベースファイルが存在しません。`")
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
        
        # ── システム側では自動でターンを減らさない ──
        if is_success:
            llm_output = call_gemini_gm([f"システム通知: プレイヤー {pending['player']} の「{pending['description']}」の判定結果は 出目{roll} で 【成功】 でした。次の展開や部屋の状況変化、アイテム発見などの描写を出力してください。"], db)
            if llm_output:
                await execute_commands(llm_output, message.channel)
        else:
            fail_warn = (
                f"❌ **判定失敗...**\n"
                f"{pending['player']} の「{pending['description']}」は失敗に終わった。\n"
                f"まだリミットは消費されていません。別のオブジェクトを調べるか、アプローチを変えてみてください！"
            )
            await message.channel.send(f"```diff\n- {fail_warn}\n```")
            
            llm_output = call_gemini_gm([f"システム通知: {pending['player']}の行動「{pending['description']}」は 出目{roll} で 【失敗】 しました。状況は変わっていません。失敗の様子をナレーションしてください。※まだターンリミットを減らすコマンド（!sub）は送らないでください。"], db)
            if llm_output:
                await execute_commands(llm_output, message.channel)
        return

    if msg.startswith("!キャラ作成"):
        if status != "setup" and status != "character_creation":
            await message.channel.send("⚠️ `System: 現在はキャラクター作成フェーズではありません。`")
            return
        
        # 全角スペース、全角コロンを半角に統一してパースの崩れを防止
        raw_input = msg.replace("!キャラ作成", "").replace("　", " ").replace("：", ":").strip()
        
        job_name = "冒険者"
        skill_name = "未覚醒"
        
        # 「スキル:」の文字列をベースに賢くパース
        if "スキル:" in raw_input:
            parts = raw_input.split("スキル:")
            job_name = parts[0].strip()
            skill_name = parts[1].strip()
        elif " " in raw_input:
            parts = raw_input.split(" ", 1)
            job_name = parts[0].strip()
            skill_name = parts[1].strip()
        elif raw_input:
            job_name = raw_input
            if any(k in job_name for k in ["魔", "僧", "神", "癒", "学"]):
                skill_name = "精神集中"
            else:
                skill_name = "ブレイブスラッシュ"

        # 職業名やスキル名からMP（魔法・知性系）かSP（物理系）かを自動判定
        if any(k in job_name or k in skill_name for k in ["魔法", "魔導", "魔剣", "神官", "僧侶", "ヒール", "癒", "呪", "学者", "鑑定", "知識"]):
            res_type = "MP"
            skill_effect = f"MPを3消費し、その能力を発動する"
        else:
            res_type = "SP"
            skill_effect = f"SPを3消費し、その能力を発動する"
            
        stats = {"HP": 20, res_type: 10, "STR": random.randint(6, 18), "INT": random.randint(6, 18), "DEX": random.randint(6, 18)}
        
        db["players"][p_name] = {
            "class": job_name, 
            "skills": {"name": skill_name, "effect": skill_effect, "resource": res_type}, 
            "stats": stats,
            "inventory": [] # 最初から空のインベントリリストを確実に紐付ける
        }
        db["session"]["status"] = "character_creation"
        write_db(db)
        await message.channel.send(f"🎲 **{p_name}** が **{job_name}** としてエントリーしました！\n能力値: `STR:{stats['STR']} / INT:{stats['INT']} / DEX:{stats['DEX']}`\n初期技: `【{skill_name}】({skill_effect})`")
        return

    if msg == "!ゲーム開始":
        if status != "character_creation":
            await message.channel.send("⚠️ `System: 参加者が1人以上キャラ作成を完了した状態で !ゲーム開始 を宣言してください。`")
            return
        db["session"].update({"status": "playing", "stage_count": 1, "turn_left": 4, "log_history": []})
        write_db(db)
        await message.channel.send("⚔️ `System: 運命の歯車が回り出した。ゲームを開始します…`")
        message_queue.append(f"システム通知: ゲームが開始されました。ステージ1の最初の部屋の描写を始めてください。必ず!set current_event.title などを実行し、『埃をかぶった木箱』『古い棚』『不自然な石の窪み』など、探索可能な具体的オブジェクトを最低3つ明文化して含めてください。")
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
