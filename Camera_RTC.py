#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Camera_RTC  ―  基板AI診断の入力RTC（OpenRTM-aist Python版）

【役割】
  Webカメラで基板を撮影する（または既存の画像ファイルを選ぶ）→ 写真は captures/ に保存
  → 「診断へ送る」ボタンで、画像のパス(文字列)と手書きの説明を JSON にして OutPort から送る。
  画像そのものではなく「パス文字列」だけを送る（3つのRTCは同じPC上で動かす前提）。

  OutPort "request_out" (RTC.TimedString) : AIDiagnosis_RTC の "request_in" へつなぐ

【送るJSON】（送らない項目は空文字 ""）
  {"boards": ["C:/.../a.jpg", ...], "reference": "", "circuit": "", "parts": "",
   "symptom": "", "circuit_note": "", "parts_note": ""}
  ※ TimedString の中身は CORBA の string 型で、日本語を直接は通せない。
    そのため JSON は ensure_ascii=True（日本語を \\uXXXX に変換）で作る。受け取り側の json.loads で元に戻る。

【画面でできること】
  ・4種類の画像（基板の写真[必須・複数可] / 正常な基板 / 回路図 / 使用部品）を、
    「選ぶ」（既存ファイル）か「撮影」（Webカメラ）で入れる。送る/送らないは、入れるか空にするかで決まる。
  ・カメラのプレビュー（初期状態はオフ。チェックでオン/オフ）
  ・手書きの説明（基板・回路図・使用部品）
  ・正常な基板・回路図・使用部品の画像と、回路図・部品の説明は、次回起動時にも自動で残る（last_inputs.json）

【コンフィグレーションパラメータ】（RT System Editor で変更できる）
  camera_index   : Webカメラの番号（既定 0）
  capture_width  : 撮影の横幅(px)（既定 1280）
  capture_height : 撮影の縦幅(px)（既定 720）

【準備】
  pip install opencv-python pillow

【起動】
  コマンドプロンプトで   python Camera_RTC.py
  RT System Editor で request_out を AIDiagnosis_RTC の request_in につなぎ、アクティブ化する。
  ※ RTCが非アクティブのあいだは送信が溜まり、アクティブ化した時点でまとめて送られる。
  画面（ウィンドウ）を閉じると終了する。

【自動で作られるもの】（このファイルと同じ場所）
  captures/          撮影した写真
  last_inputs.json   前回の入力
