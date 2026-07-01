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

# --- Flask Webサーバー（Render対応） ---
app = Flask('')
@app.route('/')
def home(): return "TRPG GM Engine is Online!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    Thread(target=run_web).start()

# --- クラウドデータベース（Supabase / ローカルJSON）接続 ---
DB_FILE = "database.json"
message_queue = []
is_processing = False

def get_default_db():
    return {
        "session": {
            "stage_count": 0,
            "status": "setup",
            "turn_left": 4,
            "log_history": [],
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

def get_db_snapshot():
    # Supabase接続を優先試行
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")
    if supabase_url and supabase_key:
        import urllib.request
        try:
            url = f"{supabase_url.rstrip('/')}/rest/v1/game_state?id=eq.1"
            headers = {
                "apikey": supabase_key,
                "Authorization": f"Bearer {supabase_key}",
                "Content-Type": "application/json"
            }
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as response:
                res_data = json.loads(response.read().decode('utf-8'))
                if res_data:
                    return res_data[0]["data"]
        except Exception as e:
            print(f"[Supabase Read Error] {e} -> Falling back to local JSON")

    # ローカルJSONフォールバック
    if not os.path.exists(DB_FILE):
        with open(DB_FILE, 'w', encoding='utf-8') as f:
            json.dump(get_default_db(), f, indent=2, ensure_ascii=False)
    with open(DB_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def write_db(data):
    # Supabase書き込みを優先試行
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")
    if supabase_url and supabase_key:
        import urllib.request
        try:
            url = f"{supabase_url.rstrip('/')}/rest/v1/game_state?id=eq.1"
            headers = {
                "apikey": supabase_key,
                "Authorization": f"Bearer {supabase_key}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge"
            }
            payload = json.dumps({"data": data}).encode('utf-8')
            req = urllib.request.Request(url, data=payload, headers=headers, method="PATCH")
            with urllib.request.urlopen(req, timeout=5) as response:
                return
        except Exception as e:
            print(f"[Supabase Write Error] {e} -> Saving to local JSON")

    # ローカルJSONフォールバック
    with open(DB_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

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
                    
                    inv = p_val.get("inventory", [])
                    if isinstance(inv, str):
                        if inv.startswith('[') and inv.endswith(']'):
                            try:
                                inv = json.loads(inv.replace("'", '"'))
                            except:
                                inv = [x.strip().replace('"', '').replace("'", "") for x in inv[1:-1].split(',')]
                        else:
                            inv = [inv]
                    elif not isinstance(inv, list):
                        inv = []
                    
                    inv = [str(x).strip() for x in inv if str(x).strip()]
                    inv_str = f" | 💼:{', '.join(inv)}" if inv else ""
                    
                    status_bars.append(f"👤 **{p_key}** [{p_val['class']}] HP:{p_stats['HP']}/20 | {res_type}:{p_stats.get(res_type, 10)}/10{inv_str}\n   ↳ 技: **{p_val['skills']['name']}** ({p_val['skills']['effect']})")
                status_str = "\n".join(status_bars)

                stage_display = f"STAGE {stage}/20" if stage < 20 else "FINAL STAGE (20/20)"
                if stage > 0:
                    header = (
                        f"━ [{speaker}] ━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📍 **{stage_display} : {event_title}** (⏳部屋のリミット: **{turn_left}** 行動)\n"
                        f"{status_str}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    )
                else:
                    header = f"━ [{speaker}] ━━━━━━━━━━\n"
                await channel.send(f"{header}{msg}")
                
            elif command in ["set", "add", "sub"]:
                sub_args = args_str.split(' ', 1)
                path, val = sub_args[0], sub_args[1]
                
                if val.startswith('"') and val.endswith('"'):
                    val = val[1:-1]
                elif val.startswith("'") and val.endswith("'"):
                    val = val[1:-1]
                
                if val.isdigit(): 
                    val = int(val)
                
                path = re.sub(r'\[\d+\]', '', path)
                keys = path.split('.')
                current = db
                for key in keys[:-1]:
                    current = current.setdefault(key, {})
                
                target_key = keys[-1]
                
                if command == "set": 
                    current[target_key] = val
                elif command == "add":
                    if isinstance(current.get(target_key), list):
                        current[target_key].append(val)
                    elif target_key == "inventory" or not isinstance(val, int):
                        if target_key not in current or not isinstance(current[target_key], list):
                            current[target_key] = []
                        current[target_key].append(val)
                    else:
                        current[target_key] = current.get(target_key, 0) + val
                elif command == "sub": 
                    # インベントリからアイテムを削除する特別ロジック（使い回し防止）
                    if target_key == "inventory" or isinstance(current.get(target_key), list):
                        if isinstance(current[target_key], list) and val in current[target_key]:
                            current[target_key].remove(val)
                    else:
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
        except Exception as cmd_error:
            print(f"[Command Execution Failed] Line: '{line}', Error: {cmd_error}")

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

    # ステージの進行ルール（重要：絶対に守ること）
    - プレイヤーが部屋の調査を終え、次のステージ（次の部屋、通路、広間など）へ進む描写をする際は、**必ず同時に `!add session.stage_count 1` および `!set session.turn_left 4` を実行し、データベースをステージ進行させてください。** これを怠ると、ステージ数が1のまま変化しなくなります。
    - プレイヤーが次の部屋に進んだら、新しい部屋名で `!set current_event.title <新しい部屋名>` を実行してください。

    # プレイヤーの職業とスキルの一貫性
    - データベース（DB）に記載されている各プレイヤーの `class` と `skills` を絶対に勝手に変更したり、別の名前に書き換えて描写したりしないでください。DBの情報が絶対の正義です。
    - プレイヤーが固有スキル・魔法を使用した場合、必ず !sub を用いて、DBに記載されている正しいリソース（MPまたはSP）を消費させてください。

    # アイテム消費のルール（重要：使い回し防止）
    - プレイヤーが「古びた鍵」や「古びた石板」などの鍵・イベントアイテムを使用、または「携帯食料」を食べて回復した場合、**必ず即座に `!sub players.名前.inventory <アイテム名>` コマンドを実行して、プレイヤーのインベントリからそのアイテムを削除・消滅させてください。**

    # 突然のダメージ・HP減少における描写ルール（重要）
    - トラップの発動や敵の襲撃、リミット超過（turn_left=0）などでプレイヤーにダメージ（HP減少）を与えるコマンド（例: `!sub players.名前.stats.HP 3`）を実行する際は、**必ずそのHP減少の正当な理由（崩落が発生した、毒ガスが噴き出した、背後から急襲されたなど）を迫真のナレーションで詳しく描写してください。**唐突に理由なくHPが減ったように見せてはなりません。

    # PVP（プレイヤー間対立）へのTRPG的対応
    - プレイヤーが「他の仲間の首をはねる」「仲間を攻撃する」などの危険な暴走行動を起こした場合、メタ的に無視したりスルーしたりしてはなりません。
    - 即座に、攻撃した側には攻撃判定（STRやDEX）、攻撃された側には回避判定（DEX）のための `!propose_dice` を提案し、本当にダメージが発生する緊張感ある展開としてゲーム的に真っ向からジャッジしてください。

    # 【最重要】ステージ20（STAGE 20/20）クライマックス ＆ エンディングルール
    - ステージが 20 に到達したら、それは神殿の最深部、すなわちラストボスの領域です。
    - あなたは直ちに「宿命のラストボス（邪悪なクリスタルゴーレム、古代の魔導師など）」を出現させ、決死の決戦を描写してください。
    - プレイヤーがこのボスを撃破する、あるいは儀式を阻止することに成功した場合、**これまでの冒険の軌跡を称える感動的なマルチエンディング（ハッピーエンド、または自己犠牲の悲劇など）を描写し、ゲームクリアを宣言してください。** 21/20 以降へダラダラとステージを進行させては絶対になりません。

    現在の難易度方針: {diff_prompt}
    """
    user_content = f"--- DB STATUS ---\n{json.dumps(db_snapshot, indent=2, ensure_ascii=False)}\n\n--- プレイヤー発言 ---\n{'/'.join(player_messages)}"

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=user_content,
            config=types.GenerateContentConfig(system_instruction=system_instruction, temperature=0.2),
        )
        return response.text if response.text else ""
    except Exception as e:
        print(f"Gemini API Error: {e}")
        return f"!chat system 🚨 **【システムエラー】**\n`{str(e)}`"

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
        # 【重要】ダイスを実行（挑戦）したため、リミット（行動消費）を自動的に1減らす
        db["session"]["turn_left"] = max(db["session"].get("turn_left", 4) - 1, 0)
        write_db(db)
        
        if is_success:
            llm_output = call_gemini_gm([f"システム通知: {pending['player']} の「{pending['description']}」の判定結果は 出目{roll} で 【成功】 でした。展開を進めてください。"], db)
            if llm_output: await execute_commands(llm_output, message.channel)
        else:
            fail_warn = f"❌ **判定失敗...**\n{pending['player']} の「{pending['description']}」は失敗に終わった。"
            await message.channel.send(f"```diff\n- {fail_warn}\n```")
            llm_output = call_gemini_gm([f"システム通知: {pending['player']}の行動「{pending['description']}」は 出目{roll} で 【失敗】 しました。次の行動を待つ描写、または状況の緊迫化を描写してください。"], db)
            if llm_output: await execute_commands(llm_output, message.channel)
        return

    if msg.startswith("!キャラ作成"):
        if status != "setup" and status != "character_creation":
            await message.channel.send("⚠️ `System: 現在はキャラクター作成フェーズではありません。`")
            return
        
        raw_input = msg.replace("!キャラ作成", "").replace("　", " ").replace("：", ":").strip()
        
        job_name = "冒険者"
        skill_name = "未覚醒"
        
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

        if any(k in job_name or k in skill_name for k in ["魔法", "魔導", "魔剣", "神官", "僧侶", "ヒール", "癒", "呪", "学者", "鑑定", "知識"]):
            res_type = "MP"
            skill_effect = f"MPを3消費し、その能力を発動する"
        else:
            res_type = "SP"
            skill_effect = f"SPを3消費し、その能力を発動する"
            
        stats = {"HP": 20, res_type: 10, "STR": random.randint(6, 18), "INT": random.randint(6, 18), "DEX": random.randint(6, 18)}
        
        # 役職に応じた「デフォルト初期装備」の設定
        initial_inventory = ["携帯食料"]
        normalized_job = job_name.lower()
        if any(k in normalized_job for k in ["戦士", "剣士", "騎士", "武士", "ナイト", "ファイター"]):
            initial_inventory = ["さびた大剣", "ボロい鉄盾", "携帯食料"]
        elif any(k in normalized_job for k in ["魔法", "魔導", "魔術", "ウィザード", "メイジ"]):
            initial_inventory = ["初心者の杖", "すり切れた魔導書", "携帯食料"]
        elif any(k in normalized_job for k in ["僧侶", "神官", "シスター", "プリースト", "ヒーラー"]):
            initial_inventory = ["木製の聖印", "回復の軟膏", "携帯食料"]
        elif any(k in normalized_job for k in ["盗賊", "シーフ", "暗殺", "アサシン"]):
            initial_inventory = ["錆びた短剣", "万能針(ピッキングツール)", "携帯食料"]
        elif any(k in normalized_job for k in ["学者", "賢者", "アルケミスト", "鑑定"]):
            initial_inventory = ["拡大用の虫眼鏡", "古い白地図", "携帯食料"]
        else:
            initial_inventory = ["旅人のナイフ", "使い古したマント", "携帯食料"]
        
        db["players"][p_name] = {
            "class": job_name, 
            "skills": {"name": skill_name, "effect": skill_effect, "resource": res_type}, 
            "stats": stats,
            "inventory": initial_inventory
        }
        db["session"]["status"] = "character_creation"
        write_db(db)
        
        inv_desc = " / ".join(initial_inventory)
        await message.channel.send(
            f"🎲 **{p_name}** が **{job_name}** としてエントリーしました！\n"
            f"能力値: `STR:{stats['STR']} / INT:{stats['INT']} / DEX:{stats['DEX']}`\n"
            f"初期技: `【{skill_name}】({skill_effect})`\n"
            f"💼 初期所持品: `[{inv_desc}]`"
        )
        return

    if msg == "!ゲーム開始":
        if status != "character_creation": return
        db["session"].update({"status": "playing", "stage_count": 1, "turn_left": 4, "log_history": []})
        write_db(db)
        await message.channel.send("⚔️ `System: ゲームを開始します…`")
        message_queue.append(f"システム通知: ステージ1開始。オブジェクトを最低3つ配置してください。")
        asyncio.create_task(process_queue(message.channel))
        return

    if status == "playing":
        if db["session"]["pending_dice"]:
            db["session"]["pending_dice"] = None
            write_db(db)
        
        # 【物理強制ステージ進行＆ダメージシステム】（スタックバグ完全解決、STAGE20手前まで稼働）
        if db["session"].get("turn_left", 4) <= 0 and db["session"].get("stage_count", 1) < 20:
            stage_count = db["session"].get("stage_count", 1) + 1
            db["session"]["stage_count"] = stage_count
            db["session"]["turn_left"] = 4 # リミット初期化
            
            damage_notif = ""
            for p_key, p_val in db["players"].items():
                old_hp = p_val["stats"].get("HP", 20)
                new_hp = max(old_hp - 3, 1)  # 死亡直前まで減らす
                p_val["stats"]["HP"] = new_hp
                damage_notif += f"👤 **{p_key}** が 3ダメージを受けた！ (HP: {old_hp} ➡️ {new_hp})\n"
                
            write_db(db)
            
            warn_embed = (
                f"🚨 **【活動限界超過：強制エリア移動発動】**\n"
                f"このエリアを探索する時間が尽きてしまいました！\n"
                f"突然、部屋に罠が発動し、崩落が発生して全員がダメージを受けました！\n\n"
                f"{damage_notif}\n"
                f"💥 その衝撃によって、あなたたちは半ば強制的に次のステージへと押し流されました！"
            )
            await message.channel.send(f"```diff\n- {warn_embed}\n```")
            
            system_force_msg = (
                f"システム通知：タイムリミット（turn_left=0）をオーバーしたため、"
                f"システム側が強制的にプレイヤーにダメージを与えて STAGE {stage_count} に進行させました。"
                f"これまでの探索エリアは崩落や罠で壊滅しました。あなたたちは命からがら、次の新しい部屋へとたどり着きました。"
                f"次の新しい部屋名（!set current_event.title）と新しい部屋の状況描写を !chat gm で開始してください。"
            )
            message_queue.append(system_force_msg)
            if not is_processing:
                asyncio.create_task(process_queue(message.channel))
            return

        # プレイヤーが「携帯食料」などの使用を宣言した、またはスキルを使用してリソースを減らす行動をした場合、システムで行動消費を1減らす
        if any(k in msg for k in ["!実行", "スキル", "使う", "攻撃", "破壊", "動かす", "どかす", "食べる", "飲む"]):
            db["session"]["turn_left"] = max(db["session"].get("turn_left", 4) - 1, 0)
            write_db(db)

        message_queue.append(f"[{p_name}]: {msg}")
        if not is_processing:
            asyncio.create_task(process_queue(message.channel))

if __name__ == "__main__":
    keep_alive()
    client.run(os.environ.get("DISCORD_TOKEN"))
