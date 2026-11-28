#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Display_RTC  ―  基板AI診断の結果表示RTC（OpenRTM-aist Python版）

【役割】
  InPort "result_in" (RTC.TimedString) : AIDiagnosis_RTC から診断の「経過・結果・エラー」(JSON文字列) を受け取り、
  tkinter の画面に表示する。

【画面でできること】
  ・まとめ / 詳細の切り替え（「詳しく見る」ボタン）
  ・場所を写真で見る（AIが怪しいと考えた場所を赤い枠と番号で示した写真）
  ・診断の記録（当たり/外れ・実際の状態・実際の原因・撮影条件）を保存し、これまでの記録と一致率を見る

【受け取るJSON】（AIDiagnosis_RTC.py の説明を参照）
  {"type": "status"|"error"|"result", ...}
  ※ JSONではない普通の文字列が届いたときは、そのまま結果として表示する。

【準備】
  pip install pillow
  AIDiagnosis_RTC.py と同じ場所に置く（診断の記録 logs/ を共有するため）。

【起動】
  コマンドプロンプトで   python Display_RTC.py
  RT System Editor で AIDiagnosis_RTC の result_out とつなぎ、アクティブ化する。
  画面（ウィンドウ）を閉じると終了する。
"""
import base64
import csv
import io
import json
import os
import queue
import sys
from datetime import datetime

try:
    from PIL import Image, ImageOps
except ImportError:  # Pillowが無くても文字の表示は動く（写真の表示は使えない）
    Image = ImageOps = None

try:
    import RTC
    import OpenRTM_aist
except ImportError as _e:
    print("OpenRTM_aist / RTC を読み込めません: %s" % _e)
    print("OpenRTM-aist (Python版) と omniORBpy のインストールを確認してください。")
    sys.exit(1)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_CSV = os.path.join(LOG_DIR, "diagnosis_log.csv")

# スペック(仕様)の文字列は CORBA に渡るので、必ず ASCII(英数字)だけにする
display_spec = [
    "implementation_id", "Display_RTC",
    "type_name", "Display_RTC",
    "description", "Display of board diagnosis result",
    "version", "1.0.0",
    "vendor", "board_ai",
    "category", "Display",
    "activity_type", "DataFlowComponent",
    "max_instance", "1",
    "language", "Python",
    "lang_type", "SCRIPT",
    "exec_cxt.periodic.type", "PeriodicExecutionContext",
    "exec_cxt.periodic.rate", "20.0",
    ""]


class LogError(Exception):
    pass


# ---------------------------------------------------------------- RTC間のデータ（JSON文字列）
def unpack(text):
    """JSON文字列 → 辞書。読めなければ None。"""
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else None
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------- 診断の記録（AIDiagnosis_RTC.py と同じ形式）
LOG_FIELDS = ["ID", "日時", "写真", "症状", "回路図", "部品の情報", "モデル", "AIの判定",
              "実際の状態", "実際の原因", "撮影条件", "判定の自動照合", "原因の当たり外れ",
              "まとめ", "詳細", "場所を示した写真"]


def auto_match(actual, verdict):
    """「実際の状態」とAIの判定が合っているか。実際の状態が不明なら空欄。"""
    if actual not in ("正常", "異常あり"):
        return ""
    if verdict == "異常なし":
        return "○" if actual == "正常" else "×"
    if verdict == "異常の疑いあり":
        return "○" if actual == "異常あり" else "×"
    return "判断不能"


def read_log():
    if not os.path.exists(LOG_CSV):
        return []
    with open(LOG_CSV, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def update_log(log_id, fields):
    """記録済みの行（ID指定）に、評価の項目を書き足す。"""
    try:
        rows = read_log()
        found = False
        for r in rows:
            if r.get("ID") == log_id:
                r.update(fields)
                r["判定の自動照合"] = auto_match(r.get("実際の状態", ""), r.get("AIの判定", ""))
                found = True
        if not found:
            raise LogError("記録の中にこの診断(ID %s)が見つかりません。" % log_id)
        with open(LOG_CSV, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            w.writeheader()
            w.writerows(rows)
    except PermissionError:
        raise LogError("記録ファイルに書き込めません。Excelで開いている場合は閉じてください。")


def log_stats(rows):
    """記録から一致率を数える。"""
    judged = [r for r in rows if r.get("判定の自動照合") in ("○", "×")]
    caused = [r for r in rows if r.get("原因の当たり外れ") in ("当たり", "外れ")]
    return {
        "total": len(rows),
        "judge_n": len(judged), "judge_ok": sum(r["判定の自動照合"] == "○" for r in judged),
        "cause_n": len(caused), "cause_ok": sum(r["原因の当たり外れ"] == "当たり" for r in caused),
    }


# ---------------------------------------------------------------- RTCとGUIの受け渡し
class Bridge:
    """RTC(別スレッド) と 画面(メインスレッド) の間でデータを受け渡す箱。"""

    def __init__(self):
        self.inbox = queue.Queue()   # 受信した文字列 (RTC → 画面)
        self.active = False          # RTCがアクティブか


BRIDGE = Bridge()


# ---------------------------------------------------------------- RTC本体
class DisplayRTC(OpenRTM_aist.DataFlowComponentBase):
    def __init__(self, manager):
        OpenRTM_aist.DataFlowComponentBase.__init__(self, manager)

    def onInitialize(self):
        self._d_in = RTC.TimedString(RTC.Time(0, 0), "")
        self._inIn = OpenRTM_aist.InPort("result_in", self._d_in)
        self.addInPort("result_in", self._inIn)
        return RTC.RTC_OK

    def onActivated(self, ec_id):
        BRIDGE.active = True
        return RTC.RTC_OK

    def onDeactivated(self, ec_id):
        BRIDGE.active = False
        return RTC.RTC_OK

    def onExecute(self, ec_id):
        n = 0
        while n < 50 and self._inIn.isNew():
            n += 1
            data = self._inIn.read()
            BRIDGE.inbox.put(getattr(data, "data", ""))
        return RTC.RTC_OK


def MyModuleInit(manager):
    profile = OpenRTM_aist.Properties(defaults_str=display_spec)
    manager.registerFactory(profile, DisplayRTC, OpenRTM_aist.Delete)
    comp = manager.createComponent("Display_RTC")
    if comp is None:
        print("Display_RTC を作れませんでした")


# ---------------------------------------------------------------- GUI
class DisplayApp:
    def __init__(self, bridge):
        import tkinter as tk
        from tkinter import messagebox, scrolledtext, ttk
        self.tk, self.ttk, self.mb, self.st = tk, ttk, messagebox, scrolledtext
        self.bridge = bridge
        self.answer = {"summary": "", "detail": "", "model": "", "showing_detail": False,
                       "marked_path": "", "log_id": None}

        root = self.root = tk.Tk()
        root.title("基板AI診断 ― 結果表示 (Display_RTC)")
        root.geometry("900x820")
        frm = ttk.Frame(root, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(1, weight=1)

        # 操作の行
        bar = ttk.Frame(frm)
        bar.grid(row=0, column=0, sticky="we", pady=(0, 8))
        bar.columnconfigure(1, weight=1)
        self.rtc_var = tk.StringVar(value="RTC: 非アクティブ（RT System Editor でつないでアクティブ化）")
        ttk.Label(bar, textvariable=self.rtc_var, foreground="#444").grid(row=0, column=0, columnspan=4, sticky="w")
        self.status_var = tk.StringVar(value="診断結果を待っています")
        ttk.Label(bar, textvariable=self.status_var).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.marks_btn = ttk.Button(bar, text="場所を写真で見る", command=self.show_marks)
        self.marks_btn.grid(row=1, column=2, padx=4)
        self.detail_btn = ttk.Button(bar, text="詳しく見る", command=self.toggle_detail)
        self.detail_btn.grid(row=1, column=3)
        self.marks_btn.state(["disabled"])
        self.detail_btn.state(["disabled"])

        # 結果
        self.result = scrolledtext.ScrolledText(frm, wrap="word", font=("", 13), height=10)
        self.result.grid(row=1, column=0, sticky="nsew")
        self.set_result("ここにAIの診断結果が表示されます。\n\nCamera_RTC で「診断へ送る」を押すと、AIDiagnosis_RTC を通って届きます。")

        # 記録（診断のあとで入れる）
        rec = ttk.LabelFrame(frm, text="この診断を記録する（次回以降の改善に使います・任意）", padding=6)
        rec.grid(row=2, column=0, sticky="we", pady=(8, 0))
        rec.columnconfigure(3, weight=1)
        ttk.Label(rec, text="AIの診断は").grid(row=0, column=0, sticky="w")
        self.hit_var = tk.StringVar(value="未確認")
        ttk.Combobox(rec, textvariable=self.hit_var, values=["未確認", "当たり", "外れ"],
                     state="readonly", width=8).grid(row=0, column=1, padx=6)
        ttk.Label(rec, text="実際の状態").grid(row=0, column=2, sticky="e")
        self.actual_var = tk.StringVar(value="不明")
        ttk.Combobox(rec, textvariable=self.actual_var, values=["不明", "正常", "異常あり"],
                     state="readonly", width=8).grid(row=0, column=3, sticky="w", padx=6)
        ttk.Label(rec, text="実際の原因").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.cause_var = tk.StringVar()
        ttk.Entry(rec, textvariable=self.cause_var).grid(row=1, column=1, columnspan=3, sticky="we", padx=6, pady=(4, 0))
        ttk.Label(rec, text="撮影条件").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.cond_var = tk.StringVar()
        ttk.Entry(rec, textvariable=self.cond_var).grid(row=2, column=1, columnspan=3, sticky="we", padx=6, pady=(4, 0))
        self.save_btn = ttk.Button(rec, text="記録を保存", command=self.save_eval)
        self.save_btn.grid(row=0, column=4, rowspan=2, padx=(10, 0), sticky="ns")
        ttk.Button(rec, text="これまでの記録を見る", command=self.show_history).grid(
            row=2, column=4, padx=(10, 0), pady=(4, 0))
        self.save_btn.state(["disabled"])

        root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        root.after(150, self.poll)

    def run(self):
        self.root.mainloop()

    # -- 画像の表示
    def to_tk(self, pil):
        buf = io.BytesIO()
        pil.save(buf, format="PNG")
        return self.tk.PhotoImage(data=base64.b64encode(buf.getvalue()))

    def show_image(self, pil, title, caption=""):
        win = self.tk.Toplevel(self.root)
        win.title(title)
        img = pil.copy()
        img.thumbnail((900, 640))
        tkimg = self.to_tk(img)
        lbl = self.ttk.Label(win, image=tkimg)
        lbl.image = tkimg
        lbl.pack(padx=8, pady=8)
        if caption:
            self.ttk.Label(win, text=caption).pack(pady=(0, 8))

    def open_photo(self, path):
        """写真ファイルを開いて別ウィンドウで見せる。"""
        if Image is None:
            self.mb.showinfo("写真", "写真の表示には Pillow が必要です。\npip install pillow")
            return None
        if not path or not os.path.isfile(path):
            self.mb.showinfo("写真", "写真のファイルが見つかりません。\n%s" % path)
            return None
        try:
            img = Image.open(path)
            img = ImageOps.exif_transpose(img) or img
            return img.convert("RGB")
        except Exception as e:
            self.mb.showinfo("写真", "写真を開けませんでした: %s" % e)
            return None

    def set_result(self, text):
        self.result.configure(state="normal")
        self.result.delete("1.0", "end")
        self.result.insert("1.0", text)
        self.result.configure(state="disabled")

    # -- 受信の処理（画面のスレッドで一定間隔ごとに確認する）
    def poll(self):
        if self.bridge.active:
            self.rtc_var.set("RTC: アクティブ（受信できます）")
        else:
            self.rtc_var.set("RTC: 非アクティブ（RT System Editor でつないでアクティブ化してください）")
        try:
            while True:
                text = self.bridge.inbox.get_nowait()
                self.handle(text)
        except queue.Empty:
            pass
        self.root.after(150, self.poll)

    def handle(self, text):
        msg = unpack(text) if (text or "").lstrip().startswith("{") else None
        if msg is None:  # JSONではない文字列は、そのまま結果として表示する
            self.on_result({"summary": (text or "").strip() or "（空のデータ）", "detail": "", "model": "", "id": ""})
            return
        kind = msg.get("type", "result")
        if kind == "status":
            self.status_var.set(str(msg.get("text", "")))
        elif kind == "error":
            self.set_result("エラー\n\n" + str(msg.get("text", "")))
            self.status_var.set("失敗")
            self.detail_btn.state(["disabled"])
            self.marks_btn.state(["disabled"])
            self.save_btn.state(["disabled"])
        else:
            self.on_result(msg)

    def on_result(self, msg):
        a = self.answer
        a.update(summary=str(msg.get("summary", "")), detail=str(msg.get("detail", "")),
                 model=str(msg.get("model", "")), showing_detail=False,
                 marked_path=str(msg.get("marked_image", "") or ""), log_id=msg.get("id") or None)
        self.show_answer()
        self.detail_btn.state(["!disabled"] if a["detail"] else ["disabled"])
        self.marks_btn.state(["!disabled"] if a["marked_path"] else ["disabled"])

        # 記録の入力欄を空に戻す
        self.hit_var.set("未確認")
        self.actual_var.set("不明")
        self.cause_var.set("")
        self.cond_var.set("")
        if a["log_id"] and msg.get("log_saved", True):
            self.save_btn.state(["!disabled"])
            self.status_var.set("完了（診断を記録しました）")
        else:
            self.save_btn.state(["disabled"])
            err = msg.get("log_error", "")
            self.status_var.set("完了" + ("（記録は失敗: %s）" % err if err else ""))

    def show_answer(self):
        a = self.answer
        tail = "\n\n（使用モデル: %s）" % a["model"] if a["model"] else ""
        if a["showing_detail"]:
            self.set_result(a["detail"] + tail)
            self.detail_btn.configure(text="まとめに戻る")
        else:
            self.set_result(a["summary"] + tail)
            self.detail_btn.configure(text="詳しく見る")

    def toggle_detail(self):
        self.answer["showing_detail"] = not self.answer["showing_detail"]
        self.show_answer()

    def show_marks(self):
        img = self.open_photo(self.answer["marked_path"])
        if img is not None:
            self.show_image(img, "怪しい所の場所",
                            "赤い枠と番号は、まとめの「怪しい所」の番号に対応します。\nAIの推測なので、位置がずれることがあります。")

    # -- 記録
    def save_eval(self):
        log_id = self.answer.get("log_id")
        if not log_id:
            return
        try:
            update_log(log_id, {"原因の当たり外れ": "" if self.hit_var.get() == "未確認" else self.hit_var.get(),
                                "実際の状態": "" if self.actual_var.get() == "不明" else self.actual_var.get(),
                                "実際の原因": self.cause_var.get().strip(), "撮影条件": self.cond_var.get().strip()})
            self.status_var.set("記録を保存しました")
        except (LogError, OSError) as e:
            self.status_var.set("記録に失敗: %s" % e)

    def show_history(self):
        tk, ttk = self.tk, self.ttk
        try:
            rows = read_log()
        except OSError as e:
            self.mb.showinfo("記録", "記録を読めませんでした: %s" % e)
            return
        if not rows:
            self.mb.showinfo("記録", "まだ記録がありません。診断すると自動で残ります。")
            return
        stats = log_stats(rows)
        win = tk.Toplevel(self.root)
        win.title("これまでの記録")
        win.geometry("860x600")
        txt = "診断した回数: %d回" % stats["total"]
        if stats["judge_n"]:
            txt += "　／　判定が実際の状態と一致: %d / %d回" % (stats["judge_ok"], stats["judge_n"])
        if stats["cause_n"]:
            txt += "　／　原因が当たり: %d / %d回" % (stats["cause_ok"], stats["cause_n"])
        ttk.Label(win, text=txt, font=("", 12)).pack(anchor="w", padx=10, pady=8)

        cols = ("日時", "AIの判定", "実際の状態", "判定の照合", "原因")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=8)
        for c, w in zip(cols, (150, 130, 100, 90, 90)):
            tree.heading(c, text=c)
            tree.column(c, width=w, anchor="w")
        tree.pack(fill="x", padx=10)
        for i, r in enumerate(reversed(rows)):  # 新しい順
            tree.insert("", "end", iid=str(len(rows) - 1 - i),
                        values=(r.get("日時", ""), r.get("AIの判定", ""), r.get("実際の状態") or "-",
                                r.get("判定の自動照合") or "-", r.get("原因の当たり外れ") or "-"))
        box = self.st.ScrolledText(win, wrap="word", height=14)
        box.pack(fill="both", expand=True, padx=10, pady=8)
        photo_btn = ttk.Button(win, text="場所を示した写真を見る")
        photo_btn.pack(pady=(0, 8))
        photo_btn.state(["disabled"])

        def show_logged_photo(p):
            img = self.open_photo(p)
            if img is not None:
                self.show_image(img, "場所を示した写真")

        def on_select(_e=None):
            sel = tree.selection()
            if not sel:
                return
            r = rows[int(sel[0])]
            box.configure(state="normal")
            box.delete("1.0", "end")
            box.insert("1.0", "【症状】%s\n【撮影条件】%s\n【実際の原因】%s\n\n%s" % (
                r.get("症状") or "（なし）", r.get("撮影条件") or "（なし）", r.get("実際の原因") or "（なし）",
                r.get("まとめ", "")))
            box.configure(state="disabled")
            p = r.get("場所を示した写真", "")
            if p and os.path.exists(p) and Image is not None:
                photo_btn.state(["!disabled"])
                photo_btn.configure(command=lambda: show_logged_photo(p))
            else:
                photo_btn.state(["disabled"])

        tree.bind("<<TreeviewSelect>>", on_select)
        tree.selection_set(str(len(rows) - 1))


def main():
    # 先にRTCを別スレッドで動かし、画面(tkinter)はメインスレッドで動かす
    mgr = OpenRTM_aist.Manager.init(sys.argv)
    mgr.setModuleInitProc(MyModuleInit)
    mgr.activateManager()
    mgr.runManager(True)   # True: 別スレッドで動かし、この関数は先へ進む
    try:
        DisplayApp(BRIDGE).run()
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
