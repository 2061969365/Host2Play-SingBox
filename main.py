import os
import sys
import time
import random
import html
import requests
import tempfile
import subprocess
from datetime import datetime, timezone, timedelta
from camoufox.sync_api import Camoufox

try:
    import speech_recognition as sr
    from pydub import AudioSegment
except ImportError:
    pass

# ==============================================================================
# 配置区域
# ==============================================================================
RENEW_URLS = [
    "https://host2play.gratis/server/renew?i=e1a5dcbf-b580-49a4-a4a9-355e8285641b",
]

MAX_CAPTCHA = 5
MAX_RENEW_RETRIES_PER_URL = 50

# ==============================================================================
# 自定义异常
# ==============================================================================
class CaptchaBlocked(Exception):
    pass

# ==============================================================================
# 统一日志
# ==============================================================================
def log(msg, level="INFO"):
    prefix = {"INFO": "[INFO]", "WARN": "[WARN]", "ERROR": "[ERROR]"}.get(level, "[INFO]")
    print(f"{prefix} {msg}", flush=True)

# ==============================================================================
# Telegram 通知
# ==============================================================================
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
            response = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption, "parse_mode": parse_mode},
                files={"photo": photo_file},
                timeout=30,
            )
        response.raise_for_status()
        log("Telegram 图片通知发送成功")
    except Exception as e:
        log(f"Telegram 图片通知异常: {e}", "ERROR")

# ==============================================================================
# 页面元素提取
# ==============================================================================
def safe_locate(frame_or_page, selector, timeout_ms=3000):
    try:
        el = frame_or_page.wait_for_selector(selector, timeout=timeout_ms, state='visible')
        return el
    except Exception:
        return None

def get_server_name(page):
    el = safe_locate(page, '#serverName', 2000)
    if el:
        try:
            return (el.text_content() or "").strip()
        except Exception:
            pass
    return "未知"

def get_expire_time(page):
    el = safe_locate(page, '#expireDate', 2000)
    if el:
        try:
            return (el.text_content() or "").strip()
        except Exception:
            pass
    for text in ['Expires in:', 'Deletes on:']:
        try:
            el = page.get_by_text(text, exact=False)
            el.wait_for(state='visible', timeout=1000)
            t = (el.text_content() or "").strip()
            if ":" in t:
                return t.split(":", 1)[1].strip()
            if t:
                return t
        except Exception:
            pass
    return "未知"

# ==============================================================================
# 构建通知
# ==============================================================================
def build_notification(success, url, server_name, old_expire, new_expire=None, failure_reason=""):
    if success:
        lines = [
            "✅ 续订成功",
            "",
            f"服务器：{server_name}",
            f"到期: {old_expire} -> {new_expire}",
            f"URL: {url}",
        ]
    else:
        lines = [
            "❌ 续订失败",
            "",
            f"服务器：{server_name}",
            f"URL: {url}",
        ]
        if failure_reason:
            lines.append(f"失败原因: {failure_reason}")
    lines.append("")
    lines.append("Host2Play Auto Renew")
    return "\n".join(lines)

def get_current_ip():
    try:
        return requests.get("https://api.ipify.org", timeout=10).text
    except Exception:
        return "未知"

def capture_page_screenshot(page, file_name, extra_info=""):
    try:
        if page is None:
            log("页面为空，无法截图", "WARN")
            return None

        current_ip = get_current_ip()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        overlay_html = f'''
        <div id="info-overlay" style="
            position: fixed;
            top: 10px;
            left: 10px;
            background: rgba(0,0,0,0.85);
            color: #00ff00;
            padding: 15px;
            border-radius: 8px;
            font-family: monospace;
            font-size: 14px;
            z-index: 999999;
            border: 2px solid #00ff00;
            max-width: 400px;
        ">
            <div style="color: #ffcc00; font-weight: bold; margin-bottom: 8px;">[DEBUG INFO]</div>
            <div>IP: {current_ip}</div>
            <div>时间: {timestamp}</div>
            {f'<div style="color: #ff6666;">{extra_info}</div>' if extra_info else ''}
        </div>
        '''

        page.evaluate(f'''
            const old = document.getElementById('info-overlay');
            if (old) old.remove();
            document.body.insertAdjacentHTML('beforeend', `{overlay_html}`);
        ''')

        time.sleep(0.5)
        page.screenshot(path=file_name)

        page.evaluate('const el = document.getElementById("info-overlay"); if(el) el.remove();')

        log(f"截图已保存: {file_name} (IP: {current_ip})")
        return file_name
    except Exception as e:
        log(f"截图失败: {e}", "WARN")
        return None

