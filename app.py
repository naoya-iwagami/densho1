import os  
import json  
import base64  
import threading  
import datetime  
import uuid  
from flask import Flask, request, render_template, redirect, url_for, session, flash, jsonify, Response  
from flask_session import Session  
from azure.cosmos import CosmosClient  
from openai import AzureOpenAI  
import certifi  
import markdown2  
  
app = Flask(__name__)  
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'your-default-secret-key')  
app.config['SESSION_TYPE'] = 'filesystem'  
app.config['SESSION_FILE_DIR'] = os.path.join(os.getcwd(), 'flask_session')  
app.config['SESSION_PERMANENT'] = False  
Session(app)  
  
client = AzureOpenAI(  
    api_key=os.getenv("AZURE_OPENAI_KEY"),  
    api_version=os.getenv("AZURE_OPENAI_API_VERSION"),  
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT")  
)  
  
cosmos_endpoint = os.getenv("AZURE_COSMOS_ENDPOINT")  
cosmos_key = os.getenv("AZURE_COSMOS_KEY")  
database_name = 'chatdb'  
container_name = 'densho1'  
cosmos_client = CosmosClient(cosmos_endpoint, credential=cosmos_key)  
database = cosmos_client.get_database_client(database_name)  
container = database.get_container_client(container_name)  
  
lock = threading.Lock()  
  
def get_authenticated_user():  
    if "user_id" in session and "user_name" in session:  
        return session["user_id"]  
    client_principal = request.headers.get("X-MS-CLIENT-PRINCIPAL")  
    if client_principal:  
        try:  
            decoded = base64.b64decode(client_principal).decode("utf-8")  
            user_data = json.loads(decoded)  
            user_id = None  
            user_name = None  
            if "claims" in user_data:  
                for claim in user_data["claims"]:  
                    if claim.get("typ") == "http://schemas.microsoft.com/identity/claims/objectidentifier":  
                        user_id = claim.get("val")  
                    if claim.get("typ") == "name":  
                        user_name = claim.get("val")  
            if user_id:  
                session["user_id"] = user_id  
            if user_name:  
                session["user_name"] = user_name  
            return user_id  
        except Exception as e:  
            print("Easy Auth ユーザー情報の取得エラー:", e)  
    session["user_id"] = "anonymous@example.com"  
    session["user_name"] = "anonymous"  
    return session["user_id"]  
  
def save_chat_history():  
    with lock:  
        try:  
            sidebar = session.get("sidebar_messages", [])  
            idx = session.get("current_chat_index", 0)  
            if idx < len(sidebar):  
                current = sidebar[idx]  
                # first_user_messageが空なら保存しない  
                if not current.get("first_user_message", "").strip():  
                    return  
                user_id = get_authenticated_user()  
                user_name = session.get("user_name", "anonymous")  
                session_id = current.get("session_id")  
                affiliation = current.get("affiliation", session.get("affiliation", "神戸品証"))  
                item = {  
                    'id': session_id,  
                    'user_id': user_id,  
                    'user_name': user_name,  
                    'session_id': session_id,  
                    'messages': current.get("messages", []),  
                    'system_message': current["system_message"],  
                    'first_user_message': current.get("first_user_message", ""),  
                    'affiliation': affiliation,  
                    'timestamp': datetime.datetime.utcnow().isoformat()  
                }  
                container.upsert_item(item)  
        except Exception as e:  
            print(f"チャット履歴保存エラー: {e}")  
  
def load_chat_history():  
    with lock:  
        user_id = get_authenticated_user()  
        sidebar_messages = []  
        try:  
            query = "SELECT * FROM c WHERE c.user_id = @user_id ORDER BY c.timestamp DESC"  
            parameters = [{"name": "@user_id", "value": user_id}]  
            items = container.query_items(query=query, parameters=parameters, enable_cross_partition_query=True)  
            for item in items:  
                if 'session_id' in item:  
                    # first_user_messageが空ならスキップ  
                    if not item.get("first_user_message", "").strip():  
                        continue  
                    chat = {  
                        "session_id": item['session_id'],  
                        "messages": item.get("messages", []),  
                        "system_message": item["system_message"],  
                        "first_user_message": item.get("first_user_message", ""),  
                        "affiliation": item.get("affiliation", "神戸品証")  
                    }  
                    sidebar_messages.append(chat)  
        except Exception as e:  
            print(f"チャット履歴読み込みエラー: {e}")  
        return sidebar_messages  
  
