import os, sys, time, random, html, json, tempfile, subprocess, requests
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright

try:
    import speech_recognition as sr
    from pydub import AudioSegment
except ImportError:
    pass

RENEW_URLS = [
    "https://host2play.gratis/server/renew?i=e1a5dcbf-b580-49a4-a4a9-355e8285641b",
]

MAX_CAPTCHA = 5
MAX_RENEW_RETRIES_PER_URL = 50

class CaptchaBlocked(Exception):
    pass

def log(msg, level="INFO"):
    prefix = {"INFO": "[INFO]", "WARN": "[WARN]", "ERROR": "[ERROR]"}.get(level, "[INFO]")
    print(f"{prefix} {msg}", flush=True)

def send_tg_photo(token, chat_id, photo_path, caption, parse_mode='HTML'):
    if not token or not chat_id:
        log("未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过通知。", "WARN")
        return
    if not photo_path or not os.path.exists(photo_path):
        log("未找到截图文件，跳过通知。", "WARN")
        return
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        with open(photo_path, "rb") as photo_file:
            response = requests.post(url, data={"chat_id": chat_id, "caption": caption, "parse_mode": parse_mode}, files={"photo": photo_file}, timeout=30)
        response.raise_for_status()
        log("Telegram 图片通知发送成功")
    except Exception as e:
        log(f"Telegram 图片通知异常: {e}", "ERROR")

def get_current_ip():
    try:
        return requests.get("https://api.ipify.org", timeout=10).text
    except Exception:
        return "未知"

def restart_warp():
    log("正在重启 WARP 以更换 IP...")
    old_ip = get_current_ip()
    log(f"当前 IP: {old_ip}")
    try:
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "disconnect"], check=False, timeout=30, capture_output=True)
        time.sleep(3)
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "registration", "delete"], check=True, timeout=30, capture_output=True)
        time.sleep(3)
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "registration", "new"], check=True, timeout=30, capture_output=True)
        time.sleep(3)
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "connect"], check=True, timeout=30, capture_output=True)
        time.sleep(10)
        new_ip = get_current_ip()
        log(f"WARP 重连成功，新 IP: {old_ip} -> {new_ip}")
        return True
    except Exception as e:
        log(f"WARP 重连失败: {e}", "ERROR")
        return False

def find_recaptcha_frame(page, kind):
    for frame in page.frames:
        if "recaptcha" in (frame.url or "") and kind in frame.url:
            return frame
    return None

def is_recaptcha_solved(page):
    try:
        token = page.evaluate("""
            (() => {
                const ta = document.querySelector('textarea[name="g-recaptcha-response"]');
                return ta ? ta.value : '';
            })()
        """)
        if token and len(token) > 30:
            return token  # return the actual token
    except Exception:
        pass
    return None

def is_blocked(page):
    bframe = find_recaptcha_frame(page, "bframe")
    if not bframe:
        return False
    try:
        result = bframe.evaluate("""
            (() => {
                const h = document.querySelector('.rc-doscaptcha-header-text');
                const hText = h ? h.textContent : '';
                const e = document.querySelector('.rc-audiochallenge-error-message');
                const isVisible = e && e.offsetParent !== null;
                return {
                    headerText: hText,
                    isTryAgain: hText.toLowerCase().includes('try again later'),
                    errorVisible: isVisible,
                    blocked: (hText.toLowerCase().includes('try again later')) || (e && e.offsetParent !== null)
                };
            })()
        """)
        if result.get('blocked'):
            log(f"[BLOCKED] header: '{result.get('headerText')}', errorVisible: {result.get('errorVisible')}", "WARN")
        return result.get('blocked', False)
    except Exception as e:
        log(f"检查封锁状态异常: {e}", "WARN")
        return False

def click_recaptcha_checkbox(page):
    anchor = find_recaptcha_frame(page, "anchor")
    if not anchor:
        for _ in range(120):
            anchor = find_recaptcha_frame(page, "anchor")
            if anchor:
                break
            time.sleep(1)
    if not anchor:
        raise RuntimeError("未找到 reCAPTCHA anchor frame")
    try:
        anchor.evaluate("document.querySelector('#recaptcha-anchor')?.click()")
    except Exception:
        pass
    time.sleep(3)
    if is_blocked(page):
        raise CaptchaBlocked("点击复选框后检测到 IP 被封锁")

