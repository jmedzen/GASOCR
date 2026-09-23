import base64
import time
import httpx
from pathlib import Path
from typing import Dict, Any, Tuple, Optional, List
import database

def build_ocr_prompt(lang: str, direction: str, column: str, custom_prompt: str = "") -> str:
    """根據使用者的排版設定動態組裝 OCR 提示詞"""
    
    # 1. 語言設定
    lang_rules = {
        "traditional": "【輸出語言】：必須強制轉換/轉譯為「繁體中文」（臺灣慣用詞彙與標準標點符號，標點請使用全形）。",
        "simplified": "【輸出語言】：必須強制轉換/轉譯為「簡體中文」。",
        "original": "【輸出語言】：嚴格維持圖片中原始文字的語言與字體，不做簡繁或語言轉換。",
    }
    lang_instruction = lang_rules.get(lang, lang_rules["traditional"])
    
    # 2. 排版方向設定
    dir_rules = {
        "horizontal": "【閱讀方向】：版面為「橫排」（文字由左至右、段落由上至下）。請依此順序連續辨識。",
        "vertical": "【閱讀方向】：版面為「直排/豎排」（文字縱向書寫，直行由右至左、由上至下閱讀）。請嚴格遵循直排順序，切勿橫向跨行拼湊文字造成斷句倒錯！",
        "auto": "【閱讀方向】：請自行分析版面的文字書寫方向（直排或橫排），並嚴格按照文字的真實閱讀順序輸出。",
    }
    dir_instruction = dir_rules.get(direction, dir_rules["auto"])
    
    # 3. 欄位結構設定
    col_rules = {
        "single": "【欄位配置】：版面為「單欄」。請由上而下逐段連續辨識輸出。",
        "double": "【欄位配置】：版面為「雙欄」。極重要：請務必完整讀完第一欄（依閱讀方向，橫排通常在左側，直排在右側），再讀取第二欄！絕對不要橫跨左右兩欄混讀！",
        "triple": "【欄位配置】：版面為「三欄」。極重要：請嚴格依據欄位順序（第一欄 -> 第二欄 -> 第三欄）逐欄讀完，切勿橫向跨欄串聯句子！",
        "auto": "【欄位配置】：請自動辨識頁面是否有多欄（單欄/雙欄/三欄）。若有多欄，請務必遵循「讀完整欄再讀下一欄」的規則，嚴禁跨欄混讀。",
    }
    col_instruction = col_rules.get(column, col_rules["auto"])
    
    custom_part = f"\n【額外指定需求】：\n{custom_prompt.strip()}" if custom_prompt.strip() else ""

    prompt = f"""你是一名頂尖的文檔 OCR 辨識與轉譯專家。請對所附圖片進行高精度的文字擷取與排版轉譯。

請嚴格遵循以下規則：
1. {lang_instruction}
2. {dir_instruction}
3. {col_instruction}
4. 【結構與格式】：
   - 保留原文的段落劃分與標題層級（使用 Markdown `#`, `##`, `###`）。
   - 若有表格，請精準轉譯為標準 Markdown 表格格式（`| 標題 | 標題 |`）。
   - 保留註腳、圖表標題與頁下備註。
5. 【輸出規範】：
   - 嚴禁添加任何多餘的引言、前置說明或後記（例如「以下是辨識結果：」等）。
   - 請直接輸出轉譯後的文檔內文。
{custom_part}
"""
    return prompt.strip()

async def refresh_oauth_token_if_needed(account: Dict[str, Any]) -> str:
    """若 OAuth Access Token 即將過期或為空，向 Google 換取新的 token"""
    now = time.time()
    current_token = account.get("oauth_access_token")
    expiry = account.get("oauth_token_expiry") or 0.0
    
    # 若還有 60 秒以上效期，直接使用
    if current_token and expiry > now + 60:
        return current_token
        
    client_id = account.get("oauth_client_id")
    client_secret = account.get("oauth_client_secret")
    refresh_token = account.get("oauth_refresh_token")
    
    if not (client_id and client_secret and refresh_token):
        raise ValueError("OAuth 帳號缺乏 client_id, client_secret 或 refresh_token")
        
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token"
            }
        )
        if resp.status_code != 200:
            raise ValueError(f"刷新 OAuth Token 失敗: {resp.text}")
            
        data = resp.json()
        new_token = data["access_token"]
        expires_in = data.get("expires_in", 3600)
        
        # 更新資料庫
        async with database.get_db() as db:
            await db.execute("""
                UPDATE accounts 
                SET oauth_access_token = ?, oauth_token_expiry = ? 
                WHERE id = ?
            """, (new_token, now + expires_in, account["id"]))
            await db.commit()
            
        return new_token