def start_new_chat():  
    new_session_id = str(uuid.uuid4())  
    affiliation = session.get("affiliation", "神戸品証")  
    initial_system_message = session.get(  
        'default_system_message',  
        "あなたは技術伝承の下準備アシスタントです。新しく業務を学ぶ人に対して、『何について教えてほしいか』『どの工程でつまずきそうですか』といった問いを繰り返し、ユーザーの『気になる/質問したい観点』を幅広く引き出してください。"  
    )  
    # 所属によって初期メッセージを切り替え  
    if affiliation == "神戸品証":  
        initial_message_content = "こんにちは！私は技術伝承の下準備アシスタントです。高田さんの技術や仕事の進め方で、伝承してもらいたいと思うことは何ですか？"  
    else:  
        initial_message_content = "日々業務の困りごと教えてください。自分以外の困り事でもいいです。"  
    initial_message = {  
        "role": "assistant",  
        "content": initial_message_content  
    }  
    new_chat = {  
        "session_id": new_session_id,  
        "messages": [initial_message],  
        "first_user_message": "",  
        "system_message": initial_system_message,  
        "affiliation": affiliation  
    }  
    sidebar = session.get("sidebar_messages", [])  
    sidebar.insert(0, new_chat)  # 新しいものを先頭に  
    session["sidebar_messages"] = sidebar  
    session["current_chat_index"] = 0  
    session["main_chat_messages"] = [initial_message]  
    session["observed_points"] = []  
    session.modified = True  
  
@app.route('/', methods=['GET', 'POST'])  
def index():  
    get_authenticated_user()  
    # 所属の初期値  
    if "affiliation" not in session:  
        session["affiliation"] = "神戸品証"  
        session.modified = True  
    if "default_system_message" not in session:  
        session["default_system_message"] = (  
            "あなたは技術伝承の下準備アシスタントです。新しく業務を学ぶ人に対して、『何について教えてほしいか』『どの工程でつまずきそうですか』といった問いを繰り返し、ユーザーの『気になる/質問したい観点』を幅広く引き出してください。"  
        )  
        session.modified = True  
    if "sidebar_messages" not in session:  
        session["sidebar_messages"] = load_chat_history() or []  
        session.modified = True  
    if "current_chat_index" not in session:  
        start_new_chat()  
        session["show_all_history"] = False  
        session.modified = True  
    if "main_chat_messages" not in session:  
        idx = session.get("current_chat_index", 0)  
        sidebar = session.get("sidebar_messages", [])  
        if sidebar and idx < len(sidebar):  
            session["main_chat_messages"] = sidebar[idx].get("messages", [])  
        else:  
            session["main_chat_messages"] = []  
        session.modified = True  
    if "show_all_history" not in session:  
        session["show_all_history"] = False  
        session.modified = True  
    if "observed_points" not in session:  
        session["observed_points"] = []  
        session.modified = True  
  
    if request.method == 'POST':  
        # 所属変更  
        if 'affiliation' in request.form:  
            new_affiliation = request.form.get("affiliation", "神戸品証")  
            session["affiliation"] = new_affiliation  
            session.modified = True  
  
            # 現在のチャットが「ユーザー発言無し」なら、最初のアシスタントメッセージを書き換える  
            sidebar = session.get("sidebar_messages", [])  
            idx = session.get("current_chat_index", 0)  
            if idx < len(sidebar):  
                current_chat = sidebar[idx]  
                user_msgs = [m for m in current_chat.get("messages", []) if m["role"] == "user"]  
                if not user_msgs:  
                    # 所属によって初期メッセージを切り替え  
                    if new_affiliation == "神戸品証":  
                        initial_message_content = "こんにちは！私は技術伝承の下準備アシスタントです。高田さんの技術や仕事の進め方で、伝承してもらいたいと思うことは何ですか？"  
                    else:  
                        initial_message_content = "日々業務の困りごと教えてください。自分以外の困り事でもいいです。"  
                    # メッセージ内容を上書き  
                    if current_chat["messages"] and current_chat["messages"][0]["role"] == "assistant":  
                        current_chat["messages"][0]["content"] = initial_message_content  
                    session["main_chat_messages"] = current_chat["messages"]  
                    current_chat["affiliation"] = new_affiliation  
                    session["sidebar_messages"] = sidebar  
                    session.modified = True  
  
            return redirect(url_for('index'))  
        # 新規チャット開始  
        if 'new_chat' in request.form:  
            start_new_chat()  
            session["show_all_history"] = False  
            session.modified = True  
            return redirect(url_for('index'))  
        # チャット履歴選択  
        if 'select_chat' in request.form:  
            session_id = request.form.get("select_chat")  
            sidebar = session.get("sidebar_messages", [])  
            idx = next((i for i, chat in enumerate(sidebar) if chat["session_id"] == session_id), None)  
            if idx is not None:  
                session["current_chat_index"] = idx  
                session["main_chat_messages"] = sidebar[idx].get("messages", [])  
                session["observed_points"] = []  
                session.modified = True  
            return redirect(url_for('index'))  
        # 履歴の表示件数切替  
        if 'toggle_history' in request.form:  
            session["show_all_history"] = not session.get("show_all_history", False)  
            session.modified = True  
            return redirect(url_for('index'))  
  
    chat_history = session.get("main_chat_messages", [])  
    sidebar_messages = session.get("sidebar_messages", [])  
    sidebar_messages = [c for c in sidebar_messages if c.get("first_user_message", "").strip()]  
    max_displayed_history = 6  
    max_total_history = 50  
    show_all_history = session.get("show_all_history", False)  
    observed_points = session.get("observed_points", [])  
    affiliation = session.get("affiliation", "神戸品証")  
  
    return render_template(  
        'index.html',  
        chat_history=chat_history,  
        chat_sessions=sidebar_messages,  
        show_all_history=show_all_history,  
        max_displayed_history=max_displayed_history,  
        max_total_history=max_total_history,  
        session=session,  
        observed_points=observed_points,  
        affiliation=affiliation  
    )  
  