# ==============================================================================
# WARP 重连
# ==============================================================================
def restart_warp():
    log("正在重启 WARP 以更换 IP...")
    try:
        old_ip = get_current_ip()
        log(f"当前 IP: {old_ip}")
    except Exception:
        old_ip = "未知"

    try:
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "disconnect"],
                      check=False, timeout=30, capture_output=True)
        time.sleep(3)
        try:
            subprocess.run(["sudo", "warp-cli", "--accept-tos", "registration", "delete"],
                          check=True, timeout=30, capture_output=True)
        except subprocess.CalledProcessError:
            log("删除注册失败（可能未注册）", "WARN")
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "registration", "new"],
                      check=True, timeout=30, capture_output=True)
        time.sleep(3)
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "connect"],
                      check=True, timeout=30, capture_output=True)
        time.sleep(10)
        new_ip = get_current_ip()
        log(f"WARP 重连成功，新 IP: {old_ip} -> {new_ip}")
        return True
    except Exception as e:
        log(f"WARP 重连失败: {e}", "ERROR")
        return False

# ==============================================================================
# reCAPTCHA 辅助函数
# ==============================================================================
def find_recaptcha_frame(page, kind):
    try:
        for frame in page.frames:
            frame_url = frame.url or ""
            if "recaptcha" in frame_url and kind in frame_url:
                return frame
    except Exception:
        pass
    return None

def is_recaptcha_solved(page):
    try:
        for frame in page.frames:
            try:
                token = frame.evaluate("return document.querySelector(\"textarea[name='g-recaptcha-response']\")?.value")
                if token and len(token) > 30:
                    return True
            except Exception:
                pass
    except Exception:
        pass
    anchor = find_recaptcha_frame(page, "anchor")
    if anchor:
        try:
            checked = anchor.evaluate("return document.querySelector('#recaptcha-anchor')?.getAttribute('aria-checked') === 'true'")
            if checked:
                return True
        except Exception:
            pass
    return False

def is_blocked(page):
    bframe = find_recaptcha_frame(page, "bframe")
    if not bframe:
        return False
    try:
        result = bframe.evaluate("""
            const h = document.querySelector('.rc-doscaptcha-header-text');
            const hText = h ? h.textContent : '';
            const isTryAgain = hText.toLowerCase().includes('try again later');

            const e = document.querySelector('.rc-audiochallenge-error-message');
            const isVisible = e && e.offsetParent !== null;

            return {
                headerText: hText,
                isTryAgain: isTryAgain,
                errorVisible: isVisible,
                blocked: isTryAgain || isVisible
            };
        """)

        if result.get('blocked'):
            log(f"[BLOCKED DETECTED] header: '{result.get('headerText')}', errorVisible: {result.get('errorVisible')}", "WARN")

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
    checkbox = safe_locate(anchor, '#recaptcha-anchor', 3000)
    if not checkbox:
        raise RuntimeError("未找到 reCAPTCHA 复选框")
    checkbox.hover()
    time.sleep(random.uniform(0.2, 0.5))
    try:
        checkbox.click()
    except Exception:
        checkbox.click(force=True)
    time.sleep(3)
    if is_blocked(page):
        raise CaptchaBlocked("点击复选框后检测到 IP 被封锁")

def switch_to_audio(page):
    bframe = find_recaptcha_frame(page, "bframe")
    if not bframe:
        return False
    try:
        input_box = safe_locate(bframe, '#audio-response', 1000)
        if input_box and input_box.is_visible():
            return True
    except Exception:
        pass
    for attempt in range(3):
        try:
            audio_btn = safe_locate(bframe, '#recaptcha-audio-button', 3000)
            if audio_btn:
                try:
                    audio_btn.click()
                except Exception:
                    audio_btn.click(force=True)
                time.sleep(3)
                if is_blocked(page):
                    raise CaptchaBlocked("点击音频按钮后检测到 IP 被封锁")
                input_box = safe_locate(bframe, '#audio-response', 1000)
                if input_box and input_box.is_visible():
                    return True
        except CaptchaBlocked:
            raise
        except Exception:
            pass
        try:
            bframe.evaluate("""
                const btn = document.querySelector('#recaptcha-audio-button');
                if (btn) btn.click();
            """)
            time.sleep(3)
            if is_blocked(page):
                raise CaptchaBlocked("JS点击音频按钮后检测到 IP 被封锁")
            input_box = safe_locate(bframe, '#audio-response', 1000)
            if input_box and input_box.is_visible():
                return True
        except CaptchaBlocked:
            raise
        except Exception:
            pass
        time.sleep(2)
    return False

def is_audio_mode(page):
    bframe = find_recaptcha_frame(page, "bframe")
    if not bframe:
        return False
    try:
        input_box = safe_locate(bframe, '#audio-response', 1000)
        return bool(input_box and input_box.is_visible())
    except Exception:
        return False

