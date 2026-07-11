import os
import sys
import time
import json
import re
import base64
import random
import html
import tempfile
import subprocess
import signal
import socket
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote
from xvfbwrapper import Xvfb
from DrissionPage import ChromiumPage, ChromiumOptions
import requests

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

WGCF_URL = "https://github.com/ViRb3/wgcf/releases/download/v2.2.22/wgcf_2.2.22_linux_amd64"
SINGBOX_PORT = 10809
WORK_DIR = Path("work")
WGCF_PATH = WORK_DIR / "wgcf"
WGCF_ARCHIVE = WORK_DIR / "wgcf-account.toml"
WGCF_PROFILE = WORK_DIR / "wgcf-profile.conf"
SINGBOX_CONFIG = WORK_DIR / "sing-box-config.json"

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

def get_server_name(page):
    try:
        ele = page.ele('#serverName', timeout=2)
        if ele:
            return ele.text.strip()
    except Exception:
        pass
    return "未知"

def get_expire_time(page):
    try:
        ele = page.ele('#expireDate', timeout=2)
        if ele:
            return ele.text.strip()
    except Exception:
        pass
    selectors = ['text:Expires in:', 'text:Deletes on:']
    for selector in selectors:
        try:
            ele = page.ele(selector, timeout=1)
            if ele:
                text = (ele.text or "").strip()
                if ":" in text:
                    return text.split(":", 1)[1].strip()
                if text:
                    return text
        except Exception:
            pass
    return "未知"

def build_notification(success, url, server_name, old_expire, new_expire=None, failure_reason=""):
    if success:
        lines = ["\u2705 \u7eed\u8ba2\u6210\u529f", "", f"\u670d\u52a1\u5668\uff1a{server_name}", f"\u5230\u671f: {old_expire} -> {new_expire}", f"URL: {url}"]
    else:
        lines = ["\u274c \u7eed\u8ba2\u5931\u8d25", "", f"\u670d\u52a1\u5668\uff1a{server_name}", f"URL: {url}"]
        if failure_reason:
            lines.append(f"\u5931\u8d25\u539f\u56e0: {failure_reason}")
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
        overlay_html = f'''<div id="info-overlay" style="position:fixed;top:10px;left:10px;background:rgba(0,0,0,0.85);color:#00ff00;padding:15px;border-radius:8px;font-family:monospace;font-size:14px;z-index:999999;border:2px solid #00ff00;max-width:400px;"><div style="color:#ffcc00;font-weight:bold;margin-bottom:8px;">[DEBUG INFO]</div><div>IP: {current_ip}</div><div>\u65f6\u95f4: {timestamp}</div>{f'<div style="color:#ff6666;">{extra_info}</div>' if extra_info else ''}</div>'''
        page.run_js(f'''
            const old = document.getElementById('info-overlay');
            if (old) old.remove();
            document.body.insertAdjacentHTML('beforeend', `{overlay_html}`);
        ''')
        time.sleep(0.5)
        page.get_screenshot(path=file_name)
        page.run_js('const el = document.getElementById("info-overlay"); if(el) el.remove();')
        log(f"\u622a\u56fe\u5df2\u4fdd\u5b58: {file_name} (IP: {current_ip})")
        return file_name
    except Exception as e:
        log(f"\u622a\u56fe\u5931\u8d25: {e}", "WARN")
        return None

def find_recaptcha_frame(page, kind):
    try:
        for frame in page.get_frames():
            frame_url = frame.url or ""
            if "recaptcha" in frame_url and kind in frame_url:
                return frame
    except Exception:
        pass
    return None