@app.route('/send_message', methods=['POST'])  
def send_message():  
    data = request.get_json()  
    prompt = data.get('prompt', '').strip()  
    if not prompt:  
        return json.dumps({'response': ''}), 400, {'Content-Type': 'application/json'}  
  
    messages = session.get("main_chat_messages", [])  
    messages.append({"role": "user", "content": prompt})  
    session["main_chat_messages"] = messages  
    session.modified = True  
  
    # ユーザー最初の発話をfirst_user_messageに記録  
    idx = session.get("current_chat_index", 0)  
    sidebar = session.get("sidebar_messages", [])  
    if idx < len(sidebar):  
        current_chat = sidebar[idx]  
        user_msgs = [m for m in messages if m["role"] == "user"]  
        if len(user_msgs) == 1:  
            current_chat["first_user_message"] = prompt  
            session["sidebar_messages"] = sidebar  
            session.modified = True  
  
    save_chat_history()  
  
    try:  
        idx = session.get("current_chat_index", 0)  
        sidebar = session.get("sidebar_messages", [])  
        if idx >= len(sidebar) or "system_message" not in sidebar[idx]:  
            raise Exception("チャットのsystem_messageが存在しません")  
        system_msg = sidebar[idx]["system_message"]  
        messages_list = [{"role": "system", "content": system_msg}]  
        messages_list.extend(session.get("main_chat_messages", [])[-40:])  
  
        model_name = "gpt-4.1"  
        response_obj = client.chat.completions.create(  
            model=model_name,  
            messages=messages_list  
        )  
        assistant_response = response_obj.choices[0].message.content  
  
        assistant_response_html = markdown2.markdown(  
            assistant_response,  
            extras=["tables", "fenced-code-blocks", "code-friendly", "break-on-newline", "cuddled-lists"]  
        )  
  
        messages.append({"role": "assistant", "content": assistant_response_html, "type": "html"})  
        session["main_chat_messages"] = messages  
        session.modified = True  
  
        if idx < len(sidebar):  
            sidebar[idx]["messages"] = messages  
            session["sidebar_messages"] = sidebar  
            session.modified = True  
  
        save_chat_history()  
  
        return json.dumps({'response': assistant_response_html}), 200, {'Content-Type': 'application/json'}  
    except Exception as e:  
        print("チャット応答エラー:", e)  
        flash(f"エラーが発生しました: {e}", "error")  
        return json.dumps({'response': f"エラーが発生しました: {e}"}), 500, {'Content-Type': 'application/json'}  
  
@app.route('/summarize_points', methods=['POST'])  
def summarize_points():  
    messages = session.get("main_chat_messages", [])  
    user_msgs = [m["content"] for m in messages if m["role"] == "user"]  
    prompt = (  
        "次の会話履歴から、ユーザーが知りたい観点や不安をリストでまとめてください。\n"  
        "会話履歴：\n"  
        + "\n".join(user_msgs)  
    )  
    idx = session.get("current_chat_index", 0)  
    sidebar = session.get("sidebar_messages", [])  
    if idx >= len(sidebar) or "system_message" not in sidebar[idx]:  
        raise Exception("チャットのsystem_messageが存在しません")  
    response_obj = client.chat.completions.create(  
        model="gpt-4.1",  
        messages=[  
            {"role": "system", "content": "あなたは業務伝承下準備の要約専門AIです。"},  
            {"role": "user", "content": prompt}  
        ]  
    )  
    summary = response_obj.choices[0].message.content  
    # 空行や空観点を除外  
    session["observed_points"] = [p.strip("・- ") for p in summary.strip().split("\n") if p.strip("・- ").strip()]  
    session.modified = True  
    return jsonify({'points_summary': summary})  
  
@app.route('/download_points', methods=['GET'])  
def download_points():  
    points = session.get('observed_points', [])  
    text = "\n".join(points)  
    return Response(  
        text,  
        mimetype='text/plain',  
        headers={'Content-Disposition': 'attachment;filename=points.txt'}  
    )  
  
if __name__ == '__main__':  
    app.run(debug=True, host='0.0.0.0')  