import os
import sys
import time
import random
import html
import requests
import tempfile
import subprocess
import json
import signal
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qs, unquote
from xvfbwrapper import Xvfb
from DrissionPage import ChromiumPage, ChromiumOptions

try:
    import speech_recognition as sr
    from pydub import AudioSegment
except ImportError:
    pass

# ==============================================================================
# 配置
# ==============================================================================
RENEW_URLS = [
    "https://host2play.gratis/server/renew?i=e1a5dcbf-b580-49a4-a4a9-355e8285641b",
    "https://host2play.gratis/server/renew?i=e3890c8f-8618-44cd-87ed-de4560c4ae91"
]

MAX_CAPTCHA = 3
MAX_RENEW_RETRIES_PER_URL = 10
SING_BOX_PORT = 1080
CONFIG_PATH = "/tmp/sing-box-config.json"
SING_BOX_BIN = None  # 运行时自动检测

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
# 获取当前 IP
# ==============================================================================
def get_current_ip(proxy=None):
    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        return requests.get("https://api.ipify.org", timeout=10, proxies=proxies).text
    except Exception:
        return "未知"

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

# ==============================================================================
# 构建通知
# ==============================================================================
def build_notification(success, url, server_name, old_expire, new_expire=None, failure_reason="", node_info=""):
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
    if node_info:
        lines.append(f"节点: {node_info}")
    lines.append("")
    lines.append("Host2Play Auto Renew (sing-box)")
    return "\n".join(lines)

# ==============================================================================
# 截图（含 IP 和状态覆盖层）
# ==============================================================================
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

        page.run_js(f'''
            const old = document.getElementById('info-overlay');
            if (old) old.remove();
            document.body.insertAdjacentHTML('beforeend', `{overlay_html}`);
        ''')

        time.sleep(0.5)
        page.get_screenshot(path=file_name)

        page.run_js('const el = document.getElementById("info-overlay"); if(el) el.remove();')

        log(f"截图已保存: {file_name} (IP: {current_ip})")
        return file_name
    except Exception as e:
        log(f"截图失败: {e}", "WARN")
        return None

# ==============================================================================
# 节点 URI 解析
# ==============================================================================
def parse_single_uri(uri):
    """解析单个节点 URI，返回 sing-box outbound JSON"""
    uri = uri.strip()
    if uri.startswith("vless://"):
        return parse_vless(uri)
    elif uri.startswith("vmess://"):
        return parse_vmess(uri)
    elif uri.startswith("ss://"):
        return parse_ss(uri)
    elif uri.startswith("trojan://"):
        return parse_trojan(uri)
    elif uri.startswith("socks5://") or uri.startswith("http://"):
        return parse_socks5(uri)
    else:
        log(f"不支持的 URI 格式: {uri[:50]}...", "WARN")
        return None