def switch_to_audio(page):
    bframe = find_recaptcha_frame(page, "bframe")
    if not bframe:
        return False
    for attempt in range(3):
        try:
            bframe.evaluate("document.querySelector('#recaptcha-audio-button')?.click()")
            time.sleep(3)
            if is_blocked(page):
                raise CaptchaBlocked("点击音频按钮后检测到 IP 被封锁")
            input_box = bframe.evaluate("document.querySelector('#audio-response') !== null")
            if input_box:
                return True
        except CaptchaBlocked:
            raise
        except Exception:
            pass
        time.sleep(2)
    return False

def get_audio_url(bframe):
    for _ in range(10):
        try:
            href = bframe.evaluate("""
                (() => {
                    const links = document.querySelectorAll('a');
                    for (const l of links) {
                        const h = l.getAttribute('href');
                        if (h && h.includes('google.com') && h.includes('audio')) return h;
                    }
                    const audio = document.querySelector('#audio-source');
                    if (audio) return audio.getAttribute('src');
                    const all = document.querySelectorAll('[src]');
                    for (const el of all) {
                        const s = el.getAttribute('src');
                        if (s && s.includes('google.com') && s.includes('audio')) return s;
                    }
                    return null;
                })()
            """)
            if href and len(href) > 10:
                return html.unescape(href)
        except Exception:
            pass
        time.sleep(1)
    return None

