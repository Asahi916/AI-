#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Launcher  ―  基板AI診断システムの自動起動ランチャー

「Gemini APIキー」と「ネームサーバーのアドレス」を入れて [開始] を押すだけで、次を自動で行う。
  1. ネームサーバー(omniNames)の起動
       ・アドレスが localhost のときだけ、つながらなければ起動する
       ・別のPCのアドレスのときは起動せず、そのネームサーバーを使う
  2. 同じフォルダの Camera_RTC.py / AIDiagnosis_RTC.py / Display_RTC.py の起動
       ・ネームサーバーのアドレスは、起動時のオプション -o corba.nameservers:ホスト:ポート で渡す
       ・APIキーは AIDiagnosis_RTC にだけ、環境変数 GEMINI_API_KEY として渡す（コマンドラインには載せない）
  3. 3つのRTCがネームサーバーに現れるのを待つ
  4. ポートの接続（Camera → AIDiagnosis → Display）
  5. 3つのRTCのアクティブ化
[停止] を押す、またはこのウィンドウを閉じると、RTC・（自分で起動した）ネームサーバーをすべて終了する。
rtshell は使わず、OpenRTM-aist に付いている omniORB(CORBA) の機能で接続・アクティブ化する。

【準備】 pip install pillow opencv-python google-genai  （3つのRTCが必要とするもの）
【起動】 python Launcher.py        ※ 4つのファイルを同じフォルダに置く
【自動で作られるもの】（このファイルと同じ場所）
  launcher_settings.json   前回の入力（ネームサーバーのアドレスなど。APIキーは含まない）
  logs/launcher.log        起動・停止の記録と、各RTCのコンソール出力