def parse_vless(uri):
    """解析 vless:// URI（完整参数支持）"""
    try:
        rest = uri[8:]
        if '#' in rest:
            rest, name = rest.rsplit('#', 1)
            name = unquote(name)
        else:
            name = "unknown"

        if '?' in rest:
            addr, params_str = rest.split('?', 1)
        else:
            addr, params_str = rest, ""

        if '@' not in addr:
            return None
        uuid, host_port = addr.split('@', 1)

        if ':' in host_port:
            server, port = host_port.rsplit(':', 1)
            port = int(port)
        else:
            server, port = host_port, 443

        params = parse_qs(params_str)

        def p(key, default=None):
            vals = params.get(key)
            return vals[0] if vals else default

        outbound = {
            "type": "vless",
            "tag": name,
            "server": server,
            "server_port": port,
            "uuid": uuid,
        }

        # flow (xtls-rprx-vision / xtls-rprx-vision-udp443)
        flow = p("flow", "").lower()
        if flow in ("xtls-rprx-vision", "xtls-rprx-vision-udp443"):
            outbound["flow"] = flow

        # packet encoding
        pe = p("packetEncoding", "")
        if pe:
            outbound["packet_encoding"] = pe

        # TLS
        security = p("security", "")
        tls_enabled = security in ("tls", "reality", "")

        if security == "reality":
            reality_pbk = p("pbk", "")
            reality_sid = p("sid", "")
            reality_spx = p("spx", "")
            sni = p("sni", server)
            fingerprint = p("fp", "chrome")

            tls_config = {
                "enabled": True,
                "server_name": sni,
                "utls": {"enabled": True, "fingerprint": fingerprint},
                "reality": {
                    "enabled": True,
                    "public_key": reality_pbk,
                }
            }
            if reality_sid:
                tls_config["reality"]["short_id"] = reality_sid
            if reality_spx:
                tls_config["reality"]["server_spider_x"] = reality_spx

            outbound["tls"] = tls_config

        elif tls_enabled:
            sni = p("sni", server)
            fingerprint = p("fp", "chrome")
            insecure = p("insecure", "0") in ("1", "true", "yes")
            allow_insecure = p("allowInsecure", "0") in ("1", "true", "yes")
            alpn_raw = p("alpn", "")

            tls_config = {
                "enabled": True,
                "server_name": sni,
            }

            if insecure or allow_insecure:
                tls_config["insecure"] = True

            if alpn_raw:
                tls_config["alpn"] = [a.strip() for a in alpn_raw.split(",")]

            if fingerprint != "none":
                tls_config["utls"] = {
                    "enabled": True,
                    "fingerprint": fingerprint
                }

            outbound["tls"] = tls_config

        # transport
        transport_type = p("type", "tcp")

        if transport_type == "ws":
            ws_path = p("path", "/")
            ws_host = p("host", server)
            transport = {
                "type": "ws",
                "path": ws_path,
                "headers": {"Host": ws_host}
            }
            ed = p("ed", "")
            if ed:
                try:
                    transport["max_early_data"] = int(ed)
                except ValueError:
                    pass
            outbound["transport"] = transport

        elif transport_type == "grpc":
            grpc_service = p("serviceName", "")
            transport = {"type": "grpc"}
            if grpc_service:
                transport["service_name"] = grpc_service
            outbound["transport"] = transport

        elif transport_type == "tcp":
            header_type = p("headerType", "none")
            if header_type == "http":
                transport = {
                    "type": "tcp",
                    "header": {
                        "type": "http",
                        "request": {},
                        "response": {}
                    }
                }
                outbound["transport"] = transport

        return outbound
    except Exception as e:
        log(f"解析 vless URI 失败: {e}", "WARN")
        return None

def parse_vmess(uri):
    """解析 vmess:// URI（完整参数支持）"""
    try:
        encoded = uri[8:]
        padding = 4 - len(encoded) % 4
        if padding != 4:
            encoded += '=' * padding
        import base64
        decoded = base64.b64decode(encoded).decode('utf-8')
        data = json.loads(decoded)

        server = data.get("add", "")
        port = int(data.get("port", 443))
        uuid = data.get("id", "")
        name = data.get("ps", "vmess-node")
        alter_id = data.get("aid", 0)
        net = data.get("net", "ws")
        tls = data.get("tls", "")
        scy = data.get("scy", "auto")
        sni = data.get("sni", server)
        host = data.get("host", server)
        path = data.get("path", "/")
        alpn_raw = data.get("alpn", "")
        fp = data.get("fp", "chrome")
        insecure = data.get("allowInsecure", "0") in ("1", "true", "yes")
        header_type = data.get("headerType", "none")

        outbound = {
            "type": "vmess",
            "tag": name,
            "server": server,
            "server_port": port,
            "uuid": uuid,
            "alter_id": alter_id,
            "security": scy if scy in ("auto", "aes-128-gcm", "chacha20-poly1305", "none", "zero") else "auto",
        }

        if tls in ("tls", "1", "true"):
            tls_config = {
                "enabled": True,
                "server_name": sni,
            }

            if insecure:
                tls_config["insecure"] = True

            if alpn_raw:
                tls_config["alpn"] = [a.strip() for a in alpn_raw.split(",")]

            fp_lower = fp.lower()
            if fp_lower != "none":
                tls_config["utls"] = {"enabled": True, "fingerprint": fp_lower}

            outbound["tls"] = tls_config

        if net == "ws":
            transport = {
                "type": "ws",
                "path": path,
                "headers": {"Host": host}
            }
            ed = data.get("maxEarlyData", "")
            if ed:
                try:
                    transport["max_early_data"] = int(ed)
                except ValueError:
                    pass
            outbound["transport"] = transport

        elif net == "grpc":
            grpc_service = data.get("serviceName", "")
            transport = {"type": "grpc"}
            if grpc_service:
                transport["service_name"] = grpc_service
            outbound["transport"] = transport

        elif net == "tcp":
            if header_type == "http":
                transport = {
                    "type": "tcp",
                    "header": {
                        "type": "http",
                        "request": {},
                        "response": {}
                    }
                }
                outbound["transport"] = transport

        return outbound
    except Exception as e:
        log(f"解析 vmess URI 失败: {e}", "WARN")
        return None

