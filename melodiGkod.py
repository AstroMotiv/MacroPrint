import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import os, re, math, bisect, threading
import numpy as np
import librosa


# =====================================================================
#                     МУЗЫКАЛЬНЫЕ УТИЛИТЫ
# =====================================================================
NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

def hz_to_midi(freq):
    if freq <= 0:
        return None
    return 69 + 12 * math.log2(freq / 440.0)

def midi_to_hz(midi):
    return 440.0 * (2 ** ((midi - 69) / 12))

def midi_to_name(midi):
    midi = int(round(midi))
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


# =====================================================================
#             ИЗВЛЕЧЕНИЕ НОТ ИЗ АУДИО (PYIN + КВАНТИЗАЦИЯ)
# =====================================================================
def extract_notes(audio_path, min_dur=0.08, fmin_hz=65.0, fmax_hz=2100.0,
                  quantize=True, merge_cents=50, progress_cb=None):
    """
    Возвращает список dict: {'start','end','freq','midi','name'}.
    Для пауз freq=0, name='rest'.
    """
    def _p(pct, txt):
        if progress_cb: progress_cb(pct, txt)

    _p(0, "Загрузка аудио...")
    y, sr = librosa.load(audio_path, sr=22050, mono=True)
    if len(y) == 0:
        raise RuntimeError("Аудиофайл пуст.")

    _p(10, "Нормализация и гармоническое разделение...")
    y = y / (np.max(np.abs(y)) + 1e-9)
    # Гармоническая составляющая = мелодия/инструменты, убирает ударные
    y_harm = librosa.effects.harmonic(y, margin=3)

    _p(25, "Определение высоты тона (pyin)...")
    hop = 512
    f0, voiced_flag, voiced_prob = librosa.pyin(
        y_harm,
        fmin=fmin_hz, fmax=fmax_hz,
        sr=sr, frame_length=2048, hop_length=hop,
        fill_na=0.0,
        # Ограничиваем диапазон количеством полутонов, чтобы ускорить
    )
    times = librosa.times_like(f0, sr=sr, hop_length=hop)

    _p(55, "Сегментация в ноты...")
    # Собираем непрерывные участки, где нота устойчива
    segments = []
    cur_start = None
    cur_freqs = []
    for t, f, v in zip(times, f0, voiced_flag):
        if v and f > 0 and not np.isnan(f):
            if cur_start is None:
                cur_start = t
            cur_freqs.append(f)
        else:
            if cur_start is not None:
                dur = t - cur_start
                if dur >= min_dur and cur_freqs:
                    segments.append((cur_start, t, float(np.median(cur_freqs))))
                cur_start = None
                cur_freqs = []
    if cur_start is not None and cur_freqs:
        dur = times[-1] - cur_start
        if dur >= min_dur:
            segments.append((cur_start, times[-1], float(np.median(cur_freqs))))

    _p(75, "Слияние и квантование нот...")
    # Слияние соседних сегментов с почти одинаковой высотой
    merged = []
    for seg in segments:
        if merged:
            pf = merged[-1][2]
            cf = seg[2]
            # Разница в центах
            cents = abs(1200 * math.log2(cf / pf)) if pf > 0 and cf > 0 else 9999
            if cents < merge_cents:
                merged[-1] = (merged[-1][0], seg[1], (merged[-1][2] + seg[2]) / 2)
                continue
        merged.append(seg)

    # Квантование к полутонам
    result = []
    for start, end, freq in merged:
        if quantize:
            midi = int(round(hz_to_midi(freq)))
            qf = midi_to_hz(midi)
            result.append({'start': start, 'end': end,
                           'freq': qf, 'midi': midi, 'name': midi_to_name(midi)})
        else:
            result.append({'start': start, 'end': end,
                           'freq': freq, 'midi': None, 'name': None})

    # Вставляем паузы между нотами (если разрыв > 40 мс)
    with_rests = []
    prev_end = 0.0
    for n in result:
        if n['start'] - prev_end > 0.04:
            with_rests.append({'start': prev_end, 'end': n['start'],
                               'freq': 0.0, 'midi': None, 'name': 'rest'})
        with_rests.append(n)
        prev_end = n['end']

    n_notes = sum(1 for n in with_rests if n['freq'] > 0)
    _p(100, f"Распознано нот: {n_notes}")
    return with_rests


