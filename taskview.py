"""
TaskView — preview AO VIVO de todos os perfis (DWM Thumbnails).

Grade em tela cheia com o preview AO VIVO de cada janela de navegador (perfil AdsPower)
aberta. Usa DWM Thumbnails (DwmRegisterThumbnail): um espelho ao vivo composto pela
GPU — o video roda, custo de CPU ~zero, e funciona mesmo com a janela COBERTA por outra.

  - Janela MINIMIZADA aparece em branco (DWM nao compoe minimizadas) -> e pulada.
  - Clique num tile -> traz aquela janela pro 1o plano.
  - F5 / botao direito -> atualiza a lista de janelas. ESC -> sai.

Deps: tkinter (stdlib) + pywin32 + ctypes.   ->  pip install pywin32

Uso:
    python taskview.py                       # filtra por processo sunbrowser.exe
    python taskview.py --list                # DIAGNOSTICO: lista processos/titulos
    python taskview.py --proc sunbrowser.exe --interval 2.5
    python taskview.py --keyword twitch      # tambem entra por titulo
"""
import argparse
import ctypes
import math
from ctypes import wintypes

import tkinter as tk
import tkinter.font as tkfont

import win32con
import win32gui
import win32process

# DPI awareness ANTES de criar janela
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

dwmapi   = ctypes.windll.dwmapi
user32   = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]

class DWM_THUMBNAIL_PROPERTIES(ctypes.Structure):
    _fields_ = [
        ("dwFlags", ctypes.c_uint), ("rcDestination", RECT), ("rcSource", RECT),
        ("opacity", ctypes.c_ubyte), ("fVisible", ctypes.c_int),
        ("fSourceClientAreaOnly", ctypes.c_int),
    ]

DWM_TNP_RECTDESTINATION      = 0x00000001
DWM_TNP_VISIBLE              = 0x00000008
DWM_TNP_OPACITY              = 0x00000004
DWM_TNP_SOURCECLIENTAREAONLY = 0x00000010
DWMWA_CLOAKED = 14
GA_ROOT       = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

DwmRegisterThumbnail         = dwmapi.DwmRegisterThumbnail
DwmUnregisterThumbnail       = dwmapi.DwmUnregisterThumbnail
DwmUpdateThumbnailProperties = dwmapi.DwmUpdateThumbnailProperties
DwmQueryThumbnailSourceSize  = dwmapi.DwmQueryThumbnailSourceSize
DwmGetWindowAttribute        = dwmapi.DwmGetWindowAttribute