def parse_ss(uri):
    """解析 ss:// URI"""
    try:
        rest = uri[5:]
        if '#' in rest:
            rest, name = rest.rsplit('#', 1)
            name = unquote(name)
        else:
            name = "ss-node"

        if '@' in rest:
            encoded, server_port = rest.split('@', 1)
            if ':' in server_port:
                server, port = server_port.rsplit(':', 1)
                port = int(port)
            else:
                server, port = server_port, 443

            padding = 4 - len(encoded) % 4
            if padding != 4:
                encoded += '=' * padding
            import base64
            decoded = base64.b64decode(encoded).decode('utf-8')

            if ':' in decoded:
                method, password = decoded.split(':', 1)
            else:
                method, password = "aes-256-gcm", decoded

            return {
                "type": "shadowsocks",
                "tag": name,
                "server": server,
                "server_port": port,
                "method": method,
                "password": password
            }
        else:
            padding = 4 - len(rest) % 4
            if padding != 4:
                rest += '=' * padding
            import base64
            decoded = base64.b64decode(rest).decode('utf-8')
            if '@' in decoded:
                method_password, server_port = decoded.rsplit('@', 1)
                method, password = method_password.split(':', 1)
                if ':' in server_port:
                    server, port = server_port.rsplit(':', 1)
                    port = int(port)
                else:
                    server, port = server_port, 443
                return {
                    "type": "shadowsocks",
                    "tag": name,
                    "server": server,
                    "server_port": port,
                    "method": method,
                    "password": password
                }
        return None
    except Exception as e:
        log(f"解析 ss URI 失败: {e}", "WARN")
        return None

def parse_trojan(uri):
    """解析 trojan:// URI"""
    try:
        rest = uri[9:]
        if '#' in rest:
            rest, name = rest.rsplit('#', 1)
            name = unquote(name)
        else:
            name = "trojan-node"

        if '?' in rest:
            addr, params_str = rest.split('?', 1)
        else:
            addr, params_str = rest, ""

        if '@' not in addr:
            return None
        password, host_port = addr.split('@', 1)

        if ':' in host_port:
            server, port = host_port.rsplit(':', 1)
            port = int(port)
        else:
            server, port = host_port, 443

        params = parse_qs(params_str)

        def p(key, default=None):
            vals = params.get(key)
            return vals[0] if vals else default

        outbound = {
            "type": "trojan",
            "tag": name,
            "server": server,
            "server_port": port,
            "password": password
        }

        # flow
        flow = p("flow", "").lower()
        if flow in ("xtls-rprx-vision", "xtls-rprx-vision-udp443"):
            outbound["flow"] = flow

        # TLS
        sni = p("sni", server)
        insecure = p("insecure", "0") in ("1", "true", "yes")
        allow_insecure = p("allowInsecure", "0") in ("1", "true", "yes")
        alpn_raw = p("alpn", "")
        fingerprint = p("fp", "chrome")

        tls_config = {
            "enabled": True,
            "server_name": sni,
        }

        if insecure or allow_insecure:
            tls_config["insecure"] = True

        if alpn_raw:
            tls_config["alpn"] = [a.strip() for a in alpn_raw.split(",")]

        if fingerprint != "none":
            tls_config["utls"] = {"enabled": True, "fingerprint": fingerprint}

        outbound["tls"] = tls_config

        # transport
        transport_type = p("type", "tcp")

        if transport_type == "ws":
            ws_path = p("path", "/")
            ws_host = p("host", server)
            transport = {
                "type": "ws",
                "path": ws_path,
                "headers": {"Host": ws_host}
            }
            ed = p("ed", "")
            if ed:
                try:
                    transport["max_early_data"] = int(ed)
                except ValueError:
                    pass
            outbound["transport"] = transport

        elif transport_type == "grpc":
            grpc_service = p("serviceName", "")
            transport = {"type": "grpc"}
            if grpc_service:
                transport["service_name"] = grpc_service
            outbound["transport"] = transport

        elif transport_type == "tcp":
            header_type = p("headerType", "none")
            if header_type == "http":
                transport = {
                    "type": "tcp",
                    "header": {
                        "type": "http",
                        "request": {},
                        "response": {}
                    }
                }
                outbound["transport"] = transport

        return outbound
    except Exception as e:
        log(f"解析 trojan URI 失败: {e}", "WARN")
        return None