# =====================================================================
#                    ПАРСЕР G-КОДА: ВРЕМЕННАЯ ШКАЛА
# =====================================================================
def parse_gcode_timeline(lines):
    """
    Возвращает (timeline, total_time), где
    timeline[i] — накопленное время (сек) до начала строки i.
    """
    x = y = z = e = 0.0
    feedrate = 1500.0                     # mm/min по умолчанию
    t = 0.0
    timeline = [0.0] * len(lines)

    # Предкомпилированные regex — быстрее в цикле
    move_re = re.compile(r'^G0?[0-3]\b', re.IGNORECASE)
    param_re = re.compile(r'([XYZEF])([-+]?\d*\.?\d+)', re.IGNORECASE)

    for i, raw in enumerate(lines):
        timeline[i] = t
        code = raw.split(';', 1)[0].strip()
        if not code:
            continue
        if not move_re.match(code):
            continue

        params = {}
        for m in param_re.finditer(code):
            params[m.group(1).upper()] = float(m.group(2))

        nx = params.get('X', x)
        ny = params.get('Y', y)
        nz = params.get('Z', z)
        ne = params.get('E', e)
        if 'F' in params and params['F'] > 0:
            feedrate = params['F']

        dx, dy, dz = nx - x, ny - y, nz - z
        de = ne - e
        dist_xyz = math.sqrt(dx * dx + dy * dy + dz * dz)

        # Оценка времени перемещения
        if dist_xyz > 1e-6:
            dist = dist_xyz
        elif abs(de) > 1e-6:
            # Чистая экструзия/ретракт — оценим через объём (диаметр филамента ~1.75)
            dist = abs(de) * 1.75 * math.pi  # очень грубо, но порядок верный
        else:
            dist = 0.0

        if feedrate > 0 and dist > 0:
            t += dist / (feedrate / 60.0)

        x, y, z, e = nx, ny, nz, ne

    return timeline, t


# =====================================================================
#              РАСПРЕДЕЛЕНИЕ МЕЛОДИИ ПО ВРЕМЕНИ ПЕЧАТИ
# =====================================================================
def build_melody_timeline(notes, total_print_time, mode='loop'):
    """
    notes — вывод extract_notes().
    Возвращает список (start_time, freq, duration) в пределах total_print_time.
    Режимы:
      - 'once'    : проиграть один раз с начала, без масштабирования
      - 'loop'    : сохранить темп, повторять, пока хватает времени печати
      - 'stretch' : пропорционально растянуть, чтобы точно заполнить всю печать
    """
    if not notes:
        return []

    t0 = notes[0]['start']
    # Нормализуем мелодию к началу = 0
    norm = [(n['start'] - t0, n['end'] - t0, n['freq']) for n in notes]
    melody_len = norm[-1][1] if norm else 0.0
    if melody_len <= 0:
        return []

    events = []
    if mode == 'once':
        for s, e, f in norm:
            events.append((s, f, e - s))

    elif mode == 'stretch':
        k = total_print_time / melody_len
        for s, e, f in norm:
            events.append((s * k, f, (e - s) * k))

    elif mode == 'loop':
        offset = 0.0
        while offset < total_print_time:
            for s, e, f in norm:
                start = offset + s
                if start >= total_print_time:
                    break
                end = min(offset + e, total_print_time)
                if end > start:
                    events.append((start, f, end - start))
            offset += melody_len

    return events


def insert_melody_distributed(lines, timeline, events):
    """
    Вставляет M300 перед строками, время которых ближе всего к началу ноты.
    Возвращает новый список строк G-кода.
    """
    insertions = {}   # индекс строки -> список команд
    for start, freq, dur in events:
        if dur <= 0 or freq <= 0:
            continue
        idx = bisect.bisect_left(timeline, start)
        if idx >= len(lines):
            idx = len(lines) - 1

        dur_ms = max(1, int(round(dur * 1000)))
        freq_hz = max(20, min(int(round(freq)), 15000))
        cmd = f"M300 S{freq_hz} P{dur_ms}"
        insertions.setdefault(idx, []).append(cmd)

    out = []
    for i, line in enumerate(lines):
        if i in insertions:
            out.append("; -- melody start --\n")
            for c in insertions[i]:
                out.append(c + "\n")
            out.append("; -- melody end --\n")
        out.append(line)
    return out


