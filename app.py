import streamlit as st
import pandas as pd
import requests
import os
import re
import mimetypes
import zipfile
import tempfile

# ==========================================
# 核心模块 1：Markdown 解析与文件名智能提取
# ==========================================
def clean_database_id(raw_id):
    """【新增防呆机制】自动从用户输入的各种奇怪内容（如完整URL、带参数链接）中提取出标准 32 位 Database ID"""
    if not raw_id: return ""
    raw_id = raw_id.strip()
    # 匹配 32 位的纯十六进制字符串，或者带连字符的标准 UUID 格式
    match = re.search(r'([a-f0-9]{8}-?[a-f0-9]{4}-?[a-f0-9]{4}-?[a-f0-9]{4}-?[a-f0-9]{12}|[a-f0-9]{32})', raw_id, re.IGNORECASE)
    if match:
        return match.group(1).replace('-', '') # Notion API 通常喜欢不带连字符的纯32位ID
    return raw_id

def extract_metadata_from_filename(filename):
    year, status, start_date, end_date = "", "", "", ""
    year_match = re.search(r'【\s*(\d{4})', filename)
    if year_match: year = year_match.group(1)
        
    if "返场" in filename: status = "返场"
    elif "常驻" in filename: status = "常驻"
    elif "限时" in filename: status = "限时"
    elif "赠品" in filename: status = "赠品"
        
    date_match = re.search(r'】\s*(\d{1,2}-\d{1,2})\s*-\s*(\d{1,2}-\d{1,2})', filename)
    if date_match and year:
        start_md = date_match.group(1)
        end_md = date_match.group(2)
        start_date = f"{year}-{start_md}"
        
        start_month = int(start_md.split('-')[0])
        end_month = int(end_md.split('-')[0])
        end_year = str(int(year) + 1) if end_month < start_month else year
        end_date = f"{end_year}-{end_md}"
        
    return status, start_date, end_date

def parse_markdown_table(md_content):
    lines = md_content.strip().split('\n')
    header_idx = -1
    for i, line in enumerate(lines):
        if line.strip().startswith('|') and '---' in lines[i+1]:
            header_idx = i
            break
    if header_idx == -1: return None
    headers = [col.strip() for col in lines[header_idx].strip('|').split('|')]
    data_lines = lines[header_idx + 2:]
    parsed_data = []
    for line in data_lines:
        line = line.strip()
        if not line.startswith('|'): continue
        cols = [col.strip() for col in line.strip('|').split('|')]
        cols = cols + [''] * (len(headers) - len(cols))
        parsed_data.append(cols)
    return pd.DataFrame(parsed_data, columns=headers)

def extract_image_filename(md_image_string):
    match = re.search(r'!\[.*?\]\((.*?)\)', str(md_image_string))
    if match: return os.path.basename(match.group(1))
    return ""

def extract_all_image_filenames(md_string):
    matches = re.findall(r'!\[.*?\]\((.*?)\)', str(md_string))
    return [os.path.basename(m) for m in matches if m]

def find_file_smart(base_folder, target_filename, context_hint=""):
    if not os.path.exists(base_folder): return None
    found_paths = []
    for root, dirs, files in os.walk(base_folder):
        if target_filename in files:
            found_paths.append(os.path.join(root, target_filename))
    if not found_paths: return None
    if len(found_paths) == 1: return found_paths[0]
    if context_hint:
        for path in found_paths:
            parent_folder_name = os.path.basename(os.path.dirname(path))
            if parent_folder_name in context_hint:
                return path 
    return found_paths[0] 

# ==========================================
# 核心模块 2：Notion 数据格式化与强力查重引擎
# ==========================================
def format_notion_property(prop_type, raw_value):
    if not str(raw_value).strip() or raw_value == "None": return None
    val = str(raw_value).strip()
    try:
        val = re.sub(r'^#.*?/', '', val).strip()
        if prop_type == "文本 (Text)": return {"rich_text": [{"text": {"content": val}}]}
        elif prop_type == "单选 (Select)": return {"select": {"name": val}}
        elif prop_type == "多选 (Multi-select)": 
            tags = [t.strip() for t in val.split('/') if t.strip()] 
            return {"multi_select": [{"name": t} for t in tags]}
        elif prop_type == "数字 (Number)":
            num_str = re.sub(r'[^\d\.-]', '', val)
            return {"number": float(num_str)} if num_str else None
        elif prop_type == "链接 (URL)": 
            match = re.search(r'\]\((.*?)\)', val)
            url = match.group(1) if match else val
            return {"url": url}
    except Exception: return None
    return None