def get_audio_url(page):
    bframe = find_recaptcha_frame(page, "bframe")
    if not bframe:
        return None
    for _ in range(10):
        try:
            link = safe_locate(bframe, '.rc-audiochallenge-tdownload-link', 1000)
            if link:
                href = link.get_attribute('href')
                if href and len(href) > 10:
                    return html.unescape(href)
            link = safe_locate(bframe, '.rc-audiochallenge-ndownload-link', 1000)
            if link:
                href = link.get_attribute('href')
                if href and len(href) > 10:
                    return html.unescape(href)
            audio = safe_locate(bframe, '#audio-source', 1000)
            if audio:
                src = audio.get_attribute('src')
                if src and len(src) > 10:
                    return html.unescape(src)
        except Exception:
            pass
        time.sleep(1)
    return None

def reload_challenge(page):
    bframe = find_recaptcha_frame(page, "bframe")
    if not bframe:
        return
    try:
        reload_btn = safe_locate(bframe, '#recaptcha-reload-button', 2000)
        if reload_btn:
            try:
                reload_btn.click()
            except Exception:
                reload_btn.click(force=True)
            time.sleep(3)
    except Exception:
        pass

def fill_and_verify(page, text):
    bframe = find_recaptcha_frame(page, "bframe")
    if not bframe:
        return False
    try:
        input_box = safe_locate(bframe, '#audio-response', 2000)
        if not input_box:
            return False
        input_box.click()
        input_box.fill('')
        input_box.fill(text)
    except Exception:
        return False
    time.sleep(random.uniform(0.5, 1.5))
    try:
        verify_btn = safe_locate(bframe, '#recaptcha-verify-button', 2000)
        if verify_btn:
            try:
                verify_btn.click()
            except Exception:
                verify_btn.click(force=True)
    except Exception:
        pass
    return True

def download_audio(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.google.com/",
    }
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
        try:
            os.remove(wav_path)
        except Exception:
            pass
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
        if is_recaptcha_solved(page):
            return True
        if is_blocked(page):
            raise CaptchaBlocked("IP 被 Google reCAPTCHA 封锁")

        if i == 0:
            click_recaptcha_checkbox(page)
            time.sleep(2)
            if is_recaptcha_solved(page):
                return True

        if not is_audio_mode(page):
            if not switch_to_audio(page):
                time.sleep(3)
                if not switch_to_audio(page):
                    click_recaptcha_checkbox(page)
                    time.sleep(3)
                    continue
            time.sleep(random.uniform(2, 4))

        if is_blocked(page):
            raise CaptchaBlocked("音频模式检测到 IP 被封锁")

        audio_url = get_audio_url(page)
        if not audio_url:
            reload_challenge(page)
            continue

        mp3 = download_audio(audio_url)
        if not mp3:
            dl_fails += 1
            if dl_fails >= 3:
                raise RuntimeError("音频连续下载失败")
            reload_challenge(page)
            time.sleep(random.uniform(3, 6))
            continue
        dl_fails = 0

        text = recognize_audio(mp3)
        try:
            os.remove(mp3)
        except Exception:
            pass
        if not text:
            reload_challenge(page)
            time.sleep(3)
            continue

        log(f"识别结果: [{text}]")
        fill_and_verify(page, text)
        time.sleep(5)
        if is_recaptcha_solved(page):
            return True
        reload_challenge(page)
        time.sleep(random.uniform(2, 4))
    raise RuntimeError("验证码达到最大尝试次数")