# =====================================================================
#                       ОСНОВНОЙ ПАЙПЛАЙН
# =====================================================================
def process(audio_path, gcode_path, out_path, mode,
            min_dur, fmin, fmax, quantize, progress_cb=None,
            log_cb=None):
    def _p(p, t):
        if progress_cb: progress_cb(p, t)
    def _log(t):
        if log_cb: log_cb(t)

    _p(0, "Анализ аудио...")
    notes = extract_notes(audio_path, min_dur=min_dur,
                          fmin_hz=fmin, fmax_hz=fmax,
                          quantize=quantize,
                          progress_cb=lambda p, t: _p(int(p * 0.5), t))

    n_notes = sum(1 for n in notes if n['freq'] > 0)
    _log(f"Распознано нот: {n_notes}, сегментов всего: {len(notes)}")
    if n_notes == 0:
        raise RuntimeError("Не удалось выделить ни одной ноты.")

    _p(50, "Чтение G-кода...")
    with open(gcode_path, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()

    _p(60, "Построение временной шкалы печати...")
    timeline, total_time = parse_gcode_timeline(lines)
    _log(f"Примерное время печати: {total_time/60:.1f} мин "
         f"({total_time:.0f} с)")

    if total_time <= 0:
        raise RuntimeError("Не удалось оценить время печати — проверьте G-код.")

    _p(75, "Распределение мелодии по всему файлу...")
    events = build_melody_timeline(notes, total_time, mode=mode)
    _log(f"Размещено нот в G-коде: {len(events)} (режим {mode})")

    new_lines = insert_melody_distributed(lines, timeline, events)

    _p(95, "Сохранение...")
    with open(out_path, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)

    _p(100, "Готово!")
    return n_notes, len(events), total_time


# =====================================================================
#                              GUI
# =====================================================================
class App:
    def __init__(self, root):
        self.root = root
        root.title("Melody → G-code  (распределение по всему файлу)")
        root.geometry("720x640")

        self.audio_path = tk.StringVar()
        self.gcode_path = tk.StringVar()
        self.mode = tk.StringVar(value='loop')
        self.min_dur = tk.DoubleVar(value=0.08)
        self.fmin = tk.DoubleVar(value=65.0)
        self.fmax = tk.DoubleVar(value=2100.0)
        self.quantize = tk.BooleanVar(value=True)

        self._build()

    def _build(self):
        pad = {'padx': 10, 'pady': 4}

        # --- Аудио ---
        tk.Label(self.root, text="Аудиофайл (мелодия):", anchor='w').pack(fill='x', **pad)
        f1 = tk.Frame(self.root); f1.pack(fill='x', padx=10)
        tk.Entry(f1, textvariable=self.audio_path).pack(side='left', fill='x', expand=True)
        tk.Button(f1, text="Обзор…", command=self._pick_audio).pack(side='right', padx=(6, 0))

        # --- G-code ---
        tk.Label(self.root, text="Файл G-кода:", anchor='w').pack(fill='x', **pad)
        f2 = tk.Frame(self.root); f2.pack(fill='x', padx=10)
        tk.Entry(f2, textvariable=self.gcode_path).pack(side='left', fill='x', expand=True)
        tk.Button(f2, text="Обзор…", command=self._pick_gcode).pack(side='right', padx=(6, 0))

        # --- Режим распределения ---
        box = tk.LabelFrame(self.root, text="Режим распределения мелодии")
        box.pack(fill='x', padx=10, pady=10)
        tk.Radiobutton(box, text="Loop — сохранить темп, повторять до конца печати",
                       variable=self.mode, value='loop').pack(anchor='w')
        tk.Radiobutton(box, text="Stretch — растянуть ноты, чтобы точно заполнить всю печать",
                       variable=self.mode, value='stretch').pack(anchor='w')
        tk.Radiobutton(box, text="Once — проиграть один раз в начале, без повторов",
                       variable=self.mode, value='once').pack(anchor='w')

        # --- Настройки распознавания ---
        box2 = tk.LabelFrame(self.root, text="Параметры распознавания нот")
        box2.pack(fill='x', padx=10, pady=6)

        r1 = tk.Frame(box2); r1.pack(fill='x', pady=2)
        tk.Label(r1, text="Мин. длительность ноты (с):", width=28, anchor='w').pack(side='left')
        tk.Spinbox(r1, from_=0.02, to=1.0, increment=0.01,
                   textvariable=self.min_dur, width=8).pack(side='left')
        tk.Label(r1, text="  (меньше = больше коротких нот)").pack(side='left')

        r2 = tk.Frame(box2); r2.pack(fill='x', pady=2)
        tk.Label(r2, text="Нижняя граница (Гц):", width=28, anchor='w').pack(side='left')
        tk.Spinbox(r2, from_=30, to=500, increment=5,
                   textvariable=self.fmin, width=8).pack(side='left')
        tk.Label(r2, text="  (C2 = 65 Гц)").pack(side='left')

        r3 = tk.Frame(box2); r3.pack(fill='x', pady=2)
        tk.Label(r3, text="Верхняя граница (Гц):", width=28, anchor='w').pack(side='left')
        tk.Spinbox(r3, from_=500, to=5000, increment=50,
                   textvariable=self.fmax, width=8).pack(side='left')
        tk.Label(r3, text="  (C7 = 2093 Гц)").pack(side='left')

        r4 = tk.Frame(box2); r4.pack(fill='x', pady=2)
        tk.Checkbutton(r4, text="Квантовать к полутонам (рекомендуется для M300)",
                       variable=self.quantize).pack(anchor='w')

        # --- Кнопка пуск ---
        self.run_btn = tk.Button(self.root, text="▶  Обработать и сохранить",
                                 bg="#4CAF50", fg="white",
                                 font=("Arial", 12, "bold"), command=self._run_async)
        self.run_btn.pack(pady=10, ipadx=10, ipady=4)

        # --- Прогресс ---
        self.pb = ttk.Progressbar(self.root, mode='determinate', maximum=100)
        self.pb.pack(fill='x', padx=10)
        self.status = tk.Label(self.root, text="Готов", fg='gray')
        self.status.pack(fill='x', padx=10, pady=(2, 6))

        # --- Лог ---
        tk.Label(self.root, text="Журнал:", anchor='w').pack(fill='x', padx=10)
        self.log = tk.Text(self.root, height=8, font=("Consolas", 9))
        self.log.pack(fill='both', expand=True, padx=10, pady=(0, 10))

    # --- выбор файлов ---
    def _pick_audio(self):
        p = filedialog.askopenfilename(
            title="Аудиофайл",
            filetypes=[("Аудио", "*.wav *.mp3 *.ogg *.flac *.m4a"),
                       ("Все файлы", "*.*")])
        if p: self.audio_path.set(p)

    def _pick_gcode(self):
        p = filedialog.askopenfilename(
            title="G-код",
            filetypes=[("G-код", "*.gcode *.gco *.nc *.gc"),
                       ("Все файлы", "*.*")])
        if p: self.gcode_path.set(p)

    # --- запуск в отдельном потоке ---
    def _run_async(self):
        a = self.audio_path.get()
        g = self.gcode_path.get()
        if not a or not os.path.isfile(a):
            messagebox.showerror("Ошибка", "Выберите корректный аудиофайл."); return
        if not g or not os.path.isfile(g):
            messagebox.showerror("Ошибка", "Выберите корректный файл G-кода."); return

        out = filedialog.asksaveasfilename(
            title="Сохранить G-код как",
            defaultextension=".gcode",
            filetypes=[("G-код", "*.gcode"), ("Все файлы", "*.*")])
        if not out: return

        self.log.delete('1.0', tk.END)
        self.run_btn.config(state='disabled')
        self.pb['value'] = 0
        self.status.config(text="Работаю…", fg='blue')

        t = threading.Thread(target=self._worker,
                             args=(a, g, out), daemon=True)
        t.start()

    def _worker(self, audio, gcode, out):
        def p_cb(p, txt):
            self.root.after(0, lambda: (self.pb.configure(value=p),
                                        self.status.config(text=f"{txt} ({p}%)")))
        def l_cb(txt):
            self.root.after(0, lambda: self.log.insert(tk.END, txt + "\n"))

        try:
            n, placed, total = process(
                audio, gcode, out,
                mode=self.mode.get(),
                min_dur=self.min_dur.get(),
                fmin=self.fmin.get(),
                fmax=self.fmax.get(),
                quantize=self.quantize.get(),
                progress_cb=p_cb, log_cb=l_cb)

            msg = (f"Готово!\n\n"
                   f"Распознано нот: {n}\n"
                   f"Вставлено M300: {placed}\n"
                   f"Длительность печати: {total/60:.1f} мин\n\n"
                   f"Файл: {out}")
            self.root.after(0, lambda: messagebox.showinfo("Успех", msg))
            self.root.after(0, lambda: self.status.config(text="Готово", fg='green'))
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Ошибка", str(e)))
            self.root.after(0, lambda: self.status.config(text="Ошибка", fg='red'))
        finally:
            self.root.after(0, lambda: self.run_btn.config(state='normal'))


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()