def upload_local_file_to_notion(local_file_path, token, version):
    if not os.path.exists(local_file_path): return None
    filename = os.path.basename(local_file_path)
    mime_type, _ = mimetypes.guess_type(local_file_path)
    mime_type = mime_type or "application/octet-stream"
    session_url = "https://api.notion.com/v1/file_uploads"
    headers = {"Authorization": f"Bearer {token}", "Notion-Version": version, "Content-Type": "application/json"}
    session_res = requests.post(session_url, headers=headers, json={"mode": "single_part"})
    if session_res.status_code not in (200, 201): return None
    upload_url = session_res.json().get("upload_url")
    upload_id = session_res.json().get("id") or session_res.json().get("file_upload", {}).get("id")
    with open(local_file_path, "rb") as f:
        upload_res = requests.post(upload_url, headers={"Authorization": f"Bearer {token}", "Notion-Version": version}, files={"file": (filename, f, mime_type)})
        if upload_res.status_code not in (200, 201): return None
    return upload_id

def get_existing_notion_titles(token, db_id, title_prop_name):
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    headers = {"Authorization": f"Bearer {token}", "Notion-Version": "2025-09-03", "Content-Type": "application/json"}
    existing_titles = set()
    has_more = True
    next_cursor = None
    
    while has_more:
        payload = {"page_size": 100}
        if next_cursor: payload["start_cursor"] = next_cursor
        res = requests.post(url, headers=headers, json=payload)
        
        if res.status_code != 200: 
            return None, f"Notion API 报错 (错误码 {res.status_code}): {res.text}"
            
        data = res.json()
        for result in data.get("results", []):
            props = result.get("properties", {})
            title_obj = props.get(title_prop_name)
            
            if title_obj is None:
                return None, f"在你填写的 Notion 数据库里，找不到名为『{title_prop_name}』的标题属性！请检查网页上的大小写或名字是否与 Notion 严格一致。"
                
            title_arr = title_obj.get("title", [])
            if title_arr: 
                existing_titles.add(title_arr[0].get("text", {}).get("content", "").strip())
                
        has_more = data.get("has_more", False)
        next_cursor = data.get("next_cursor")
    return existing_titles, None

def create_notion_page(token, db_id, properties_data, children_data=None):
    url = "https://api.notion.com/v1/pages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Notion-Version": "2025-09-03"}
    payload = {"parent": { "database_id": db_id }, "properties": properties_data}
    if children_data and len(children_data) > 0: payload["children"] = children_data
    response = requests.post(url, headers=headers, json=payload)
    return response.status_code == 200, response.text

# ==========================================
# 核心模块 3：Streamlit GUI 
# ==========================================
st.set_page_config(page_title="谷子库自动更新插件", layout="wide")
st.title("📦 Notion 谷子库自动更新插件")
st.caption("只需拖入官方 MD 文件和图片压缩包，自动为你查重并更新 Notion 数据库。")

with st.sidebar:
    st.header("🔑 你的专属配置")
    st.info("💡 安全提示：你的 Token 仅在当前浏览器生效，我们不会在服务器保存任何隐私数据。")
    token = st.text_input("Notion Token (Internal Integration Secret)", type="password")
    db_id_raw = st.text_input("你的 Database ID 或 完整数据库URL")
    
    # 执行防呆清洗
    db_id = clean_database_id(db_id_raw)

col_md, col_zip = st.columns(2)
with col_md:
    uploaded_files = st.file_uploader("1️⃣ 上传 Markdown 文件 (.md) [支持多选]", type="md", accept_multiple_files=True)
with col_zip:
    uploaded_zip = st.file_uploader("2️⃣ 上传图片压缩包 (.zip) [选填]", type="zip")