"""
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "launcher_settings.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")
LAUNCHER_LOG = os.path.join(LOG_DIR, "launcher.log")
KEY_FILE = os.path.join(BASE_DIR, "gemini_api_key.txt")

DEFAULT_OMNINAMES = (r"C:\Program Files\OpenRTM-aist\2.1.0\omniORB\4.3.4_vc16\bin\x86_win32\omniNames.exe")
DEFAULT_NS = "localhost:2809"

# 起動するRTC: (表示名, ファイル名, ネームサーバー上の type_name)
COMPONENTS = [("Camera_RTC", "Camera_RTC.py", "Camera_RTC"),
              ("AIDiagnosis_RTC", "AIDiagnosis_RTC.py", "AIDiagnosis_RTC"),
              ("Display_RTC", "Display_RTC.py", "Display_RTC")]

# つなぐポート: (出力側RTC, 出力ポート, 入力側RTC, 入力ポート)
CONNECTIONS = [("Camera_RTC", "request_out", "AIDiagnosis_RTC", "request_in"),
               ("AIDiagnosis_RTC", "result_out", "Display_RTC", "result_in")]
EXPECTED_PORTS = {"Camera_RTC": {"request_out"}, "AIDiagnosis_RTC": {"request_in", "result_out"},
                  "Display_RTC": {"result_in"}}
ACTIVATE_ORDER = ["Display_RTC", "AIDiagnosis_RTC", "Camera_RTC"]   # 受け取る側から先にアクティブ化する

NS_START_TIMEOUT = 20      # ネームサーバーの起動待ち(秒)
REGISTER_TIMEOUT = 60      # 3つのRTCが現れるまでの待ち(秒)。cv2やtkinterの読み込みで時間がかかることがある
ACTIVATE_TIMEOUT = 20


class LaunchError(Exception):
    pass


# ---------------------------------------------------------------- 設定・アドレス
def parse_address(text):
    """'host', 'host:port', 'corbaloc:iiop:host:port' などを (host, port) にする。空なら localhost:2809。"""
    t = (text or "").strip()
    if t.lower().startswith("corbaloc"):
        t = t.split(":", 2)[-1].split("/")[0].lstrip(":")
    t = t.replace("http://", "").replace("//", "").strip().rstrip("/")
    if not t:
        return "localhost", 2809
    host, port = t, 2809
    if t.count(":") == 1:
        h, p = t.split(":")
        host = h.strip() or "localhost"
        try:
            port = int(p)
        except ValueError:
            raise LaunchError("ネームサーバーのポート番号が数字ではありません: %s" % p)
    if not (1 <= port <= 65535):
        raise LaunchError("ネームサーバーのポート番号が範囲外です: %d" % port)
    return host, port


def is_local_host(host):
    h = host.strip().lower()
    names = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
    try:
        names.add(socket.gethostname().lower())
    except OSError:
        pass
    return h in names


def port_open(host, port, timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_omninames(saved=""):
    """omniNames.exe の場所を探す。見つからなければ ""。"""
    import glob
    cands = [saved, os.environ.get("OMNINAMES_PATH", ""), DEFAULT_OMNINAMES]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    for root in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        hits = sorted(glob.glob(os.path.join(root, "OpenRTM-aist", "*", "omniORB", "*", "bin", "*", "omniNames.exe")))
        if hits:
            return hits[-1]
    return shutil.which("omniNames") or ""


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(d):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


# ---------------------------------------------------------------- ネーミングサービス・RTCの操作（CORBA）
class NamingClient:
    """ネームサーバーに問い合わせ、RTCのポート接続・アクティブ化をする。"""

    def __init__(self, host, port):
        import OpenRTM_aist  # noqa: F401  （omniORB・RTCのIDLを読み込める状態にする）
        from omniORB import CORBA
        import CosNaming
        import RTC
        import SDOPackage
        self.CORBA, self.CosNaming, self.RTC, self.SDOPackage = CORBA, CosNaming, RTC, SDOPackage
        # 応答しない相手（古い登録など）で固まらないよう、呼び出しの待ち時間を5秒にする
        self.orb = CORBA.ORB_init(["launcher", "-ORBclientCallTimeOutPeriod", "5000"], CORBA.ORB_ID)
        obj = self.orb.string_to_object("corbaloc:iiop:%s:%d/NameService" % (host, port))
        self.root = obj._narrow(CosNaming.NamingContext)
        if self.root is None:
            raise LaunchError("ネームサーバー(%s:%d)に接続できません。" % (host, port))

    def ping(self):
        try:
            self.root.list(1)
            return True
        except Exception:
            return False

    def _walk(self, ctx, depth=0):
        if depth > 6:
            return
        bl, bi = ctx.list(500)
        items = list(bl)
        if bi is not None:
            try:
                while True:
                    ok, b = bi.next_one()
                    if not ok:
                        break
                    items.append(b)
            finally:
                try:
                    bi.destroy()
                except Exception:
                    pass
        for b in items:
            name = list(b.binding_name)
            if b.binding_type == self.CosNaming.ncontext:
                try:
                    sub = ctx.resolve(name)._narrow(self.CosNaming.NamingContext)
                except Exception:
                    continue
                if sub is not None:
                    yield from self._walk(sub, depth + 1)
            else:
                yield ctx, name

    def find(self, type_names):
        """{type_name: RTObject} を返す。生きているものだけ。同じ名前が複数あれば最後のもの。"""
        found = {}
        for ctx, name in self._walk(self.root):
            if name[-1].kind != "rtc":
                continue
            try:
                rtc = ctx.resolve(name)._narrow(self.RTC.RTObject)
                if rtc is None or rtc._non_existent():
                    continue
                prof = rtc.get_component_profile()
            except Exception:
                continue
            for t in type_names:
                if prof.type_name == t:
                    found[t] = rtc
        return found

    def port_map(self, rtc):
        out = {}
        for p in rtc.get_ports():
            out[p.get_port_profile().name.split(".")[-1]] = p
        return out

    def connect(self, conn_name, p_out, p_in):
        CORBA, SDO, RTC = self.CORBA, self.SDOPackage, self.RTC
        props = [SDO.NameValue(k, CORBA.Any(CORBA.TC_string, v)) for k, v in (
            ("dataport.interface_type", "corba_cdr"),
            ("dataport.dataflow_type", "push"),
            ("dataport.subscription_type", "new"))]
        cp = RTC.ConnectorProfile(conn_name, "", [p_out, p_in], props)
        ret, _ = p_out.connect(cp)
        if ret != RTC.RTC_OK:
            raise LaunchError("ポートの接続に失敗しました（%s: %s）" % (conn_name, ret))

    def _ec(self, rtc):
        ecs = rtc.get_owned_contexts()
        return ecs[0] if ecs else None

    def is_active(self, rtc):
        ec = self._ec(rtc)
        return ec is not None and ec.get_component_state(rtc) == self.RTC.ACTIVE_STATE

    def activate(self, rtc, timeout=ACTIVATE_TIMEOUT):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ec = self._ec(rtc)
                if ec is not None:
                    if ec.get_component_state(rtc) == self.RTC.ACTIVE_STATE:
                        return
                    ec.activate_component(rtc)   # 起動直後は失敗することがあるので、成功するまで繰り返す
            except Exception:
                pass
            time.sleep(0.5)
        raise LaunchError("アクティブ化できませんでした（時間切れ）。")

    def deactivate(self, rtc):
        try:
            ec = self._ec(rtc)
            if ec is not None:
                ec.deactivate_component(rtc)
        except Exception:
            pass

    def exit(self, rtc):
        try:
            rtc.exit()
        except Exception:
            pass


# ---------------------------------------------------------------- 起動・停止の本体（画面なし）
class SystemRunner:
    def __init__(self, cfg, log, state, naming_factory=NamingClient, popen=subprocess.Popen, sleep=time.sleep):
        """cfg: host, port, api_key, omninames, script_dir, python
        log(text) / state(名前, 文字列) は別スレッドから呼ばれる。"""
        self.cfg, self.log, self.state = cfg, log, state
        self.naming_factory, self.popen, self.sleep = naming_factory, popen, sleep
        self.procs = {}          # 名前 -> Popen
        self.tails = {}          # 名前 -> 直近の出力
        self.ns_proc = None
        self.ns_dir = None
        self.ns = None
        self.rtcs = {}
        self.stopping = False

    # -- 補助
    def _spawn(self, name, args, env=None, cwd=None):
        kw = {}
        if sys.platform.startswith("win"):
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = self.popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, **kw)
        self.procs[name] = proc
        self.tails[name] = []
        threading.Thread(target=self._pump, args=(name, proc), daemon=True).start()
        return proc

    def _pump(self, name, proc):
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self.tails[name] = (self.tails[name] + [line])[-15:]
                    self.log("[%s] %s" % (name, line))
        except Exception:
            pass

    def _tail(self, name):
        return "\n".join(self.tails.get(name, [])[-8:])

    def _check_alive(self):
        for name, p in self.procs.items():
            code = p.poll()
            if code is not None and not self.stopping:
                raise LaunchError("%s が途中で終了しました（終了コード %s）。\n%s" % (name, code, self._tail(name)))

    def child_status(self):
        """{名前: 'running' / 'exited(コード)'}"""
        out = {}
        for name, p in self.procs.items():
            code = p.poll()
            out[name] = "running" if code is None else "exited(%s)" % code
        return out

    # -- 開始
    def start(self):
        host, port = self.cfg["host"], self.cfg["port"]
        script_dir = self.cfg["script_dir"]
        missing = [f for _, f, _ in COMPONENTS if not os.path.isfile(os.path.join(script_dir, f))]
        if missing:
            raise LaunchError("同じフォルダに次のファイルがありません: " + ", ".join(missing))

        # 1. ネームサーバー
        self.state("ネームサーバー", "確認中…")
        if port_open(host, port):
            self.log("ネームサーバー %s:%d はすでに動いています。それを使います。" % (host, port))
            self.state("ネームサーバー", "既存のものを使用")
        elif not is_local_host(host):
            raise LaunchError("ネームサーバー %s:%d につながりません。別のPCのネームサーバーは自動では起動できません。\n"
                              "相手のPCでネームサーバーが動いているか、ファイアウォールを確認してください。" % (host, port))
        else:
            self._start_nameserver(port)
        self.ns = self.naming_factory(host, port)
        if not self.ns.ping():
            raise LaunchError("ネームサーバーに接続できません。")

        # 2. RTCの起動
        ns_opt = "corba.nameservers:%s:%d" % (host, port)
        for name, fname, _ in COMPONENTS:
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            if name == "AIDiagnosis_RTC" and self.cfg.get("api_key"):
                env["GEMINI_API_KEY"] = self.cfg["api_key"]   # このRTCにだけ渡す
            self.state(name, "起動中…")
            self._spawn(name, [self.cfg["python"], "-u", os.path.join(script_dir, fname), "-o", ns_opt],
                        env=env, cwd=script_dir)
            self.log("%s を起動しました。" % fname)

        # 3. 3つのRTCが現れるのを待つ
        types = [t for _, _, t in COMPONENTS]
        deadline = time.time() + REGISTER_TIMEOUT
        while True:
            self._check_alive()
            found = self.ns.find(types)
            ok = False
            if all(t in found for t in types):
                try:
                    ports = {t: self.ns.port_map(found[t]) for t in types}
                    ok = all(EXPECTED_PORTS[t] <= set(ports[t]) for t in types)
                except Exception:
                    ok = False
            if ok:
                self.rtcs = found
                self.ports = ports
                break
            if time.time() > deadline:
                lack = [t for t in types if t not in found]
                raise LaunchError("RTCがネームサーバーに現れませんでした（%s）。\n"
                                  "ネームサーバーのアドレス、ファイアウォール、各RTCのエラーを確認してください。\n%s"
                                  % (", ".join(lack) or "ポートが揃いません", "\n".join(self._tail(t) for t in types)))
            self.sleep(0.5)
        for t in types:
            self.state(t, "登録を確認")
        self.log("3つのRTCがネームサーバーに登録されました。")

        # 4. ポートの接続
        for a, pa, b, pb in CONNECTIONS:
            self.ns.connect("%s_%s_to_%s_%s" % (a, pa, b, pb), self.ports[a][pa], self.ports[b][pb])
            self.log("接続しました: %s.%s → %s.%s" % (a, pa, b, pb))

        # 5. アクティブ化（受け取る側から）
        for t in ACTIVATE_ORDER:
            self.ns.activate(self.rtcs[t])
            self.state(t, "稼働中（アクティブ）")
            self.log("%s をアクティブ化しました。" % t)
        self.log("準備ができました。Camera_RTC の画面で写真を入れて「診断へ送る」を押してください。")

    def _start_nameserver(self, port):
        exe = self.cfg.get("omninames", "")
        if not exe or not os.path.isfile(exe):
            raise LaunchError("omniNames.exe が見つかりません。「詳細設定」で場所を指定してください。\n現在の指定: %s" % (exe or "（空）"))
        self.state("ネームサーバー", "起動中…")
        self.ns_dir = tempfile.mkdtemp(prefix="omninames_")   # 毎回新しい保存先にして、古い登録が残らないようにする
        args = [exe, "-start", str(port), "-logdir", self.ns_dir, "-errlog", os.path.join(self.ns_dir, "error.log")]
        self.ns_proc = self._spawn("omniNames", args, cwd=os.path.dirname(exe))
        self.log("ネームサーバー(omniNames)を起動しました（ポート %d）。" % port)
        deadline = time.time() + NS_START_TIMEOUT
        while time.time() < deadline:
            if self.ns_proc.poll() is not None:
                err = ""
                try:
                    with open(os.path.join(self.ns_dir, "error.log"), encoding="utf-8", errors="replace") as f:
                        err = f.read()[-600:]
                except OSError:
                    pass
                raise LaunchError("omniNames がすぐに終了しました。\n%s\n%s" % (self._tail("omniNames"), err))
            if port_open("localhost", port):
                self.state("ネームサーバー", "起動済み")
                return
            self.sleep(0.3)
        raise LaunchError("ネームサーバーが %d 秒以内に起動しませんでした。" % NS_START_TIMEOUT)

    # -- 停止
    def stop(self):
        self.stopping = True
        # RTCを止める（アクティブ解除 → 終了の指示）
        if self.ns is not None:
            for t in reversed(ACTIVATE_ORDER):
                rtc = self.rtcs.get(t)
                if rtc is not None:
                    self.ns.deactivate(rtc)
            for t in ACTIVATE_ORDER:
                rtc = self.rtcs.get(t)
                if rtc is not None:
                    self.ns.exit(rtc)
        self.sleep(0.5)
        # プロセスを終了する（RTC → ネームサーバーの順）
        order = [n for n in self.procs if n != "omniNames"] + (["omniNames"] if "omniNames" in self.procs else [])
        for name in order:
            p = self.procs[name]
            if p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass
        for name in order:
            p = self.procs[name]
            try:
                p.wait(timeout=4)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
            self.log("%s を終了しました。" % name)
        if self.ns_dir:
            shutil.rmtree(self.ns_dir, ignore_errors=True)
        for name, _, _ in COMPONENTS:
            self.state(name, "停止")
        self.state("ネームサーバー", "停止" if self.ns_proc is not None else "（使用していません）")
        self.procs, self.rtcs, self.ns, self.ns_proc, self.ns_dir = {}, {}, None, None, None
        self.stopping = False


# ---------------------------------------------------------------- GUI
class LauncherApp:
    def __init__(self):
        import tkinter as tk
        from tkinter import messagebox, scrolledtext, ttk
        self.tk, self.ttk, self.mb = tk, ttk, messagebox
        self.q = queue.Queue()
        self.runner = None
        self.busy = False        # 開始・停止の処理中
        self.running = False     # システムが動いている
        self.closing = False
        os.makedirs(LOG_DIR, exist_ok=True)
        st = load_settings()

        root = self.root = tk.Tk()
        root.title("基板AI診断 ランチャー")
        root.geometry("760x640")
        frm = ttk.Frame(root, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(6, weight=1)

        ttk.Label(frm, text="ネームサーバー").grid(row=0, column=0, sticky="w")
        self.ns_var = tk.StringVar(value=st.get("ns", DEFAULT_NS))
        ttk.Entry(frm, textvariable=self.ns_var).grid(row=0, column=1, sticky="we", padx=6, pady=3)
        ttk.Label(frm, text="例: localhost:2809 / 192.168.0.10:2809", foreground="gray").grid(row=0, column=2, sticky="w")

        ttk.Label(frm, text="Gemini APIキー").grid(row=1, column=0, sticky="w")
        self.key_var = tk.StringVar(value="")
        ttk.Entry(frm, textvariable=self.key_var, show="*").grid(row=1, column=1, sticky="we", padx=6, pady=3)
        ttk.Label(frm, text="空なら 環境変数 / gemini_api_key.txt を使用", foreground="gray").grid(row=1, column=2, sticky="w")
        self.save_key_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="このPCにキーを保存する（gemini_api_key.txt に平文で保存されます）",
                        variable=self.save_key_var).grid(row=2, column=1, sticky="w", padx=6)

        adv = ttk.LabelFrame(frm, text="詳細設定", padding=6)
        adv.grid(row=3, column=0, columnspan=3, sticky="we", pady=(8, 0))
        adv.columnconfigure(1, weight=1)
        ttk.Label(adv, text="omniNames.exe").grid(row=0, column=0, sticky="w")
        self.nm_var = tk.StringVar(value=find_omninames(st.get("omninames", "")) or st.get("omninames", ""))
        ttk.Entry(adv, textvariable=self.nm_var).grid(row=0, column=1, sticky="we", padx=6)

        bar = ttk.Frame(frm)
        bar.grid(row=4, column=0, columnspan=3, sticky="we", pady=10)
        self.start_btn = ttk.Button(bar, text="開始", command=self.on_start)
        self.start_btn.pack(side="left", ipadx=24)
        self.stop_btn = ttk.Button(bar, text="停止", command=self.on_stop)
        self.stop_btn.pack(side="left", padx=8, ipadx=24)
        self.stop_btn.state(["disabled"])
        self.msg_var = tk.StringVar(value="ネームサーバーのアドレスとAPIキーを入れて [開始] を押してください。")
        ttk.Label(bar, textvariable=self.msg_var, wraplength=480).pack(side="left", padx=10)

        stf = ttk.LabelFrame(frm, text="状態", padding=6)
        stf.grid(row=5, column=0, columnspan=3, sticky="we")
        stf.columnconfigure(1, weight=1)
        self.state_vars = {}
        for i, name in enumerate(["ネームサーバー"] + [c[0] for c in COMPONENTS]):
            ttk.Label(stf, text=name, width=16).grid(row=i, column=0, sticky="w")
            v = tk.StringVar(value="停止")
            ttk.Label(stf, textvariable=v).grid(row=i, column=1, sticky="w")
            self.state_vars[name] = v

        self.logbox = scrolledtext.ScrolledText(frm, wrap="word", height=12, font=("", 10))
        self.logbox.grid(row=6, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
        self.logbox.configure(state="disabled")

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(200, self.poll)
        root.after(1000, self.watch_children)

    def run(self):
        self.root.mainloop()

    # -- ログ・状態（別スレッドからも呼べる）
    def log(self, text):
        line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), text)
        self.q.put(("log", line))
        try:
            with open(LAUNCHER_LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def set_state(self, name, text):
        self.q.put(("state", name, text))

    def poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "log":
                    self.logbox.configure(state="normal")
                    self.logbox.insert("end", item[1] + "\n")
                    self.logbox.see("end")
                    self.logbox.configure(state="disabled")
                elif item[0] == "state" and item[1] in self.state_vars:
                    self.state_vars[item[1]].set(item[2])
                elif item[0] == "started":
                    if self.closing:   # 起動中にウィンドウを閉じられたときは、起動が終わりしだい片付ける
                        threading.Thread(target=self._stop_worker, daemon=True).start()
                        continue
                    self.busy, self.running = False, True
                    self.msg_var.set("動作中です。Camera_RTC の画面で診断を始められます。")
                    self.stop_btn.state(["!disabled"])
                elif item[0] == "failed":
                    self.msg_var.set("失敗しました。下のログを確認してください。")
                    self.mb.showerror("起動に失敗しました", item[1])
                elif item[0] == "stopped":
                    self.busy, self.running = False, False
                    self.start_btn.state(["!disabled"])
                    self.stop_btn.state(["disabled"])
                    if not self.closing:
                        self.msg_var.set("停止しました。")
                    else:
                        self.root.destroy()
                        return
        except queue.Empty:
            pass
        self.root.after(200, self.poll)

    def watch_children(self):
        """RTCの画面を閉じるなどで全部終了していたら、後始末をする。"""
        if self.running and not self.busy and self.runner is not None:
            sts = self.runner.child_status()
            comps = [sts.get(c[0], "") for c in COMPONENTS]
            for (name, _, _), s in zip(COMPONENTS, comps):
                if s.startswith("exited"):
                    self.set_state(name, "終了")
            if comps and all(s.startswith("exited") for s in comps):
                self.log("3つのRTCがすべて終了したので、後始末をします。")
                self.on_stop()
        self.root.after(1000, self.watch_children)

    # -- 操作
    def on_start(self):
        if self.busy or self.running:
            return
        try:
            host, port = parse_address(self.ns_var.get())
        except LaunchError as e:
            self.mb.showerror("入力を確認してください", str(e))
            return
        key = self.key_var.get().strip()
        if self.save_key_var.get() and key:
            try:
                with open(KEY_FILE, "w", encoding="utf-8") as f:
                    f.write(key)
                self.log("APIキーを gemini_api_key.txt に保存しました。")
            except OSError as e:
                self.log("APIキーを保存できませんでした: %s" % e)
        if not key and not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or os.path.isfile(KEY_FILE)):
            if not self.mb.askyesno("APIキーが空です",
                                    "APIキーが入力されておらず、環境変数やファイルにも見つかりません。\n"
                                    "このまま起動すると、診断のときにエラーになります。続けますか？"):
                return
        save_settings({"ns": self.ns_var.get().strip(), "omninames": self.nm_var.get().strip()})
        cfg = {"host": host, "port": port, "api_key": key, "omninames": self.nm_var.get().strip(),
               "script_dir": BASE_DIR, "python": sys.executable}
        self.runner = SystemRunner(cfg, self.log, self.set_state)
        self.busy = True
        self.start_btn.state(["disabled"])
        self.msg_var.set("起動中です。1分ほどかかることがあります…")
        self.log("=== 開始 ===")
        threading.Thread(target=self._start_worker, daemon=True).start()

    def _start_worker(self):
        try:
            self.runner.start()
            self.q.put(("started",))
        except LaunchError as e:
            self.log("失敗: %s" % str(e).splitlines()[0])
            self.q.put(("failed", str(e)))
            self._stop_worker()          # 途中までの分を片付ける
        except Exception as e:
            self.log("予期しないエラー: %r" % e)
            self.q.put(("failed", "予期しないエラー: %r" % e))
            self._stop_worker()

    def on_stop(self):
        if self.runner is None or (self.busy and not self.running):
            return
        self.busy = True
        self.stop_btn.state(["disabled"])
        self.msg_var.set("停止中です…")
        self.log("=== 停止 ===")
        threading.Thread(target=self._stop_worker, daemon=True).start()

    def _stop_worker(self):
        try:
            self.runner.stop()
        except Exception as e:
            self.log("停止中のエラー: %r" % e)
        self.q.put(("stopped",))

    def on_close(self):
        if self.closing:
            return
        self.closing = True
        if self.runner is not None and (self.running or self.busy or self.runner.procs):
            self.msg_var.set("終了しています…")
            self.start_btn.state(["disabled"])
            self.stop_btn.state(["disabled"])
            if not (self.busy and not self.running):   # 起動処理中は、その失敗処理の中で片付く
                self.busy = True
                self.log("=== 終了 ===")
                threading.Thread(target=self._stop_worker, daemon=True).start()
        else:
            self.root.destroy()


def main():
    LauncherApp().run()


if __name__ == "__main__":
    main()