# ==============================================================================
# 单个 URL 续期流程 —— 所有重试共享一个 Camoufox 实例
# ==============================================================================
def renew_single_url(url):
    success = False
    server_name = "未知"
    old_expire = "未知"
    new_expire = "未知"
    screenshot_path = None
    failure_reason = ""
    screenshot_dir = "output/screenshots"
    os.makedirs(screenshot_dir, exist_ok=True)

    try:
        with Camoufox(headless=True) as browser:
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

                    server_name = get_server_name(page)
                    old_expire = get_expire_time(page)
                    log(f"服务器: {server_name}, 到期时间: {old_expire}")

                    page.evaluate("""
                        const cssSelectors = ['ins.adsbygoogle', 'iframe[src*="ads"]', '.modal-backdrop'];
                        cssSelectors.forEach(sel => {
                            document.querySelectorAll(sel).forEach(el => el.remove());
                        });
                    """)
                    time.sleep(2)
                    try:
                        consent_btn = page.get_by_role('button', name='Consent')
                        consent_btn.wait_for(state='visible', timeout=2000)
                        consent_btn.click()
                        time.sleep(3)
                    except Exception:
                        pass

                    for _ in range(3):
                        scroll_y = random.randint(200, 600)
                        page.mouse.wheel(0, scroll_y)
                        time.sleep(random.uniform(0.5, 1.5))
                        page.mouse.move(random.randint(100, 800), random.randint(100, 500))
                        time.sleep(random.uniform(0.5, 1.0))
                    time.sleep(random.uniform(1.0, 2.0))

                    log("打开续期弹窗...")
                    renew_btn1 = safe_locate(page, 'xpath=//button[contains(text(), "Renew server")]', 3000)
                    if renew_btn1:
                        try:
                            renew_btn1.click()
                        except Exception:
                            renew_btn1.click(force=True)
                    else:
                        page.evaluate("document.querySelectorAll('button').forEach(b => {if(b.textContent.includes('Renew server')) b.click();});")
                    time.sleep(3)

                    for _ in range(8):
                        try:
                            el = page.get_by_text('Expires in:', exact=False)
                            el.wait_for(state='visible', timeout=500)
                            break
                        except Exception:
                            pass
                        try:
                            el = page.get_by_text('Deletes on:', exact=False)
                            el.wait_for(state='visible', timeout=500)
                            break
                        except Exception:
                            pass
                        time.sleep(1)

                    renew_btn2 = safe_locate(page, 'xpath=//button[contains(text(), "Renew server")]', 2000)
                    if renew_btn2:
                        try:
                            renew_btn2.click()
                        except Exception:
                            renew_btn2.click(force=True)
                    time.sleep(random.uniform(7, 10))

                    anchor_frame = find_recaptcha_frame(page, "anchor")
                    if not anchor_frame:
                        log("未检测到 reCAPTCHA，检查是否已直接成功")
                        new_expire = get_expire_time(page)
                        if new_expire != old_expire and new_expire != "未知":
                            success = True
                        else:
                            failure_reason = "未找到 reCAPTCHA 验证码区域"
                        screenshot_path = capture_page_screenshot(page, os.path.join(screenshot_dir, f"host2play-{server_name}-{'success' if success else 'fail'}.png"), f"状态: {'成功' if success else '失败'}" + (f" | 原因: {failure_reason}" if failure_reason else ""))
                        break

                    log("启动 reCAPTCHA 音频破解...")
                    try:
                        solved = solve_recaptcha(page)
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

                    if not solved:
                        failure_reason = "未通过 reCAPTCHA 验证"
                        screenshot_path = capture_page_screenshot(page, os.path.join(screenshot_dir, f"host2play-{server_name}-fail.png"), f"状态: 失败 | 原因: {failure_reason}")
                        break

                    log("点击最终 Renew 按钮")
                    final_btn = safe_locate(page, 'xpath=//button[normalize-space(text())="Renew"]', 3000)
                    if final_btn:
                        try:
                            final_btn.click()
                        except Exception:
                            final_btn.click(force=True)
                        time.sleep(10)
                        new_expire = get_expire_time(page)
                        if new_expire != old_expire and new_expire != "未知":
                            log(f"到期时间已更新: {old_expire} -> {new_expire}")
                            success = True
                        else:
                            page_text = (page.content() or "").lower()
                            if any(w in page_text for w in ["successfully", "renewed"]):
                                success = True
                            else:
                                failure_reason = "续期后未检测到成功标志"
                    else:
                        failure_reason = "找不到最终 Renew 按钮"
                    screenshot_path = capture_page_screenshot(page, os.path.join(screenshot_dir, f"host2play-{server_name}-{'success' if success else 'fail'}.png"), f"状态: {'成功' if success else '失败'}" + (f" | 原因: {failure_reason}" if failure_reason else ""))
                    break

                except Exception as e:
                    log(f"续期尝试异常: {e}", "ERROR")
                    failure_reason = f"运行异常: {str(e)[:200]}"
                    restart_warp()
                    continue

    except Exception as e:
        log(f"Camoufox 启动失败: {e}", "ERROR")
        failure_reason = f"浏览器启动失败: {str(e)[:200]}"

    return success, server_name, old_expire, new_expire, screenshot_path, failure_reason

# ==============================================================================
# 主入口
# ==============================================================================
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
            caption = build_notification(True, url, server_name, old_expire, new_expire)
            total_success += 1
        else:
            caption = build_notification(False, url, server_name, old_expire, failure_reason=failure_reason)

        send_tg_photo(tg_token, tg_chat_id, screenshot, caption, parse_mode='HTML')

    log(f"全部完成，成功 {total_success}/{len(RENEW_URLS)} 个链接")
    if total_success < len(RENEW_URLS):
        sys.exit(1)

if __name__ == "__main__":
    main()