if uploaded_files:
    all_dfs = []
    for uf in uploaded_files:
        md_content = uf.getvalue().decode("utf-8")
        df = parse_markdown_table(md_content)
        if df is not None:
            status, s_date, e_date = extract_metadata_from_filename(uf.name)
            df['⚙️来源文件'] = uf.name
            df['⚙️提取状态'] = status
            df['⚙️提取开始日期'] = s_date
            df['⚙️提取结束日期'] = e_date
            all_dfs.append(df)
            
    if all_dfs:
        final_df = pd.concat(all_dfs, ignore_index=True)
        st.subheader(f"👀 数据预览 (已合并 {len(uploaded_files)} 个文件，共 {len(final_df)} 条)")
        st.dataframe(final_df, use_container_width=True)
        
        md_columns = ["忽略此列 (不导入)"] + list(final_df.columns)
        
        st.divider()
        st.subheader("🧩 核心字段映射")
        col1, col2 = st.columns(2)
        with col1:
            notion_title_name = st.text_input("Notion [Title / 标题] 属性名", value="Name")
            md_title_col = st.selectbox("对应 MD 哪一列？", md_columns, index=md_columns.index("商品名") if "商品名" in md_columns else 0)
        with col2:
            notion_cover_name = st.text_input("Notion [Files / 封面] 属性名", value="封面图")
            md_cover_col = st.selectbox("对应 MD 哪一列？(包含封面图片)", md_columns, index=md_columns.index("封面图") if "封面图" in md_columns else 0)

        st.write("---")
        st.subheader("📅 文件名元数据提取")
        col_s, col_d = st.columns(2)
        with col_s: notion_status_name = st.text_input("Notion [Select / 状态] 属性名", value="状态")
        with col_d: notion_date_name = st.text_input("Notion [Date / 日期] 属性名", value="售卖时间")

        st.write("---")
        st.subheader("➕ 附加字段映射")
        NOTION_TYPES = ["文本 (Text)", "单选 (Select)", "多选 (Multi-select)", "数字 (Number)", "链接 (URL)", "图片组 -> 放入属性", "图片组 -> 放入正文"]
        
        default_extras = [
            {"name": "游戏", "type": "单选 (Select)", "col": "游戏"},
            {"name": "系列", "type": "单选 (Select)", "col": "系列"},
            {"name": "价格", "type": "数字 (Number)", "col": "价格"},
            {"name": "商品链接", "type": "链接 (URL)", "col": "商品链接"},
            {"name": "款式", "type": "多选 (Multi-select)", "col": "款式"},
            {"name": "详情图", "type": "图片组 -> 放入正文", "col": "详情图"}
        ]
        
        current_extras = []
        for i in range(6):
            def_name = default_extras[i]["name"] if i < len(default_extras) else ""
            def_type = default_extras[i]["type"] if i < len(default_extras) else "文本 (Text)"
            def_col = default_extras[i]["col"] if i < len(default_extras) else md_columns[0]
            
            c1, c2, c3 = st.columns([3, 2, 3])
            with c1: name_val = st.text_input(f"附加字段 {i+1} - Notion 属性名", value=def_name, key=f"ex_name_{i}")
            with c2: type_val = st.selectbox(f"数据类型 / 插入位置", NOTION_TYPES, index=NOTION_TYPES.index(def_type) if def_type in NOTION_TYPES else 0, key=f"ex_type_{i}")
            with c3: col_val = st.selectbox(f"对应 MD 列", md_columns, index=md_columns.index(def_col) if def_col in md_columns else 0, key=f"ex_col_{i}")
            current_extras.append({"name": name_val, "type": type_val, "col": col_val})

        st.divider()
        st.subheader("🚀 开始导入")
        enable_deduplication = st.toggle("🛡️ 开启智能查重 (绝不覆盖已有记录)", value=True)
        
        if st.button("⚡ 开始同步到 Notion", type="primary"):
            if not token or not db_id: 
                st.error("👈 请先在左侧边栏配置 Token 和 Database ID！")
            else:
                progress_bar = st.progress(0)
                status_text = st.empty()
                total_rows = len(final_df)
                success_count, skip_count = 0, 0
                
                temp_dir_context = tempfile.TemporaryDirectory()
                img_folder = ""
                if uploaded_zip:
                    status_text.text("📦 正在云端解压图片...")
                    with zipfile.ZipFile(uploaded_zip, 'r') as zip_ref:
                        zip_ref.extractall(temp_dir_context.name)
                    img_folder = temp_dir_context.name

                try:
                    existing_items = set()
                    if enable_deduplication:
                        status_text.text("🔍 正在安全扫描你的 Notion 库寻找已购商品...")
                        existing_items, error_msg = get_existing_notion_titles(token, db_id, notion_title_name)
                        if error_msg:
                            st.error(f"❌ 查重引擎启动失败！原因：{error_msg}")
                            st.stop()
                        else:
                            st.info(f"💡 查重扫描成功！在 Notion 中发现了 {len(existing_items)} 个已有商品。")
                    
                    for index, row in final_df.iterrows():
                        current_item_name = str(row[md_title_col]).strip() if md_title_col != "忽略此列 (不导入)" else ""
                        if enable_deduplication and current_item_name in existing_items:
                            status_text.text(f"⏭️ [{current_item_name}] 已存在，跳过 ({index+1}/{total_rows})")
                            skip_count += 1
                            progress_bar.progress((index + 1) / total_rows)
                            continue

                        status_text.text(f"正在导入 [{row['⚙️来源文件']}] : {current_item_name}...")
                        properties, children_blocks = {}, []
                        row_full_text = " ".join([str(val) for val in row.values])
                        
                        if current_item_name: properties[notion_title_name] = {"title": [{"text": {"content": current_item_name}}]}
                        if notion_status_name and row['⚙️提取状态']: properties[notion_status_name] = {"select": {"name": row['⚙️提取状态']}}
                        if notion_date_name and row['⚙️提取开始日期']:
                            date_payload = {"start": row['⚙️提取开始日期']}
                            if row['⚙️提取结束日期']: date_payload["end"] = row['⚙️提取结束日期']
                            properties[notion_date_name] = {"date": date_payload}
                            
                        if md_cover_col != "忽略此列 (不导入)" and img_folder:
                            filename = extract_image_filename(row[md_cover_col])
                            if filename:
                                local_path = find_file_smart(img_folder, filename, row_full_text)
                                if local_path:
                                    upload_id = upload_local_file_to_notion(local_path, token, "2025-09-03")
                                    if upload_id: properties[notion_cover_name] = {"files": [{"name": filename, "type": "file_upload", "file_upload": {"id": upload_id}}]}
                                    
                        for ex in current_extras:
                            n_name, m_col, p_type = ex["name"].strip(), ex["col"], ex["type"]
                            if m_col != "忽略此列 (不导入)":
                                raw_val = row[m_col]
                                if p_type in ["图片组 -> 放入属性", "图片组 -> 放入正文"] and img_folder:
                                    filenames = extract_all_image_filenames(raw_val)
                                    files_payload = []
                                    for fname in filenames:
                                        local_path = find_file_smart(img_folder, fname, row_full_text)
                                        if local_path:
                                            upload_id = upload_local_file_to_notion(local_path, token, "2025-09-03")
                                            if upload_id:
                                                if p_type == "图片组 -> 放入属性": files_payload.append({"name": fname, "type": "file_upload", "file_upload": {"id": upload_id}})
                                                elif p_type == "图片组 -> 放入正文": children_blocks.append({"object": "block", "type": "image", "image": {"type": "file_upload", "file_upload": {"id": upload_id}}})
                                    if files_payload and p_type == "图片组 -> 放入属性" and n_name: properties[n_name] = {"files": files_payload}
                                elif n_name:
                                    prop_payload = format_notion_property(p_type, raw_val)
                                    if prop_payload: properties[n_name] = prop_payload
                                    
                        success, err_msg = create_notion_page(token, db_id, properties, children_blocks)
                        if success: success_count += 1
                        else: st.error(f"第 {index+1} 行导入失败: {err_msg}")
                        progress_bar.progress((index + 1) / total_rows)
                        
                    st.success(f"🎉 更新完毕！新增了 {success_count} 条，跳过了 {skip_count} 条已有记录。")
                finally:
                    temp_dir_context.cleanup()