"""
import base64
import io
import json
import os
import queue
import sys
from datetime import datetime

try:
    from PIL import Image, ImageOps
except ImportError:  # Pillowが無いとプレビュー・撮影は使えない
    Image = ImageOps = None

try:
    import cv2  # Webカメラ用
except ImportError:
    cv2 = None

try:
    import RTC
    import OpenRTM_aist
except ImportError as _e:
    print("OpenRTM_aist / RTC を読み込めません: %s" % _e)
    print("OpenRTM-aist (Python版) と omniORBpy のインストールを確認してください。")
    sys.exit(1)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CAPTURE_DIR = os.path.join(BASE_DIR, "captures")
STATE_FILE = os.path.join(BASE_DIR, "last_inputs.json")

THUMB = (200, 120)      # 画像欄のサムネイル
PREVIEW = (320, 180)    # カメラのプレビュー

# スペック(仕様)の文字列は CORBA に渡るので、必ず ASCII(英数字)だけにする
camera_spec = [
    "implementation_id", "Camera_RTC",
    "type_name", "Camera_RTC",
    "description", "Board photo input and request sender",
    "version", "1.0.0",
    "vendor", "board_ai",
    "category", "Camera",
    "activity_type", "DataFlowComponent",
    "max_instance", "1",
    "language", "Python",
    "lang_type", "SCRIPT",
    "exec_cxt.periodic.type", "PeriodicExecutionContext",
    "exec_cxt.periodic.rate", "20.0",
    "conf.default.camera_index", "0",
    "conf.default.capture_width", "1280",
    "conf.default.capture_height", "720",
    "conf.__widget__.camera_index", "text",
    "conf.__widget__.capture_width", "text",
    "conf.__widget__.capture_height", "text",
    "conf.__type__.camera_index", "int",
    "conf.__type__.capture_width", "int",
    "conf.__type__.capture_height", "int",
    ""]


# ---------------------------------------------------------------- RTC間のデータ（JSON文字列）
def pack(obj):
    """辞書 → JSON文字列。日本語は \\uXXXX にして、CORBAのstring型でも安全な ASCII だけにする。"""
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"))


# ---------------------------------------------------------------- 前回の入力の保存
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
    except OSError:
        pass  # 保存できなくても診断はできる


def open_pil(path):
    """向き情報を反映したRGB画像を返す（Pillow必須）。"""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img) or img
    return img.convert("RGB")


# ---------------------------------------------------------------- RTCとGUIの受け渡し
class Bridge:
    """RTC(別スレッド) と 画面(メインスレッド) の間でデータを受け渡す箱。"""

    def __init__(self):
        self.outbox = queue.Queue()    # 送信するJSON文字列 (画面 → onExecute)
        self.events = queue.Queue()    # 画面に出す一言 (onExecute → 画面)
        self.active = False            # RTCがアクティブか
        # コンフィグレーションの入れ物（値は [0] に入る。RTC側がここへバインドする）
        self.camera_index = [0]
        self.capture_width = [1280]
        self.capture_height = [720]

    def camera_conf(self):
        def num(box, default):
            try:
                return int(box[0])
            except (TypeError, ValueError, IndexError):
                return default
        return num(self.camera_index, 0), num(self.capture_width, 1280), num(self.capture_height, 720)


BRIDGE = Bridge()


# ---------------------------------------------------------------- RTC本体
class CameraRTC(OpenRTM_aist.DataFlowComponentBase):
    def __init__(self, manager):
        OpenRTM_aist.DataFlowComponentBase.__init__(self, manager)

    def onInitialize(self):
        self._d_out = RTC.TimedString(RTC.Time(0, 0), "")
        self._outOut = OpenRTM_aist.OutPort("request_out", self._d_out)
        self.addOutPort("request_out", self._outOut)

        self.bindParameter("camera_index", BRIDGE.camera_index, "0")
        self.bindParameter("capture_width", BRIDGE.capture_width, "1280")
        self.bindParameter("capture_height", BRIDGE.capture_height, "720")
        return RTC.RTC_OK

    def onActivated(self, ec_id):
        BRIDGE.active = True
        return RTC.RTC_OK

    def onDeactivated(self, ec_id):
        BRIDGE.active = False
        return RTC.RTC_OK

    def onExecute(self, ec_id):
        # 画面で「診断へ送る」が押されたぶんを、ここ(RTCのスレッド)で送信する
        while True:
            try:
                text = BRIDGE.outbox.get_nowait()
            except queue.Empty:
                break
            self._d_out.data = text
            try:
                OpenRTM_aist.setTimestamp(self._d_out)
            except Exception:
                pass
            try:
                ok = self._outOut.write()
                if ok:
                    BRIDGE.events.put("診断の依頼を送信しました（%s）" % datetime.now().strftime("%H:%M:%S"))
                else:
                    BRIDGE.events.put("送信できなかった可能性があります。AIDiagnosis_RTC とつながっているか確認してください。")
            except Exception as e:
                BRIDGE.events.put("送信でエラー: %r" % e)
        return RTC.RTC_OK


def MyModuleInit(manager):
    profile = OpenRTM_aist.Properties(defaults_str=camera_spec)
    manager.registerFactory(profile, CameraRTC, OpenRTM_aist.Delete)
    comp = manager.createComponent("Camera_RTC")
    if comp is None:
        print("Camera_RTC を作れませんでした")


# ---------------------------------------------------------------- GUI
class CameraApp:
    def __init__(self, bridge):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
        self.tk, self.ttk, self.fd, self.mb = tk, ttk, filedialog, messagebox
        self.bridge = bridge
        self.slots = {}
        self.cap = None            # cv2.VideoCapture（プレビュー中・撮影中だけ開く）
        self.prev_tk = None

        root = self.root = tk.Tk()
        root.title("基板AI診断 ― 入力 (Camera_RTC)")
        root.geometry("960x900")
        frm = ttk.Frame(root, padding=10)
        frm.pack(fill="both", expand=True)
        for c in range(4):
            frm.columnconfigure(c, weight=1, uniform="col")

        # RTCの状態
        self.rtc_var = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.rtc_var, foreground="#444").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))

        # 写真・画像の入力（4つ）
        self.make_slot(frm, 0, "board", "診断する基板の写真（必須）", multi=True)
        self.make_slot(frm, 1, "reference", "正常な基板の写真（任意）")
        self.make_slot(frm, 2, "circuit", "回路図（任意）")
        self.make_slot(frm, 3, "parts", "使用部品の写真（任意）")

        # カメラのプレビュー（初期状態はオフ）
        cam = ttk.LabelFrame(frm, text="Webカメラ", padding=6)
        cam.grid(row=2, column=0, columnspan=4, sticky="we", padx=3, pady=(6, 0))
        self.preview_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(cam, text="プレビューを表示する（オフでも「撮影」は使えます）",
                        variable=self.preview_var, command=self.toggle_preview).pack(anchor="w")
        holder = tk.Frame(cam, width=PREVIEW[0] + 4, height=PREVIEW[1] + 4, bg="#e8e8e8")
        holder.pack_propagate(False)
        holder.pack(pady=(4, 0))
        self.preview_lbl = tk.Label(holder, text="（プレビューはオフ）", bg="#e8e8e8", fg="gray")
        self.preview_lbl.pack(expand=True)
        if cv2 is None or Image is None:
            ttk.Label(cam, foreground="#b00",
                      text="カメラを使うには  pip install opencv-python pillow  が必要です。").pack(anchor="w")

        # 手書きの入力（3つ）
        self.symptom_box = self.make_note(frm, 0, 2, "基板について（手書き・任意）\n例: LEDが光らない。電池を入れると熱くなる")
        self.circuit_box = self.make_note(frm, 2, 1, "回路図について（手書き・任意）\n例: ボタンを押すとLEDが光る回路")
        self.parts_box = self.make_note(frm, 3, 1, "使用部品（手書き・任意）\n例: R1=330Ω、LED赤×1")

        # 送信
        bar = ttk.Frame(frm)
        bar.grid(row=4, column=0, columnspan=4, sticky="we", pady=8)
        bar.columnconfigure(1, weight=1)
        ttk.Button(bar, text="診断へ送る", command=self.send).grid(row=0, column=0, ipadx=20)
        self.status_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.status_var, wraplength=700).grid(row=0, column=1, sticky="w", padx=8)

        # 前回の入力を戻す
        st = load_state()
        for key in ("reference", "circuit", "parts"):
            p = st.get(key)
            if p and os.path.exists(p):
                self.set_slot(key, [p], save=False)
        self.circuit_box.insert("1.0", st.get("circuit_note", ""))
        self.parts_box.insert("1.0", st.get("parts_note", ""))

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(200, self.poll)

    def run(self):
        self.root.mainloop()

    # -- 入力欄の部品
    def make_slot(self, parent, col, key, title, multi=False):
        tk, ttk = self.tk, self.ttk
        box = ttk.LabelFrame(parent, text=title, padding=4)
        box.grid(row=1, column=col, sticky="nsew", padx=3)
        holder = tk.Frame(box, width=THUMB[0] + 4, height=THUMB[1] + 4, bg="#e8e8e8", cursor="hand2")
        holder.pack_propagate(False)
        holder.pack()
        thumb = tk.Label(holder, text="（未選択）", bg="#e8e8e8", fg="gray")
        thumb.pack(expand=True)
        thumb.bind("<Button-1>", lambda e, k=key: self.enlarge(k))
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(4, 0))
        ttk.Button(row, text="選ぶ", width=6, command=lambda k=key, m=multi: self.pick(k, m)).pack(side="left")
        ttk.Button(row, text="撮影", width=6, command=lambda k=key: self.capture(k)).pack(side="left", padx=4)
        ttk.Button(row, text="×", width=3, command=lambda k=key: self.set_slot(k, [])).pack(side="left")
        name = ttk.Label(box, text="", foreground="gray", wraplength=THUMB[0])
        name.pack(anchor="w")
        self.slots[key] = {"paths": [], "thumb": thumb, "name": name, "img": None}

    def make_note(self, parent, col, span, title):
        ttk = self.ttk
        f = ttk.Frame(parent)
        f.grid(row=3, column=col, columnspan=span, sticky="nsew", padx=3, pady=(6, 0))
        ttk.Label(f, text=title, foreground="#444").pack(anchor="w")
        box = self.tk.Text(f, height=4, wrap="word")
        box.pack(fill="both", expand=True)
        return box

    def pick(self, key, multi):
        types = [("画像", "*.jpg *.jpeg *.png *.webp *.JPG *.JPEG *.PNG"), ("すべて", "*.*")]
        if multi:
            ps = self.fd.askopenfilenames(title="画像を選ぶ", filetypes=types)
        else:
            p = self.fd.askopenfilename(title="画像を選ぶ", filetypes=types)
            ps = [p] if p else []
        if ps:
            self.set_slot(key, [os.path.abspath(p) for p in ps])

    def set_slot(self, key, paths, save=True):
        s = self.slots[key]
        s["paths"] = list(paths)
        s["img"] = None
        if not paths:
            s["thumb"].configure(image="", text="（未選択）")
            s["name"].configure(text="")
        else:
            names = os.path.basename(paths[0]) + (" ほか%d枚" % (len(paths) - 1) if len(paths) > 1 else "")
            s["name"].configure(text=names)
            if Image is None:
                s["thumb"].configure(image="", text="（プレビューにはPillowが必要）")
            else:
                try:
                    img = open_pil(paths[-1] if key == "board" else paths[0])  # 基板は最後(=最新)の1枚を見せる
                    img.thumbnail(THUMB)
                    s["img"] = self.to_tk(img)
                    s["thumb"].configure(image=s["img"], text="")
                except Exception:
                    s["thumb"].configure(image="", text="（画像を開けません）")
        if save:
            self.save_inputs()

    def add_path(self, key, path):
        """撮影した写真を入れる。基板の写真は追加、それ以外は置き換え。"""
        if key == "board":
            self.set_slot(key, self.slots[key]["paths"] + [path])
        else:
            self.set_slot(key, [path])

    def enlarge(self, key):
        paths = self.slots[key]["paths"]
        if paths and Image is not None:
            try:
                p = paths[-1] if key == "board" else paths[0]
                self.show_image(open_pil(p), os.path.basename(p))
            except Exception:
                pass

    def save_inputs(self):
        def first(k):
            p = self.slots[k]["paths"]
            return p[0] if p else None
        save_state({"reference": first("reference"), "circuit": first("circuit"), "parts": first("parts"),
                    "circuit_note": self.circuit_box.get("1.0", "end").strip(),
                    "parts_note": self.parts_box.get("1.0", "end").strip()})

    def to_tk(self, pil):
        buf = io.BytesIO()
        pil.save(buf, format="PNG")
        return self.tk.PhotoImage(data=base64.b64encode(buf.getvalue()))

    def show_image(self, pil, title):
        win = self.tk.Toplevel(self.root)
        win.title(title)
        img = pil.copy()
        img.thumbnail((900, 640))
        tkimg = self.to_tk(img)
        lbl = self.ttk.Label(win, image=tkimg)
        lbl.image = tkimg
        lbl.pack(padx=8, pady=8)

    # -- カメラ
    def open_camera(self):
        if self.cap is not None and self.cap.isOpened():
            return self.cap
        if cv2 is None or Image is None:
            raise RuntimeError("カメラを使うには  pip install opencv-python pillow  が必要です。")
        idx, w, h = self.bridge.camera_conf()
        cap = None
        if sys.platform.startswith("win"):
            cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)  # Windowsでは開くのが速く安定する
        if cap is None or not cap.isOpened():
            if cap is not None:
                cap.release()
            cap = cv2.VideoCapture(idx)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError("カメラ(番号 %d)を開けませんでした。\n"
                               "・他のアプリがカメラを使っていないか\n"
                               "・RT System Editor のコンフィグレーション camera_index の番号\n"
                               "を確認してください。" % idx)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        self.cap = cap
        return cap

    def close_camera(self):
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

    @staticmethod
    def frame_to_pil(frame):
        return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

    def toggle_preview(self):
        if self.preview_var.get():
            try:
                self.open_camera()
            except Exception as e:
                self.preview_var.set(False)
                self.mb.showerror("カメラ", str(e))
                return
            self.preview_tick()
        else:
            self.close_camera()
            self.preview_lbl.configure(image="", text="（プレビューはオフ）")
            self.prev_tk = None

    def preview_tick(self):
        if not self.preview_var.get() or self.cap is None:
            return
        try:
            ok, frame = self.cap.read()
            if ok and frame is not None:
                pil = self.frame_to_pil(frame)
                pil.thumbnail(PREVIEW)
                self.prev_tk = self.to_tk(pil)
                self.preview_lbl.configure(image=self.prev_tk, text="")
        except Exception:
            pass
        self.root.after(70, self.preview_tick)

    def capture(self, key):
        """Webカメラで1枚撮影して、captures/ に保存し、その入力欄に入れる。"""
        keep_open = self.preview_var.get()
        path = None
        try:
            cap = self.open_camera()
            frame = None
            for _ in range(3 if keep_open else 10):  # 起動直後は暗いので、数枚読み捨てて最新の1枚を使う
                ok, f = cap.read()
                if ok and f is not None:
                    frame = f
            if frame is None:
                raise RuntimeError("カメラから映像を取得できませんでした。")
            os.makedirs(CAPTURE_DIR, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
            path = os.path.join(CAPTURE_DIR, "%s_%s.jpg" % (key, stamp))
            self.frame_to_pil(frame).save(path, quality=92)  # 日本語フォルダでも保存できるようPillowで保存
        except Exception as e:
            self.mb.showerror("撮影できませんでした", str(e))
            path = None
        finally:
            if not keep_open:
                self.close_camera()
        if path:
            self.add_path(key, path)
            self.status_var.set("撮影しました: %s" % os.path.basename(path))

    # -- 送信
    def send(self):
        boards = self.slots["board"]["paths"]
        if not boards:
            self.mb.showinfo("基板AI診断", "診断する基板の写真を「選ぶ」か「撮影」で入れてください。")
            return

        def first(k):
            p = self.slots[k]["paths"]
            return p[0] if p else ""

        req = {"boards": list(boards), "reference": first("reference"), "circuit": first("circuit"),
               "parts": first("parts"), "symptom": self.symptom_box.get("1.0", "end").strip(),
               "circuit_note": self.circuit_box.get("1.0", "end").strip(),
               "parts_note": self.parts_box.get("1.0", "end").strip()}
        paths = req["boards"] + [req[k] for k in ("reference", "circuit", "parts") if req[k]]
        missing = [p for p in paths if not os.path.isfile(p)]
        if missing:
            self.mb.showerror("基板AI診断", "画像ファイルが見つかりません:\n" + "\n".join(missing))
            return
        self.save_inputs()
        self.bridge.outbox.put(pack(req))
        if self.bridge.active:
            self.status_var.set("送信を依頼しました…")
        else:
            self.status_var.set("RTCが非アクティブです。RT System Editor でアクティブ化すると送信されます。")

    # -- 定期処理
    def poll(self):
        if self.bridge.active:
            self.rtc_var.set("RTC: アクティブ（送信できます）")
        else:
            self.rtc_var.set("RTC: 非アクティブ（RT System Editor でつないでアクティブ化してください）")
        try:
            while True:
                self.status_var.set(self.bridge.events.get_nowait())
        except queue.Empty:
            pass
        self.root.after(200, self.poll)

    def on_close(self):
        self.preview_var.set(False)
        self.close_camera()
        self.save_inputs()
        self.root.destroy()


def main():
    # 先にRTCを別スレッドで動かし、画面(tkinter)はメインスレッドで動かす
    mgr = OpenRTM_aist.Manager.init(sys.argv)
    mgr.setModuleInitProc(MyModuleInit)
    mgr.activateManager()
    mgr.runManager(True)   # True: 別スレッドで動かし、この関数は先へ進む
    try:
        CameraApp(BRIDGE).run()
    except Exception as e:
        print("画面の表示でエラーが起きました: %r" % e)
    finally:
        try:
            mgr.shutdown()
        except Exception:
            pass
        sys.stdout.flush()
        os._exit(0)  # 終了時にスレッドが残って固まるのを防ぐ


if __name__ == "__main__":
    main()