def parse_socks5(uri):
    """解析 socks5:// URI"""
    try:
        rest = uri.split("://", 1)[1]
        if '@' in rest:
            auth, server_port = rest.split('@', 1)
            user, password = auth.split(':', 1) if ':' in auth else (auth, "")
        else:
            server_port = rest
            user, password = "", ""

        if ':' in server_port:
            server, port = server_port.rsplit(':', 1)
            port = int(port)
        else:
            server, port = server_port, 1080

        return {
            "type": "socks",
            "tag": "socks5-node",
            "server": server,
            "server_port": port,
            "username": user,
            "password": password
        }
    except Exception as e:
        log(f"解析 socks5 URI 失败: {e}", "WARN")
        return None

# ==============================================================================
# 订阅解析
# ==============================================================================
def parse_subscription(url):
    """下载并解析订阅，返回节点 outbound 列表"""
    nodes = []
    try:
        log(f"下载订阅: {url[:50]}...")
        r = requests.get(url, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/plain, */*"
        })
        r.raise_for_status()
        content = r.text.strip()

        log(f"订阅内容长度: {len(content)}, 前100字符: {content[:100]}")

        lines = content.splitlines()

        # 检查是否包含 URI 节点，没有则尝试 base64 解码
        has_uri = False
        for l in lines:
            for prefix in ["vless://", "vmess://", "ss://", "trojan://"]:
                if l.startswith(prefix):
                    has_uri = True
                    break
            if has_uri:
                break

        if not has_uri and len(lines) <= 5:
            try:
                import base64
                raw = content.replace('\n', '').replace('\r', '').strip()
                padding = 4 - len(raw) % 4
                if padding != 4:
                    raw += '=' * padding
                decoded = base64.b64decode(raw).decode('utf-8', errors='replace')
                lines = decoded.splitlines()
                log(f"base64 解码后 {len(lines)} 行: {decoded[:100]}...")
            except Exception as e:
                log(f"base64 解码失败: {e}", "WARN")

        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue

            if line.startswith("vless://") or line.startswith("vmess://") or \
               line.startswith("ss://") or line.startswith("trojan://"):
                outbound = parse_single_uri(line)
                if outbound:
                    nodes.append(outbound)

        log(f"订阅解析完成，共 {len(nodes)} 个节点")
    except Exception as e:
        log(f"下载订阅失败: {e}", "ERROR")
    return nodes

# ==============================================================================
# 节点池管理
# ==============================================================================
class NodePool:
    def __init__(self, primary_uri=None, sub_url=None):
        self.primary_nodes = []  # 改为列表，支持多节点
        self.backup_nodes = []
        self.current_outbound = None
        self.primary_index = 0
        self.used_indices = set()

        if primary_uri:
            for line in primary_uri.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                outbound = parse_single_uri(line)
                if outbound:
                    self.primary_nodes.append(outbound)

            if self.primary_nodes:
                log(f"主节点解析成功，共 {len(self.primary_nodes)} 个")
            else:
                log("主节点全部解析失败", "WARN")

        if sub_url:
            self.backup_nodes = parse_subscription(sub_url)
            log(f"备用节点池: {len(self.backup_nodes)} 个")

    def get_next_node(self, force_backup=False):
        """获取下一个节点（优先主节点池，再切备用池）"""
        if not force_backup and self.primary_nodes:
            node = self.primary_nodes[self.primary_index % len(self.primary_nodes)]
            self.primary_index += 1
            self.current_outbound = node
            log(f"使用主节点 #{self.primary_index}/{len(self.primary_nodes)}: {node.get('tag', 'unknown')}")
            return self.current_outbound

        if self.backup_nodes:
            available = [i for i in range(len(self.backup_nodes)) if i not in self.used_indices]
            if not available:
                self.used_indices.clear()
                available = list(range(len(self.backup_nodes)))

            idx = random.choice(available)
            self.used_indices.add(idx)
            self.current_outbound = self.backup_nodes[idx]
            log(f"使用备用节点 #{idx}: {self.current_outbound.get('tag', 'unknown')}")
            return self.current_outbound

        log("无可用节点", "ERROR")
        return None

    def has_backup(self):
        return len(self.backup_nodes) > 0

    def has_primary(self):
        return len(self.primary_nodes) > 0

# ==============================================================================
# sing-box 管理
# ==============================================================================
class SingBoxManager:
    def __init__(self):
        self.process = None

    def start(self, outbound):
        """用指定 outbound 启动 sing-box"""
        self.stop()

        config = {
            "log": {"level": "error"},
            "inbounds": [{
                "type": "socks",
                "listen": "127.0.0.1",
                "listen_port": SING_BOX_PORT
            }],
            "outbounds": [outbound]
        }

        log(f"sing-box 配置: server={outbound.get('server','?')}:{outbound.get('server_port','?')}  type={outbound.get('type','?')}")

        with open(CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=2)

        try:
            self.process = subprocess.Popen(
                [SING_BOX_BIN, "run", "-c", CONFIG_PATH],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid
            )
            time.sleep(3)

            if self.process.poll() is not None:
                stderr = self.process.stderr.read().decode()
                log(f"sing-box 启动失败: {stderr}", "ERROR")
                return False

            log(f"sing-box 启动成功 (PID: {self.process.pid})")
            return True
        except Exception as e:
            log(f"sing-box 启动异常: {e}", "ERROR")
            return False

    def stop(self):
        """停止 sing-box"""
        if self.process:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except Exception:
                try:
                    self.process.terminate()
                except Exception:
                    pass
            try:
                self.process.wait(timeout=5)
            except Exception:
                try:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                except Exception:
                    try:
                        self.process.kill()
                    except Exception:
                        pass
            self.process = None
            log("sing-box 已停止")

    def check(self, target_url=None):
        """检查 sing-box 代理是否可用（检测目标网站而非 api.ipify.org）"""
        if self.process and self.process.poll() is not None:
            stderr = self.process.stderr.read().decode()
            log(f"sing-box 进程已退出: {stderr[:200]}", "WARN")
            return False

        proxy = f"socks5://127.0.0.1:{SING_BOX_PORT}"
        check_url = target_url or "https://host2play.gratis"

        # 方式1: curl 通过代理访问目标网站（主要检测方式）
        try:
            result = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 "--max-time", "15",
                 "--socks5-hostname", f"127.0.0.1:{SING_BOX_PORT}",
                 check_url],
                capture_output=True, text=True, timeout=20
            )
            http_code = result.stdout.strip()
            if http_code and http_code not in ("000", ""):
                log(f"代理检测通过，目标响应: HTTP {http_code}")
                return True
        except Exception as e:
            log(f"curl 代理检测异常: {e}", "WARN")

        # 方式2: 直接检查进程是否存活
        if self.process:
            try:
                self.process.wait(timeout=0.1)
                stderr = self.process.stderr.read().decode()
                log(f"sing-box 已退出: {stderr[:200]}", "ERROR")
                return False
            except subprocess.TimeoutExpired:
                pass

        log("代理检测失败", "WARN")
        return False

# ==============================================================================
# WARP 重连 → 改为 sing-box 重连
# ==============================================================================
def restart_proxy(sing_box, node_pool, force_backup=False, target_url=None):
    """停止当前节点，切换到下一个"""
    sing_box.stop()

    outbound = node_pool.get_next_node(force_backup=force_backup)
    if not outbound:
        log("无可用节点，无法重启", "ERROR")
        return False

    if not sing_box.start(outbound):
        return False

    if sing_box.check(target_url=target_url):
        return True

    log("新节点不可用，尝试下一个", "WARN")
    return restart_proxy(sing_box, node_pool, force_backup=True, target_url=target_url)

# ==============================================================================
# reCAPTCHA 辅助函数
# ==============================================================================
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

            return {
                headerText: hText,
                isTryAgain: isTryAgain,
                errorVisible: isVisible,
                blocked: isTryAgain || isVisible
            };
        """)

        if result.get('blocked'):
            log(f"[BLOCKED] header: '{result.get('headerText')}', errorVisible: {result.get('errorVisible')}", "WARN")

        return result.get('blocked', False)
    except Exception:
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
            bframe.run_js("""
                const btn = document.querySelector('#recaptcha-audio-button');
                if (btn) btn.click();
            """)
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
# 单个 URL 续期流程
# ==============================================================================
def renew_single_url(url, sing_box, node_pool, use_proxy=True):
    success = False
    server_name = "未知"
    old_expire = "未知"
    new_expire = "未知"
    screenshot_path = None
    failure_reason = ""
    node_info = "WARP 直连" if not use_proxy else ""
    screenshot_dir = "output/screenshots"
    os.makedirs(screenshot_dir, exist_ok=True)

    vdisplay = Xvfb(width=1280, height=720, colordepth=24)
    vdisplay.start()

    try:
        for attempt in range(1, MAX_RENEW_RETRIES_PER_URL + 1):
            log(f"{'='*20} 续期尝试 {attempt}/{MAX_RENEW_RETRIES_PER_URL} {'='*20}")
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
                if use_proxy:
                    co.set_argument(f'--proxy-server=socks5://127.0.0.1:{SING_BOX_PORT}')
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
                node_info = node_pool.current_outbound.get('tag', '未知') if node_pool and node_pool.current_outbound else 'WARP 直连'
                log(f"服务器: {server_name}, 到期时间: {old_expire}, 节点: {node_info}")

                # 清理遮挡广告
                page.run_js("""
                    const cssSelectors = ['ins.adsbygoogle', 'iframe[src*="ads"]', '.modal-backdrop'];
                    cssSelectors.forEach(sel => {
                        document.querySelectorAll(sel).forEach(el => el.remove());
                    });
                """)
                time.sleep(2)
                consent_btn = page.ele('tag:button@@text():Consent', timeout=2)
                if consent_btn:
                    consent_btn.click()
                    time.sleep(3)

                # 模拟真实鼠标轨迹和滚动
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
                    except Exception:
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
                    except Exception:
                        renew_btn2.click(by_js=True)
                time.sleep(random.uniform(7, 10))

                # reCAPTCHA 破解
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
                    log("IP 被封锁，换节点后重试", "WARN")
                    failure_reason = "IP 被 reCAPTCHA 封锁"
                    try:
                        page.quit()
                    except Exception:
                        pass
                    page = None
                    if attempt < MAX_RENEW_RETRIES_PER_URL:
                        if use_proxy and sing_box:
                            force_backup = (attempt > 3)
                            restart_proxy(sing_box, node_pool, force_backup=force_backup, target_url=url)
                        continue
                    break

                if not solved:
                    failure_reason = "未通过 reCAPTCHA 验证"
                    break

                log("点击最终 Renew 按钮")
                final_btn = page.ele('xpath://button[normalize-space(text())="Renew"]', timeout=3)
                if final_btn:
                    try:
                        final_btn.click()
                    except Exception:
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
                        except Exception:
                            pass
                        page = None
                    if use_proxy and sing_box:
                        force_backup = (attempt > 3)
                        restart_proxy(sing_box, node_pool, force_backup=force_backup, target_url=url)
                    continue
                break
            finally:
                if page:
                    screen_name = f"host2play-{server_name}-{'success' if success else 'fail'}.png"
                    extra_info = f"状态: {'成功' if success else '失败'} | 节点: {node_info}"
                    if failure_reason:
                        extra_info += f" | 原因: {failure_reason}"
                    screenshot_path = capture_page_screenshot(
                        page,
                        os.path.join(screenshot_dir, screen_name),
                        extra_info
                    )
                    try:
                        page.quit()
                    except Exception:
                        pass
    finally:
        vdisplay.stop()

    return success, server_name, old_expire, new_expire, screenshot_path, failure_reason, node_info

# ==============================================================================
# 主入口
# ==============================================================================
def main():
    global SING_BOX_BIN

    tg_token = os.getenv("TG_BOT_TOKEN")
    tg_chat_id = os.getenv("TG_CHAT_ID")
    primary_uri = os.getenv("host2")
    sub_url = os.getenv("SUB_URL")

    if not RENEW_URLS:
        log("请在 RENEW_URLS 列表中添加续期链接", "ERROR")
        sys.exit(1)

    if not primary_uri and not sub_url:
        log("未配置 host2 或 SUB_URL 节点，将使用 WARP 直连模式", "WARN")

    # 尝试检测 sing-box（没有则跳过代理模式）
    for path in ["/usr/bin/sing-box", "/usr/local/bin/sing-box", "/opt/sing-box/sing-box"]:
        if os.path.isfile(path):
            SING_BOX_BIN = path
            break
    if not SING_BOX_BIN:
        try:
            result = subprocess.run(["which", "sing-box"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                SING_BOX_BIN = result.stdout.strip()
        except Exception:
            pass

    proxy_available = False
    sing_box = SingBoxManager()
    node_pool = None

    if SING_BOX_BIN:
        log(f"sing-box 路径: {SING_BOX_BIN}")
        node_pool = NodePool(primary_uri=primary_uri, sub_url=sub_url)

        outbound = node_pool.get_next_node()
        if outbound and sing_box.start(outbound) and sing_box.check(target_url=RENEW_URLS[0]):
            proxy_available = True
            log("代理模式就绪")
        else:
            log("代理模式不可用，尝试备用节点...", "WARN")
            if outbound and restart_proxy(sing_box, node_pool, force_backup=True, target_url=RENEW_URLS[0]):
                proxy_available = True
                log("代理模式就绪（备用节点）")
            else:
                log("所有代理节点不可用，后续将使用 WARP 直连", "WARN")
    else:
        log("未找到 sing-box，将使用 WARP 直连模式", "WARN")

    total_success = 0
    for idx, url in enumerate(RENEW_URLS, 1):
        log(f"{'#'*60}")
        log(f"处理第 {idx} 个链接: {url}")
        log(f"{'#'*60}")

        if proxy_available:
            success, server_name, old_expire, new_expire, screenshot, failure_reason, node_info = renew_single_url(
                url, sing_box, node_pool, use_proxy=True
            )
        else:
            success = False
            failure_reason = "代理不可用"

        # 代理模式失败，切 WARP 直连兜底
        if not success:
            log("切换到 WARP 直连模式", "WARN")
            if sing_box:
                sing_box.stop()
            success, server_name, old_expire, new_expire, screenshot, failure_reason, node_info = renew_single_url(
                url, None, None, use_proxy=False
            )

        if success:
            caption = build_notification(True, url, server_name, old_expire, new_expire, node_info=node_info)
            total_success += 1
        else:
            caption = build_notification(False, url, server_name, old_expire, failure_reason=failure_reason, node_info=node_info)

        send_tg_photo(tg_token, tg_chat_id, screenshot, caption, parse_mode='HTML')

    if sing_box:
        sing_box.stop()
    log(f"全部完成，成功 {total_success}/{len(RENEW_URLS)} 个链接")
    if total_success < len(RENEW_URLS):
        sys.exit(1)

if __name__ == "__main__":
    main()