def download_audio(url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Referer": "https://www.google.com/"}
    urls = [url]
    if "recaptcha.net" in url:
        urls.append(url.replace("recaptcha.net", "www.google.com"))
    elif "google.com" in url:
        urls.append(url.replace("www.google.com", "recaptcha.net"))
    for audio_url in urls:
        try:
            r = requests.get(audio_url, headers=headers, timeout=30)
            r.raise_for_status()
            if len(r.content) < 1000:
                continue
            path = tempfile.mktemp(suffix=".mp3")
            with open(path, "wb") as f:
                f.write(r.content)
            return path
        except Exception:
            pass
    return None

def recognize_audio(mp3_path):
    try:
        wav_path = mp3_path.replace(".mp3", ".wav")
        AudioSegment.from_mp3(mp3_path).export(wav_path, format="wav")
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as src:
            audio_data = recognizer.record(src)
            text = recognizer.recognize_google(audio_data)
        try: os.remove(wav_path)
        except: pass
        return text
    except Exception:
        return None

def solve_recaptcha(page):
    start = time.time()
    while time.time() - start < 15:
        if find_recaptcha_frame(page, "anchor"):
            break
        time.sleep(1)
    else:
        raise RuntimeError("reCAPTCHA 加载超时")

    dl_fails = 0
    for i in range(MAX_CAPTCHA):
        token = is_recaptcha_solved(page)
        if token:
            return token
        if is_blocked(page):
            raise CaptchaBlocked("IP 被 Google reCAPTCHA 封锁")

        if i == 0:
            click_recaptcha_checkbox(page)
            time.sleep(2)
            token = is_recaptcha_solved(page)
            if token:
                return token

        if not switch_to_audio(page):
            click_recaptcha_checkbox(page)
            time.sleep(3)
            continue

        if is_blocked(page):
            raise CaptchaBlocked("音频模式检测到 IP 被封锁")

        bframe = find_recaptcha_frame(page, "bframe")
        audio_url = get_audio_url(bframe) if bframe else None
        if not audio_url:
            try: bframe.evaluate("document.querySelector('#recaptcha-reload-button')?.click()")
            except: pass
            continue

        mp3 = download_audio(audio_url)
        if not mp3:
            dl_fails += 1
            if dl_fails >= 3:
                raise RuntimeError("音频连续下载失败")
            try: bframe.evaluate("document.querySelector('#recaptcha-reload-button')?.click()")
            except: pass
            time.sleep(random.uniform(3, 6))
            continue
        dl_fails = 0

        text = recognize_audio(mp3)
        try: os.remove(mp3)
        except: pass
        if not text:
            try: bframe.evaluate("document.querySelector('#recaptcha-reload-button')?.click()")
            except: pass
            time.sleep(3)
            continue

        log(f"识别结果: [{text}]")
        try:
            bframe.evaluate(f"document.querySelector('#audio-response')?.value = '{text}'")
            bframe.evaluate("document.querySelector('#recaptcha-verify-button')?.click()")
        except: pass
        time.sleep(5)
        token = is_recaptcha_solved(page)
        if token:
            return token
        try: bframe.evaluate("document.querySelector('#recaptcha-reload-button')?.click()")
        except: pass
        time.sleep(random.uniform(2, 4))

    raise RuntimeError("验证码达到最大尝试次数")

def capture_screenshot(page, file_name, extra_info=""):
    try:
        current_ip = get_current_ip()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        overlay = f"""
        <div id="info-overlay" style="position:fixed;top:10px;left:10px;background:rgba(0,0,0,0.85);color:#00ff00;padding:15px;border-radius:8px;font-family:monospace;font-size:14px;z-index:999999;border:2px solid #00ff00;max-width:400px;">
            <div style="color:#ffcc00;font-weight:bold;margin-bottom:8px;">[DEBUG INFO]</div>
            <div>IP: {current_ip}</div>
            <div>时间: {timestamp}</div>
            {f'<div style="color:#ff6666;">{extra_info}</div>' if extra_info else ''}
        </div>"""
        page.evaluate(f"const o=document.getElementById('info-overlay');if(o)o.remove();document.body.insertAdjacentHTML('beforeend',`{overlay}`)")
        time.sleep(0.5)
        page.screenshot(path=file_name)
        page.evaluate("const el=document.getElementById('info-overlay');if(el)el.remove();")
        log(f"截图已保存: {file_name} (IP: {current_ip})")
        return file_name
    except Exception as e:
        log(f"截图失败: {e}", "WARN")
        return None

def renew_single_url(url):
    success = False
    server_name = "未知"
    old_expire = "未知"
    new_expire = "未知"
    screenshot_path = None
    failure_reason = ""
    screenshot_dir = "output/screenshots"
    old_expire_text = "未知"
    os.makedirs(screenshot_dir, exist_ok=True)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox']
            )
            context = browser.new_context(no_viewport=True)
            page = context.new_page()

            for attempt in range(1, MAX_RENEW_RETRIES_PER_URL + 1):
                log(f"{'='*20} 续期尝试 {attempt}/{MAX_RENEW_RETRIES_PER_URL} {'='*20}")
                try:
                    log(f"访问: {url}")
                    for retry in range(3):
                        try:
                            page.goto(url, wait_until='domcontentloaded', timeout=30000)
                            break
                        except Exception:
                            if retry < 2:
                                time.sleep(2)
                            else:
                                raise
                    time.sleep(random.uniform(5, 8))

                    try: page.evaluate("document.querySelectorAll('ins.adsbygoogle,iframe[src*=\"ads\"],.modal-backdrop').forEach(el=>el.remove())")
                    except: pass
                    time.sleep(2)

                    # Get CSRF token
                    csrf_token = page.evaluate("document.querySelector('meta[name=\"csrf-token\"]')?.getAttribute('content') or ''")
                    log(f"CSRF Token: {csrf_token[:20]}...")

                    # Get server info
                    server_name = page.evaluate("document.getElementById('serverName')?.textContent?.trim() || '未知'")
                    old_expire_text = page.evaluate("document.getElementById('expireDate')?.textContent?.trim() || '未知'")
                    old_expire = old_expire_text
                    log(f"服务器: {server_name}, 到期: {old_expire_text}")

                    # Click "Renew server" button to open modal
                    log("打开续期弹窗...")
                    page.evaluate("""
                        const btns = document.querySelectorAll('button');
                        for (const b of btns) {
                            if (b.textContent.includes('Renew server')) { b.click(); break; }
                        }
                    """)
                    time.sleep(random.uniform(5, 8))

                    # Wait for reCAPTCHA
                    anchor_frame = find_recaptcha_frame(page, "anchor")
                    if not anchor_frame:
                        log("未检测到 reCAPTCHA")
                        failure_reason = "未找到 reCAPTCHA"
                        break

                    # Solve reCAPTCHA
                    log("启动 reCAPTCHA 音频破解...")
                    try:
                        captcha_token = solve_recaptcha(page)
                    except CaptchaBlocked:
                        log("IP 被封锁，换 IP 后重试", "WARN")
                        failure_reason = "IP 被 reCAPTCHA 封锁"
                        restart_warp()
                        continue
                    except RuntimeError as e:
                        log(f"reCAPTCHA 破解失败: {e}", "WARN")
                        failure_reason = str(e)[:100]
                        restart_warp()
                        continue

                    if not captcha_token:
                        failure_reason = "未通过 reCAPTCHA 验证"
                        break

                    log(f"reCAPTCHA token 获取成功 ({len(captcha_token)} chars)")

                    # Directly POST to the renewal API
                    log(f"直接 POST 到 /publicapis/renewServer...")
                    renew_api = f"/publicapis/renewServer?i={url.split('?i=')[-1]}"
                    resp = page.evaluate(f"""
                        async (captchaToken) => {{
                            try {{
                                const r = await fetch('{renew_api}', {{
                                    method: 'POST',
                                    headers: {{'CSRF-Token': '{csrf_token}', 'Content-Type': 'application/json'}},
                                    body: JSON.stringify({{captcha: captchaToken}})
                                }});
                                const data = await r.json();
                                return JSON.stringify(data);
                            }} catch(e) {{ return 'error: ' + e.message; }}
                        }}
                    """, captcha_token)
                    log(f"API 响应: {resp}")

                    if '"success":1' in resp or '"success": 1' in resp:
                        success = True
                        time.sleep(2)
                        new_expire_text = page.evaluate("document.getElementById('expireDate')?.textContent?.trim() || '未知'")
                        new_expire = new_expire_text
                        log(f"续期成功！到期: {new_expire_text}")
                    else:
                        failure_reason = f"API 返回失败: {resp}"

                    screenshot_path = capture_screenshot(page, os.path.join(screenshot_dir, f"host2play-{server_name}-{'success' if success else 'fail'}.png"),
                        f"状态: {'成功' if success else '失败'}" + (f" | {failure_reason}" if failure_reason else ""))
                    break

                except Exception as e:
                    log(f"续期尝试异常: {e}", "ERROR")
                    failure_reason = f"运行异常: {str(e)[:200]}"
                    restart_warp()
                    continue

            browser.close()

    except Exception as e:
        log(f"浏览器启动失败: {e}", "ERROR")
        failure_reason = f"浏览器启动失败: {str(e)[:200]}"

    return success, server_name, old_expire, new_expire, screenshot_path, failure_reason

def main():
    tg_token = os.getenv("TG_BOT_TOKEN")
    tg_chat_id = os.getenv("TG_CHAT_ID")
    if not RENEW_URLS:
        log("请在 RENEW_URLS 列表中添加续期链接", "ERROR")
        sys.exit(1)

    total_success = 0
    for idx, url in enumerate(RENEW_URLS, 1):
        log(f"{'#'*60}")
        log(f"处理第 {idx} 个链接: {url}")
        log(f"{'#'*60}")

        success, server_name, old_expire, new_expire, screenshot, failure_reason = renew_single_url(url)

        if success:
            caption = f"✅ 续订成功\n\n服务器：{server_name}\n到期: {old_expire} -> {new_expire}\nURL: {url}\n\nHost2Play Auto Renew"
            total_success += 1
        else:
            caption = f"❌ 续订失败\n\n服务器：{server_name}\nURL: {url}\n失败原因: {failure_reason}\n\nHost2Play Auto Renew"

        send_tg_photo(tg_token, tg_chat_id, screenshot, caption, parse_mode='HTML')

    log(f"全部完成，成功 {total_success}/{len(RENEW_URLS)} 个链接")
    if total_success < len(RENEW_URLS):
        sys.exit(1)

if __name__ == "__main__":
    main()