async def call_gemini_ocr(
    account: Dict[str, Any],
    image_path: Path,
    model: str,
    prompt: str
) -> Tuple[bool, str, int]:
    """
    呼叫 Google AI Studio Gemini API 進行單頁圖片 OCR
    回傳值: (is_success, text_or_error, http_status_code)
    """
    # 讀取圖片轉為 Base64
    with open(image_path, "rb") as f:
        img_bytes = f.read()
    img_b64 = base64.b64encode(img_bytes).decode("utf-8")
    
    # 構造 Google Generative Language API 請求
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"Content-Type": "application/json"}
    params = {}
    
    if account["auth_type"] == "api_key":
        api_key = account.get("api_key", "").strip()
        if not api_key:
            return False, "API Key 為空", 400
        params["key"] = api_key
    elif account["auth_type"] == "oauth":
        try:
            access_token = await refresh_oauth_token_if_needed(account)
            headers["Authorization"] = f"Bearer {access_token}"
        except Exception as e:
            return False, f"OAuth 認證失敗: {str(e)}", 401
    else:
        return False, f"未知的認證方式: {account.get('auth_type')}", 400
        
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": "image/png",
                            "data": img_b64
                        }
                    },
                    {
                        "text": prompt
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.1,  # 低溫度確保精確辨識原文，減少幻覺
            "maxOutputTokens": 8192
        }
    }
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, headers=headers, params=params, json=payload)
            status_code = resp.status_code
            
            if status_code == 200:
                result_json = resp.json()
                try:
                    candidates = result_json.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        text_output = "".join([part.get("text", "") for part in parts])
                        return True, text_output.strip(), 200
                    else:
                        finish_reason = result_json.get("promptFeedback", {})
                        return False, f"模型未回傳結果 (原因: {finish_reason})", 200
                except Exception as parse_err:
                    return False, f"解析模型回傳格式錯誤: {str(parse_err)}", 200
            elif status_code == 429:
                return False, f"觸發 Google AI Studio 速率或額度限制 (429 RESOURCE_EXHAUSTED): {resp.text}", 429
            else:
                return False, f"API 回傳錯誤 (代碼 {status_code}): {resp.text}", status_code
    except httpx.TimeoutException:
        return False, "連線逾時 (Timeout)", 408
    except Exception as e:
        return False, f"呼叫 API 發生異常: {str(e)}", 500

async def fetch_google_models(account: Dict[str, Any]) -> Tuple[bool, List[Dict[str, Any]], str]:
    """向 Google AI Studio API 動態抓取可用模型清單"""
    url = "https://generativelanguage.googleapis.com/v1beta/models"
    headers = {}
    params = {"pageSize": 100}
    
    if account["auth_type"] == "api_key":
        api_key = account.get("api_key", "").strip()
        if not api_key:
            return False, [], "API Key 為空"
        params["key"] = api_key
    elif account["auth_type"] == "oauth":
        try:
            access_token = await refresh_oauth_token_if_needed(account)
            headers["Authorization"] = f"Bearer {access_token}"
        except Exception as e:
            return False, [], f"OAuth 認證失敗: {str(e)}"
    else:
        return False, [], f"未知的認證方式: {account.get('auth_type')}"
        
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code != 200:
                return False, [], f"API 錯誤 ({resp.status_code}): {resp.text}"
            
            data = resp.json()
            raw_models = data.get("models", [])
            parsed_models = []
            
            for m in raw_models:
                methods = m.get("supportedGenerationMethods", [])
                name = m.get("name", "")
                if "generateContent" in methods and "gemini" in name.lower():
                    model_id = name.replace("models/", "")
                    display_name = m.get("displayName", model_id)
                    description = m.get("description", "")
                    parsed_models.append({
                        "id": model_id,
                        "name": f"{display_name} ({model_id})",
                        "description": description
                    })
            
            # 智慧排序：優先推薦 Flash 模型，其次 Pro 等
            def sort_key(item):
                mid = item["id"].lower()
                is_flash = 0 if "flash" in mid else 1
                return (is_flash, item["name"])
                
            parsed_models.sort(key=sort_key)
            return True, parsed_models, "成功"
    except Exception as e:
        return False, [], f"網路請求異常: {str(e)}"