def is_recaptcha_solved(page):
    try:
        for frame in page.get_frames():
            try:
                token = frame.run_js("return document.querySelector(\"textarea[name='g-recaptcha-response']\")?.value")
                if token and len(token) > 30:
                    return True
            except Exception:
                pass
    except Exception:
        pass
    anchor = find_recaptcha_frame(page, "anchor")
    if anchor:
        try:
            checked = anchor.run_js("return document.querySelector('#recaptcha-anchor')?.getAttribute('aria-checked') === 'true'")
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
        result = bframe.run_js("""
            const h = document.querySelector('.rc-doscaptcha-header-text');
            const hText = h ? h.textContent : '';
            const isTryAgain = hText.toLowerCase().includes('try again later');
            const e = document.querySelector('.rc-audiochallenge-error-message');
            const isVisible = e && e.offsetParent !== null;
            return { headerText: hText, isTryAgain: isTryAgain, errorVisible: isVisible, blocked: isTryAgain || isVisible };
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
    checkbox = anchor.ele('#recaptcha-anchor', timeout=3)
    if not checkbox:
        raise RuntimeError("未找到 reCAPTCHA 复选框")
    page.actions.move_to(checkbox, duration=random.uniform(0.4, 1.0))
    time.sleep(random.uniform(0.2, 0.5))
    try:
        checkbox.click()
    except Exception:
        checkbox.click(by_js=True)
    time.sleep(3)
    if is_blocked(page):
        raise CaptchaBlocked("点击复选框后检测到 IP 被封锁")

def switch_to_audio(page):
    bframe = find_recaptcha_frame(page, "bframe")
    if not bframe:
        return False
    try:
        input_box = bframe.ele('#audio-response', timeout=1)
        if input_box and input_box.states.is_displayed:
            return True
    except Exception:
        pass
    for attempt in range(3):
        try:
            audio_btn = bframe.ele('#recaptcha-audio-button', timeout=3)
            if audio_btn:
                try:
                    audio_btn.click()
                except Exception:
                    audio_btn.click(by_js=True)
                time.sleep(3)
                if is_blocked(page):
                    raise CaptchaBlocked("点击音频按钮后检测到 IP 被封锁")
                input_box = bframe.ele('#audio-response', timeout=1)
                if input_box and input_box.states.is_displayed:
                    return True
        except CaptchaBlocked:
            raise
        except Exception:
            pass
        try:
            bframe.run_js("const btn = document.querySelector('#recaptcha-audio-button'); if (btn) btn.click();")
            time.sleep(3)
            if is_blocked(page):
                raise CaptchaBlocked("JS点击音频按钮后检测到 IP 被封锁")
            input_box = bframe.ele('#audio-response', timeout=1)
            if input_box and input_box.states.is_displayed:
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
        input_box = bframe.ele('#audio-response', timeout=1)
        return bool(input_box and input_box.states.is_displayed)
    except Exception:
        return False

def get_audio_url(page):
    bframe = find_recaptcha_frame(page, "bframe")
    if not bframe:
        return None
    for _ in range(10):
        try:
            link = bframe.ele('.rc-audiochallenge-tdownload-link', timeout=1)
            if link:
                href = link.attr('href')
                if href and len(href) > 10:
                    return html.unescape(href)
            link = bframe.ele('.rc-audiochallenge-ndownload-link', timeout=1)
            if link:
                href = link.attr('href')
                if href and len(href) > 10:
                    return html.unescape(href)
            audio = bframe.ele('#audio-source', timeout=1)
            if audio:
                src = audio.attr('src')
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
        reload_btn = bframe.ele('#recaptcha-reload-button', timeout=2)
        if reload_btn:
            try:
                reload_btn.click()
            except Exception:
                reload_btn.click(by_js=True)
            time.sleep(3)
    except Exception:
        pass

def fill_and_verify(page, text):
    bframe = find_recaptcha_frame(page, "bframe")
    if not bframe:
        return False
    try:
        input_box = bframe.ele('#audio-response', timeout=2)
        if not input_box:
            return False
        input_box.click()
        input_box.clear()
        input_box.input(text)
    except Exception:
        return False
    time.sleep(random.uniform(0.5, 1.5))
    try:
        verify_btn = bframe.ele('#recaptcha-verify-button', timeout=2)
        if verify_btn:
            try:
                verify_btn.click()
            except Exception:
                verify_btn.click(by_js=True)
    except Exception:
        pass
    return True

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

def parse_proxy_uri(uri):
    uri = uri.strip()
    if uri.startswith("vless://"):
        return parse_vless(uri)
    elif uri.startswith("vmess://"):
        return parse_vmess(uri)
    elif uri.startswith("ss://") or uri.startswith("ssconf://"):
        return parse_ss(uri)
    elif uri.startswith("trojan://"):
        return parse_trojan(uri)
    elif uri.startswith("tuic://"):
        return parse_tuic(uri)
    return None

def parse_vless(uri):
    try:
        rest = uri[8:]
        if '#' in rest:
            rest, name = rest.rsplit('#', 1); name = unquote(name)
        else:
            name = "vless"
        if '?' in rest:
            addr, params_str = rest.split('?', 1)
        else:
            addr, params_str = rest, ""
        if '@' not in addr:
            return None
        uuid, host_port = addr.split('@', 1)
        if ':' in host_port:
            server, port = host_port.rsplit(':', 1); port = int(port)
        else:
            server, port = host_port, 443
        params = parse_qs(params_str)
        def p(k, d=None):
            v = params.get(k); return v[0] if v else d
        out = {"type": "vless", "tag": name, "server": server, "server_port": port, "uuid": uuid}
        flow = p("flow", "").lower()
        if flow in ("xtls-rprx-vision", "xtls-rprx-vision-udp443"):
            out["flow"] = flow
        pe = p("packetEncoding", "")
        if pe:
            out["packet_encoding"] = pe
        security = p("security", "")
        tls_enabled = security in ("tls", "reality", "")
        if security == "reality":
            out["tls"] = {"enabled": True, "server_name": p("sni", server), "utls": {"enabled": True, "fingerprint": p("fp", "chrome")}, "reality": {"enabled": True, "public_key": p("pbk", ""), "short_id": [p("sid", "0123456789abcdef")]}}
        elif tls_enabled:
            tls_cfg = {"enabled": True, "server_name": p("sni", server)}
            if p("insecure", "0") in ("1", "true", "yes") or p("allowInsecure", "0") in ("1", "true", "yes"):
                tls_cfg["insecure"] = True
            alpn = p("alpn", "")
            if alpn:
                tls_cfg["alpn"] = [a.strip() for a in alpn.split(",")]
            fp = p("fp", "chrome")
            if fp != "none":
                tls_cfg["utls"] = {"enabled": True, "fingerprint": fp}
            out["tls"] = tls_cfg
        ttype = p("type", "tcp")
        if ttype == "ws":
            ws = {"type": "ws", "path": p("path", "/"), "headers": {"Host": p("host", server)}}
            ed = p("ed", "")
            if ed:
                try: ws["max_early_data"] = int(ed)
                except: pass
            out["transport"] = ws
        elif ttype == "grpc":
            out["transport"] = {"type": "grpc", "service_name": p("serviceName", "")}
        elif ttype == "tcp" and p("headerType", "none") == "http":
            out["transport"] = {"type": "tcp", "header": {"type": "http", "request": {}, "response": {}}}
        return out
    except Exception as e:
        log(f"解析 vless URI 失败: {e}", "WARN")
        return None

def parse_vmess(uri):
    try:
        enc = uri[8:]
        pad = 4 - len(enc) % 4
        if pad != 4:
            enc += '=' * pad
        d = json.loads(base64.b64decode(enc).decode('utf-8'))
        out = {"type": "vmess", "tag": d.get("ps", "vmess"), "server": d.get("add", ""), "server_port": int(d.get("port", 443)), "uuid": d.get("id", ""), "alter_id": d.get("aid", 0), "security": d.get("scy", "auto") if d.get("scy", "auto") in ("auto", "aes-128-gcm", "chacha20-poly1305", "none", "zero") else "auto"}
        tls = d.get("tls", "")
        if tls in ("tls", "1", "true"):
            tc = {"enabled": True, "server_name": d.get("sni", d.get("add", ""))}
            if d.get("allowInsecure", "0") in ("1", "true", "yes"):
                tc["insecure"] = True
            fp = d.get("fp", "chrome")
            if fp.lower() != "none":
                tc["utls"] = {"enabled": True, "fingerprint": fp.lower()}
            out["tls"] = tc
        net = d.get("net", "ws")
        if net == "ws":
            tr = {"type": "ws", "path": d.get("path", "/"), "headers": {"Host": d.get("host", d.get("add", ""))}}
            ed = d.get("maxEarlyData", "")
            if ed:
                try: tr["max_early_data"] = int(ed)
                except: pass
            out["transport"] = tr
        elif net == "grpc":
            out["transport"] = {"type": "grpc", "service_name": d.get("serviceName", "")}
        elif net == "tcp" and d.get("headerType", "none") == "http":
            out["transport"] = {"type": "tcp", "header": {"type": "http", "request": {}, "response": {}}}
        return out
    except Exception as e:
        log(f"解析 vmess URI 失败: {e}", "WARN")
        return None

def parse_ss(uri):
    try:
        prefix = "ss://"
        if uri.startswith("ssconf://"):
            prefix = "ssconf://"
        rest = uri[len(prefix):]
        if '#' in rest:
            rest, name = rest.rsplit('#', 1); name = unquote(name)
        else:
            name = "ss"
        if '@' in rest:
            enc, sp = rest.split('@', 1)
            if ':' in sp:
                srv, prt = sp.rsplit(':', 1); prt = int(prt)
            else:
                srv, prt = sp, 443
            pad = 4 - len(enc) % 4
            if pad != 4: enc += '=' * pad
            dec = base64.b64decode(enc).decode('utf-8')
            if ':' in dec:
                method, pw = dec.split(':', 1)
            else:
                method, pw = "aes-256-gcm", dec
            return {"type": "shadowsocks", "tag": name, "server": srv, "server_port": prt, "method": method, "password": pw}
        else:
            pad = 4 - len(rest) % 4
            if pad != 4: rest += '=' * pad
            dec = base64.b64decode(rest).decode('utf-8')
            if '@' in dec:
                mp, sp = dec.rsplit('@', 1)
                method, pw = mp.split(':', 1)
                srv, prt = sp.rsplit(':', 1) if ':' in sp else (sp, 443); prt = int(prt)
                return {"type": "shadowsocks", "tag": name, "server": srv, "server_port": prt, "method": method, "password": pw}
        return None
    except Exception as e:
        log(f"解析 ss URI 失败: {e}", "WARN")
        return None

def parse_tuic(uri):
    try:
        rest = uri[7:]
        if '#' in rest:
            rest, name = rest.rsplit('#', 1); name = unquote(name)
        else:
            name = "tuic"
        if '?' in rest:
            addr, ps = rest.split('?', 1)
        else:
            addr, ps = rest, ""
        if '@' not in addr:
            return None
        creds, hp = addr.rsplit('@', 1)
        creds = unquote(creds)
        uuid, password = creds.split(':', 1) if ':' in creds else (creds, "")
        srv, prt = hp.rsplit(':', 1) if ':' in hp else (hp, 443); prt = int(prt)
        pp = parse_qs(ps)
        def p(k, d=None):
            v = pp.get(k); return v[0] if v else d
        out = {"type": "tuic", "tag": name, "server": srv, "server_port": prt, "uuid": uuid, "password": password, "congestion_control": p("congestion_control", "bbr"), "udp_relay_mode": p("udp_relay_mode", "native")}
        out["tls"] = {"enabled": True, "server_name": p("sni", srv), "insecure": p("allow_insecure", "0") in ("1", "true", "yes")}
        alpn = p("alpn", "")
        if alpn:
            out["tls"]["alpn"] = [a.strip() for a in alpn.split(",")]
        return out
    except Exception as e:
        log(f"解析 tuic URI 失败: {e}", "WARN")
        return None

def parse_trojan(uri):
    try:
        rest = uri[9:]
        if '#' in rest:
            rest, name = rest.rsplit('#', 1); name = unquote(name)
        else:
            name = "trojan"
        if '?' in rest:
            addr, ps = rest.split('?', 1)
        else:
            addr, ps = rest, ""
        if '@' not in addr:
            return None
        pw, hp = addr.split('@', 1)
        srv, prt = hp.rsplit(':', 1) if ':' in hp else (hp, 443); prt = int(prt)
        pp = parse_qs(ps)
        def p(k, d=None):
            v = pp.get(k); return v[0] if v else d
        out = {"type": "trojan", "tag": name, "server": srv, "server_port": prt, "password": pw}
        tc = {"enabled": True, "server_name": p("sni", srv)}
        if p("insecure", "0") in ("1", "true", "yes") or p("allowInsecure", "0") in ("1", "true", "yes"):
            tc["insecure"] = True
        alpn = p("alpn", "")
        if alpn:
            tc["alpn"] = [a.strip() for a in alpn.split(",")]
        fp = p("fp", "chrome")
        if fp != "none":
            tc["utls"] = {"enabled": True, "fingerprint": fp}
        out["tls"] = tc
        ttype = p("type", "tcp")
        if ttype == "ws":
            tr = {"type": "ws", "path": p("path", "/"), "headers": {"Host": p("host", srv)}}
            ed = p("ed", "")
            if ed:
                try: tr["max_early_data"] = int(ed)
                except: pass
            out["transport"] = tr
        elif ttype == "grpc":
            out["transport"] = {"type": "grpc", "service_name": p("serviceName", "")}
        return out
    except Exception as e:
        log(f"解析 trojan URI 失败: {e}", "WARN")
        return None

TEST_CONFIG = WORK_DIR / "sing-box-test-config.json"

def test_proxy_node(outbound, timeout=15):
    sb_bin = None
    for p in ["/usr/bin/sing-box", "/usr/local/bin/sing-box"]:
        if os.path.exists(p):
            sb_bin = p
            break
    if not sb_bin:
        log("sing-box 未安装，无法测试节点", "ERROR")
        return False, "sing-box 未安装", "0.00s"

    config = {
        "log": {"level": "error"},
        "inbounds": [{"type": "socks", "listen": "127.0.0.1", "listen_port": SINGBOX_PORT}],
        "outbounds": [outbound, {"type": "direct", "tag": "direct"}],
        "route": {"final": outbound.get("tag", "proxy")}
    }
    TEST_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    TEST_CONFIG.write_text(json.dumps(config, indent=2))

    proc = None
    try:
        proc = subprocess.Popen(
            [sb_bin, "run", "-c", str(TEST_CONFIG)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            preexec_fn=os.setsid
        )
        time.sleep(3)
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode()[:200]
            return False, f"sing-box 启动失败: {stderr}", "0.00s"
        for _ in range(10):
            try:
                s = socket.create_connection(("127.0.0.1", SINGBOX_PORT), timeout=1)
                s.close()
                break
            except Exception:
                time.sleep(1)
        else:
            return False, "SOCKS5 端口未监听", "0.00s"

        start = time.time()
        result = subprocess.run(
            ["curl", "-s", "-k", "-o", "/dev/null", "-w", "%{http_code}",
             "--max-time", str(timeout),
             "--socks5-hostname", f"127.0.0.1:{SINGBOX_PORT}",
             "https://host2play.gratis/"],
            capture_output=True, text=True, timeout=timeout + 5
        )
        elapsed = time.time() - start
        code = result.stdout.strip()
        ok = code not in ("", "000") and code.isdigit()
        if ok:
            return True, f"HTTP {code}", f"{elapsed:.2f}s"
        stderr = result.stderr.strip()[:60]
        return False, stderr or f"HTTP {code}", f"{elapsed:.2f}s"
    except subprocess.TimeoutExpired:
        return False, "超时", f"{timeout}.00s"
    except Exception as e:
        return False, str(e)[:40], "0.00s"
    finally:
        if proc:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=3)
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass
        try:
            TEST_CONFIG.unlink()
        except Exception:
            pass

class ProxyManager:
    def __init__(self, sub_url):
        self.sub_url = sub_url
        self.proxy_nodes = []
        self.current_proxy_idx = 0
        self.singbox_process = None
        self.warp_private_key = None
        self.warp_address = None
        self.warp_reserved = None

    def init(self):
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        self._download_wgcf()
        self._fetch_proxies()
        self._register_warp()
        self._start_singbox()

    def _download_wgcf(self):
        if WGCF_PATH.exists():
            return
        log("下载 wgcf...")
        r = requests.get(WGCF_URL, timeout=60)
        WGCF_PATH.write_bytes(r.content)
        WGCF_PATH.chmod(0o755)
        log("wgcf 下载完成")

    def _fetch_proxies(self):
        if not self.sub_url:
            log("未配置 SUB_URL，将直连 WARP（无代理隧道）", "WARN")
            return
        raw = self.sub_url.strip()
        uris = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or '://' not in line:
                continue
            if line.startswith(('http://', 'https://')):
                log(f"获取订阅: {line[:60]}...")
                try:
                    r = requests.get(line, timeout=30)
                    r.raise_for_status()
                    text = r.text.strip()
                    try:
                        pad = 4 - len(text) % 4
                        if pad != 4:
                            text += '=' * pad
                        decoded = base64.b64decode(text).decode('utf-8')
                        if any(c in decoded for c in [':', '/', '\n']):
                            text = decoded
                    except Exception:
                        pass
                    uris.extend(u.strip() for u in text.splitlines() if u.strip() and '://' in u)
                except Exception as e:
                    log(f"获取订阅失败: {e}", "WARN")
                    continue
            else:
                log(f"检测到节点 URI: {line[:50]}...")
                uris.append(line)
        if not uris:
            log("未解析到任何节点 URI", "WARN")
            return
        parsed = []
        for uri in uris:
            ob = parse_proxy_uri(uri)
            if ob:
                parsed.append(ob)
        log(f"解析到 {len(parsed)} 个节点，开始连通性测试...")
        valid = []
        for i, ob in enumerate(parsed):
            tag = ob.get('tag', '?')
            addr = f"{ob.get('server','?')}:{ob.get('server_port','?')}"
            log(f"测试节点 [{i+1}/{len(parsed)}] {tag} ({addr})...")
            ok, status, latency = test_proxy_node(ob)
            if ok:
                log(f"  -> 有效 ({status}, {latency})")
                valid.append(ob)
            else:
                log(f"  -> 失效 ({status}, {latency})", "WARN")
        self.proxy_nodes = valid
        log(f"节点测试完成: {len(self.proxy_nodes)} 有效 / {len(parsed)} 总计")
        if self.proxy_nodes:
            n = self.proxy_nodes[0]
            log(f"首个有效节点: [{n.get('tag','?')}] {n.get('type','?')} -> {n.get('server','?')}:{n.get('server_port','?')}")
        else:
            log("无有效节点，将直连 WARP", "WARN")

    def _register_warp(self):
        log("注册 WARP WireGuard...")
        for f in [WGCF_ARCHIVE, WGCF_PROFILE]:
            if f.exists():
                f.unlink()
        try:
            subprocess.run(f"echo Yes | {WGCF_PATH} register", shell=True, cwd=WORK_DIR, capture_output=True, timeout=90)
            subprocess.run(f"{WGCF_PATH} generate", shell=True, cwd=WORK_DIR, capture_output=True, timeout=60)
        except subprocess.TimeoutExpired:
            log("wgcf 超时", "ERROR")
            raise RuntimeError("wgcf 超时")
        except Exception as e:
            log(f"wgcf 失败: {e}", "ERROR")
            raise

        if not WGCF_PROFILE.exists():
            raise RuntimeError("wgcf-profile.conf 未生成")

        for line in WGCF_PROFILE.read_text().splitlines():
            t = line.strip()
            if t.startswith('PrivateKey = '):
                self.warp_private_key = t[len('PrivateKey = '):]
            elif t.startswith('Address = ') and not self.warp_address:
                self.warp_address = t[len('Address = '):]

        if WGCF_ARCHIVE.exists():
            content = WGCF_ARCHIVE.read_text()
            idx = content.find('reserved = ')
            if idx >= 0:
                start = content.find('[', idx)
                end = content.find(']', start)
                if start >= 0 and end > start:
                    inner = content[start+1:end]
                    self.warp_reserved = [int(s.strip()) for s in inner.split(',') if s.strip().lstrip('-').isdigit()]

        log(f"WARP 密钥: {self.warp_private_key[:16] if self.warp_private_key else 'N/A'}...")
        log(f"WARP 地址: {self.warp_address}")
        if self.warp_reserved:
            log(f"WARP reserved: {self.warp_reserved}")
        if not self.warp_private_key or not self.warp_address:
            raise RuntimeError("WARP 配置不完整")

    def _build_config(self):
        warp_wg = {
            "type": "wireguard",
            "tag": "warp-wg",
            "server": "engage.cloudflareclient.com",
            "server_port": 2408,
            "local_address": [self.warp_address],
            "private_key": self.warp_private_key,
            "peer_public_key": "bmXOC+F1FxEMF9dyiK2H5/1SUtzH0JuVo51h2wPfgyo=",
            "reserved": self.warp_reserved or [0, 0, 0],
            "mtu": 1280,
        }

        outbounds = [{"type": "direct", "tag": "direct"}]

        if self.current_proxy_idx < len(self.proxy_nodes):
            proxy = self.proxy_nodes[self.current_proxy_idx].copy()
            proxy["tag"] = "proxy-out"
            warp_wg["detour"] = "proxy-out"
            outbounds.insert(0, proxy)
            np = self.proxy_nodes[self.current_proxy_idx]
            log(f"WARP 隧道走代理: [{np.get('tag','?')}] {np.get('type','?')} -> {np.get('server','?')}:{np.get('server_port','?')}")
        else:
            log("WARP 直连（无代理隧道）")

        outbounds.append(warp_wg)

        config = {
            "log": {"level": "error"},
            "inbounds": [{"type": "socks", "listen": "127.0.0.1", "listen_port": SINGBOX_PORT}],
            "outbounds": outbounds,
            "route": {"final": "warp-wg"}
        }

        SINGBOX_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        SINGBOX_CONFIG.write_text(json.dumps(config, indent=2))
        log(f"sing-box 配置已写入: {SINGBOX_CONFIG}")

    def _start_singbox(self):
        self._build_config()
        self._stop_singbox()

        sb_bin = None
        for p in ["/usr/bin/sing-box", "/usr/local/bin/sing-box"]:
            if os.path.exists(p):
                sb_bin = p
                break
        if not sb_bin:
            raise RuntimeError("sing-box 未安装 (需先 apt install)")

        try:
            self.singbox_process = subprocess.Popen(
                [sb_bin, "run", "-c", str(SINGBOX_CONFIG)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                preexec_fn=os.setsid
            )
            time.sleep(3)
            if self.singbox_process.poll() is not None:
                stderr = self.singbox_process.stderr.read().decode()
                raise RuntimeError(f"sing-box 启动失败: {stderr[:300]}")
            log(f"sing-box 启动成功 (PID: {self.singbox_process.pid})")
            if not self.check_socks():
                log("SOCKS5 端口未监听，等待...")
                for _ in range(10):
                    time.sleep(1)
                    if self.check_socks():
                        break
                if not self.check_socks():
                    raise RuntimeError("SOCKS5 端口监听超时")
            log("SOCKS5 代理就绪 (127.0.0.1:{SINGBOX_PORT})")
        except Exception as e:
            log(f"sing-box 启动异常: {e}", "ERROR")
            raise

    def _stop_singbox(self):
        if self.singbox_process:
            try:
                os.killpg(os.getpgid(self.singbox_process.pid), signal.SIGTERM)
                self.singbox_process.wait(timeout=5)
            except Exception:
                try:
                    os.killpg(os.getpgid(self.singbox_process.pid), signal.SIGKILL)
                except Exception:
                    pass
            self.singbox_process = None
            log("sing-box 已停止")

    def restart(self):
        log("重新注册 WARP + 重启 sing-box...")
        self._stop_singbox()
        self.warp_address = None
        self.warp_private_key = None
        self.warp_reserved = None
        self._register_warp()
        self._start_singbox()

    def switch_proxy_and_restart(self):
        if not self.proxy_nodes:
            log("无有效代理节点，回退到直连 WARP", "WARN")
            self._stop_singbox()
            self.current_proxy_idx = 9999
            self._start_singbox()
            return True
        self._stop_singbox()
        self.current_proxy_idx = (self.current_proxy_idx + 1) % len(self.proxy_nodes)
        log(f"切换到代理 [{self.current_proxy_idx + 1}/{len(self.proxy_nodes)}]")
        self._start_singbox()
        return True

    def stop(self):
        self._stop_singbox()

    def check_socks(self):
        try:
            s = socket.create_connection(("127.0.0.1", SINGBOX_PORT), timeout=2)
            s.close()
            return True
        except Exception:
            return False

def renew_single_url(url, proxy_mgr):
    success = False
    server_name = "未知"
    old_expire = "未知"
    new_expire = "未知"
    screenshot_path = None
    failure_reason = ""
    screenshot_dir = "output/screenshots"
    os.makedirs(screenshot_dir, exist_ok=True)

    vdisplay = Xvfb(width=1280, height=720, colordepth=24)
    vdisplay.start()

    try:
        for attempt in range(1, MAX_RENEW_RETRIES_PER_URL + 1):
            log(f"{'='*20} 续期尝试 {attempt}/{MAX_RENEW_RETRIES_PER_URL} {'='*20}")
            if not proxy_mgr.check_socks():
                log("SOCKS5 代理不可用，重启 sing-box", "WARN")
                try:
                    proxy_mgr.restart()
                except Exception as e:
                    log(f"重启 sing-box 失败: {e}", "ERROR")
                    failure_reason = f"sing-box 重启失败: {str(e)[:100]}"
                    break

            page = None
            try:
                co = ChromiumOptions()
                co.set_browser_path('/usr/bin/google-chrome')
                co.set_argument('--no-sandbox')
                co.set_argument('--disable-dev-shm-usage')
                co.set_argument('--disable-gpu')
                co.set_argument('--disable-setuid-sandbox')
                co.set_argument('--disable-software-rasterizer')
                co.set_argument('--disable-extensions')
                co.set_argument('--no-first-run')
                co.set_argument('--no-default-browser-check')
                co.set_argument('--disable-popup-blocking')
                co.set_argument('--window-size=1280,720')
                co.set_argument('--log-level=3')
                co.set_argument('--silent')
                co.set_argument(f'--proxy-server=socks5://127.0.0.1:{SINGBOX_PORT}')
                user_data_dir = tempfile.mkdtemp()
                co.set_user_data_path(user_data_dir)
                co.auto_port()
                co.headless(False)
                page = ChromiumPage(co)

                page.add_init_js("""
                    const getParameter = WebGLRenderingContext.prototype.getParameter;
                    WebGLRenderingContext.prototype.getParameter = function(parameter) {
                        if (parameter === 37445) return 'Intel Inc.';
                        if (parameter === 37446) return 'Intel(R) UHD Graphics 630';
                        return getParameter.apply(this, [parameter]);
                    };
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
                """)

                log(f"访问: {url}")
                page.get(url, retry=3)
                time.sleep(random.uniform(5, 8))

                server_name = get_server_name(page)
                old_expire = get_expire_time(page)
                log(f"服务器: {server_name}, 到期: {old_expire}")

                page.run_js("""
                    const cssSelectors = ['ins.adsbygoogle', 'iframe[src*="ads"]', '.modal-backdrop'];
                    cssSelectors.forEach(sel => { document.querySelectorAll(sel).forEach(el => el.remove()); });
                """)
                time.sleep(2)
                consent_btn = page.ele('tag:button@@text():Consent', timeout=2)
                if consent_btn:
                    consent_btn.click()
                    time.sleep(3)

                for _ in range(3):
                    scroll_y = random.randint(200, 600)
                    page.scroll.down(scroll_y)
                    time.sleep(random.uniform(0.5, 1.5))
                    page.actions.move(random.randint(100, 800), random.randint(100, 500))
                    time.sleep(random.uniform(0.5, 1.0))
                time.sleep(random.uniform(1.0, 2.0))

                log("打开续期弹窗...")
                renew_btn1 = page.ele('xpath://button[contains(text(), "Renew server")]', timeout=3)
                if renew_btn1:
                    try:
                        renew_btn1.click()
                    except:
                        renew_btn1.click(by_js=True)
                else:
                    page.run_js("document.querySelectorAll('button').forEach(b => {if(b.textContent.includes('Renew server')) b.click();});")
                time.sleep(3)

                for _ in range(8):
                    if page.ele('text:Expires in:', timeout=0.5) or page.ele('text:Deletes on:', timeout=0.5):
                        break
                    time.sleep(1)

                renew_btn2 = page.ele('xpath://button[contains(text(), "Renew server")]', timeout=2)
                if renew_btn2:
                    try:
                        renew_btn2.click()
                    except:
                        renew_btn2.click(by_js=True)
                time.sleep(random.uniform(7, 10))

                anchor_frame = find_recaptcha_frame(page, "anchor")
                if not anchor_frame:
                    log("未检测到 reCAPTCHA，检查是否已直接成功")
                    new_expire = get_expire_time(page)
                    if new_expire != old_expire and new_expire != "未知":
                        success = True
                    else:
                        failure_reason = "未找到 reCAPTCHA 验证码区域"
                    break

                log("启动 reCAPTCHA 音频破解...")
                try:
                    solved = solve_recaptcha(page)
                except CaptchaBlocked:
                    log("IP 被封锁，切换代理/重注册后重试", "WARN")
                    failure_reason = "IP 被 reCAPTCHA 封锁"
                    try:
                        page.quit()
                    except:
                        pass
                    page = None
                    if attempt < MAX_RENEW_RETRIES_PER_URL:
                        try:
                            proxy_mgr.switch_proxy_and_restart()
                        except Exception:
                            proxy_mgr.restart()
                        continue
                    break
                except Exception as e:
                    log(f"reCAPTCHA 异常: {e}", "ERROR")
                    failure_reason = f"reCAPTCHA 异常: {e}"
                    break

                if not solved:
                    failure_reason = "未通过 reCAPTCHA 验证"
                    break

                log("点击最终 Renew 按钮")
                final_btn = page.ele('xpath://button[normalize-space(text())="Renew"]', timeout=3)
                if final_btn:
                    try:
                        final_btn.click()
                    except:
                        final_btn.click(by_js=True)
                    time.sleep(10)
                    new_expire = get_expire_time(page)
                    if new_expire != old_expire and new_expire != "未知":
                        log(f"到期时间已更新: {old_expire} -> {new_expire}")
                        success = True
                    else:
                        page_text = (page.html or "").lower()
                        if any(w in page_text for w in ["successfully", "renewed"]):
                            success = True
                        else:
                            failure_reason = "续期后未检测到成功标志"
                else:
                    failure_reason = "找不到最终 Renew 按钮"
                break

            except Exception as e:
                log(f"续期尝试异常: {e}", "ERROR")
                failure_reason = f"运行异常: {str(e)[:200]}"
                if attempt < MAX_RENEW_RETRIES_PER_URL:
                    if page:
                        try:
                            page.quit()
                        except:
                            pass
                        page = None
                    try:
                        proxy_mgr.restart()
                    except Exception:
                        pass
                    continue
                break
            finally:
                if page:
                    screen_name = f"host2play-{server_name}-{'success' if success else 'fail'}.png"
                    extra_info = f"状态: {'成功' if success else '失败'}"
                    if failure_reason:
                        extra_info += f" | 原因: {failure_reason}"
                    screenshot_path = capture_page_screenshot(page, os.path.join(screenshot_dir, screen_name), extra_info)
                    try:
                        page.quit()
                    except:
                        pass
    finally:
        vdisplay.stop()

    return success, server_name, old_expire, new_expire, screenshot_path, failure_reason

def main():
    tg_token = os.getenv("TG_BOT_TOKEN")
    tg_chat_id = os.getenv("TG_CHAT_ID")
    sub_url = os.getenv("SUB_URL")

    if not RENEW_URLS:
        log("请在 RENEW_URLS 列表中添加续期链接", "ERROR")
        sys.exit(1)

    proxy_mgr = ProxyManager(sub_url)
    try:
        log("初始化 WARP + sing-box 代理环境...")
        proxy_mgr.init()
    except Exception as e:
        log(f"代理初始化失败: {e}", "ERROR")
        proxy_mgr.stop()
        sys.exit(1)

    total_success = 0
    try:
        for idx, url in enumerate(RENEW_URLS, 1):
            log(f"{'#'*60}")
            log(f"处理第 {idx} 个链接: {url}")
            log(f"{'#'*60}")

            success, server_name, old_expire, new_expire, screenshot, failure_reason = renew_single_url(url, proxy_mgr)

            if success:
                caption = build_notification(True, url, server_name, old_expire, new_expire)
                total_success += 1
            else:
                caption = build_notification(False, url, server_name, old_expire, failure_reason=failure_reason)

            send_tg_photo(tg_token, tg_chat_id, screenshot, caption, parse_mode='HTML')

        log(f"全部完成，成功 {total_success}/{len(RENEW_URLS)} 个链接")
    finally:
        proxy_mgr.stop()

    if total_success < len(RENEW_URLS):
        sys.exit(1)

if __name__ == "__main__":
    main()