def _proc_name(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.split("\\")[-1].lower()
        return ""
    finally:
        kernel32.CloseHandle(h)

def _is_cloaked(hwnd):
    val = ctypes.c_int(0)
    try:
        DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        return False
    return val.value != 0


def enumerar_navegadores(keywords, procs, excluir_hwnds, excluir_titulos):
    """[(hwnd, titulo, procname)] das janelas top-level visiveis que casam por
    TITULO (contem keyword) OU por NOME DO PROCESSO."""
    keywords = [k.lower() for k in keywords if k]
    procs = {p.lower() for p in procs if p}
    achados = []

    def _cb(hwnd, _):
        if hwnd in excluir_hwnds:
            return True
        if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            return True
        if win32gui.GetWindow(hwnd, win32con.GW_OWNER):
            return True
        if _is_cloaked(hwnd):
            return True
        titulo = win32gui.GetWindowText(hwnd) or ""
        if not titulo:
            return True
        tl = titulo.lower()
        if any(x and x.lower() in tl for x in excluir_titulos):
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            pname = _proc_name(pid)
        except Exception:
            pname = ""
        if (keywords and any(k in tl for k in keywords)) or (procs and pname in procs):
            achados.append((hwnd, titulo, pname))
        return True

    win32gui.EnumWindows(_cb, None)
    def _pos(item):
        try:
            l, t, _, _ = win32gui.GetWindowRect(item[0])
            return (t // 50, l)
        except Exception:
            return (0, 0)
    achados.sort(key=_pos)
    return achados


class TaskView:
    PAD = 8
    CAPTION_H = 22

    def __init__(self, keywords, procs, intervalo):
        self.keywords = keywords
        self.procs = procs
        self.intervalo = max(500, int(intervalo * 1000))

        self.root = tk.Tk()
        self.root.title("twitch_swap — TaskView (preview ao vivo)")
        self.root.configure(bg="#0b0b0b")
        self.root.attributes("-fullscreen", True)
        self.root.update_idletasks()
        self.host_hwnd = user32.GetAncestor(self.root.winfo_id(), GA_ROOT)

        self.canvas = tk.Canvas(self.root, bg="#0b0b0b", highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.cap_font = tkfont.Font(family="Segoe UI", size=9)

        self.thumbs = {}   # hwnd -> handle do thumbnail
        self.ordem = []    # [(hwnd, titulo)] na ordem da grade
        self.cells = []    # rects das celulas

        self.root.bind("<Escape>", lambda e: self.fechar())
        self.root.bind("<F5>", lambda e: self.atualizar())
        self.canvas.bind("<Button-1>", self.on_click)
        self.canvas.bind("<Button-3>", lambda e: self.atualizar())
        self.canvas.bind("<Configure>", lambda e: self.relayout())
        self.root.protocol("WM_DELETE_WINDOW", self.fechar)
        self.atualizar()

    def atualizar(self):
        alvos = enumerar_navegadores(
            self.keywords, self.procs,
            excluir_hwnds={self.host_hwnd},
            excluir_titulos=["TaskView", "twitch_swap"],
        )
        atuais = {h for h, _, _ in alvos}
        for hwnd in list(self.thumbs.keys()):
            if hwnd not in atuais:
                try:
                    DwmUnregisterThumbnail(self.thumbs[hwnd])
                except Exception:
                    pass
                del self.thumbs[hwnd]
        for hwnd, _t, _p in alvos:
            if hwnd not in self.thumbs:
                thumb = wintypes.HANDLE()
                if DwmRegisterThumbnail(self.host_hwnd, hwnd, ctypes.byref(thumb)) == 0:
                    self.thumbs[hwnd] = thumb
        self.ordem = [(h, t) for (h, t, _) in alvos if h in self.thumbs]
        self.relayout()
        if getattr(self, "_vivo", True):
            self.root.after(self.intervalo, self.atualizar)

    def relayout(self):
        n = len(self.ordem)
        self.canvas.delete("all")
        self.cells = []
        if n == 0:
            self.canvas.create_text(
                self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2,
                text="Nenhum perfil aberto (ou todos minimizados).\nF5 atualiza · ESC sai",
                fill="#666", font=("Segoe UI", 16), justify="center")
            return
        W = max(1, self.canvas.winfo_width())
        H = max(1, self.canvas.winfo_height())
        cols = math.ceil(math.sqrt(n))
        rows = math.ceil(n / cols)
        cw, ch = W / cols, H / rows
        for i, (hwnd, titulo) in enumerate(self.ordem):
            r, c = divmod(i, cols)
            cx0, cy0 = int(c * cw) + self.PAD, int(r * ch) + self.PAD
            cx1, cy1 = int((c + 1) * cw) - self.PAD, int((r + 1) * ch) - self.PAD
            self.cells.append((cx0, cy0, cx1, cy1))
            self.canvas.create_rectangle(cx0, cy0, cx1, cy1, outline="#222", width=1)
            self.canvas.create_rectangle(cx0, cy0, cx1, cy0 + self.CAPTION_H,
                                         fill="#161616", outline="")
            txt = titulo if len(titulo) < 80 else titulo[:77] + "..."
            self.canvas.create_text(cx0 + 8, cy0 + self.CAPTION_H // 2, text=txt,
                                    anchor="w", fill="#cfcfcf", font=self.cap_font)
            area = (cx0 + 2, cy0 + self.CAPTION_H, cx1 - 2, cy1 - 2)
            self._posicionar_thumb(hwnd, area)

    def _posicionar_thumb(self, hwnd, area):
        ax0, ay0, ax1, ay1 = area
        aw, ah = max(1, ax1 - ax0), max(1, ay1 - ay0)
        thumb = self.thumbs.get(hwnd)
        if not thumb:
            return
        src = SIZE()
        try:
            DwmQueryThumbnailSourceSize(thumb, ctypes.byref(src))
        except Exception:
            src.cx, src.cy = 0, 0
        if src.cx > 0 and src.cy > 0:
            escala = min(aw / src.cx, ah / src.cy)
            dw, dh = max(1, int(src.cx * escala)), max(1, int(src.cy * escala))
            ox, oy = ax0 + (aw - dw) // 2, ay0 + (ah - dh) // 2
            dest = RECT(ox, oy, ox + dw, oy + dh)
        else:
            dest = RECT(ax0, ay0, ax1, ay1)
        props = DWM_THUMBNAIL_PROPERTIES()
        props.dwFlags = (DWM_TNP_RECTDESTINATION | DWM_TNP_VISIBLE
                         | DWM_TNP_OPACITY | DWM_TNP_SOURCECLIENTAREAONLY)
        props.rcDestination = dest
        props.opacity = 255
        props.fVisible = 1
        props.fSourceClientAreaOnly = 1   # corta barra/abas: mostra so a pagina
        try:
            DwmUpdateThumbnailProperties(thumb, ctypes.byref(props))
        except Exception:
            pass

    def on_click(self, event):
        for i, (x0, y0, x1, y1) in enumerate(self.cells):
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                if i < len(self.ordem):
                    self._focar(self.ordem[i][0])
                return

    def _focar(self, hwnd):
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            except Exception:
                pass

    def fechar(self):
        self._vivo = False
        for thumb in self.thumbs.values():
            try:
                DwmUnregisterThumbnail(thumb)
            except Exception:
                pass
        self.thumbs.clear()
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self._vivo = True
        self.root.mainloop()


def listar_todas():
    linhas = []
    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            return True
        if _is_cloaked(hwnd) or win32gui.GetWindow(hwnd, win32con.GW_OWNER):
            return True
        titulo = win32gui.GetWindowText(hwnd) or ""
        if not titulo:
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            pname = _proc_name(pid)
        except Exception:
            pname = "?"
        linhas.append((pname, titulo))
        return True
    win32gui.EnumWindows(_cb, None)
    linhas.sort()
    print("=" * 70)
    print("  JANELAS TOP-LEVEL VISIVEIS (processo | titulo)")
    print("=" * 70)
    for pname, titulo in linhas:
        print(f"  {pname:<26} | {titulo[:70]}")
    print("-" * 70)
    print(f"  {len(linhas)} janelas. Use o nome do processo do navegador em --proc")


def main():
    ap = argparse.ArgumentParser(description="TaskView ao vivo dos perfis (DWM Thumbnails)")
    ap.add_argument("--keyword", default="",
                    help="titulos que contem isto entram (virgula). Vazio = so por processo")
    ap.add_argument("--proc", default="sunbrowser.exe",
                    help="nomes de processo que entram (virgula). AdsPower = sunbrowser.exe")
    ap.add_argument("--interval", type=float, default=2.5,
                    help="segundos entre atualizacoes da lista de janelas")
    ap.add_argument("--list", action="store_true",
                    help="DIAGNOSTICO: lista janelas (processo+titulo) e sai")
    args = ap.parse_args()
    if args.list:
        listar_todas()
        return
    keywords = [s.strip() for s in args.keyword.split(",") if s.strip()]
    procs = [s.strip() for s in args.proc.split(",") if s.strip()]
    TaskView(keywords, procs, args.interval).run()


if __name__ == "__main__":
    main()
