"""
===============================================================================
FastTestCoil Pro v3.3 — калибровка 3D-принтеров
===============================================================================
Слои: Profiles → GCodeWriter → TestGenerator → ThreeMFBuilder → GUI

v3.3:
  * аудит больше не путает возврат ретракта с экструзией
  * запрет печати при активном ретракте на уровне писателя
  * стол задаётся двумя противоположными углами
  * убрана галочка паспорта; значения оператора сохраняются и подхватываются
  * сопло только в профиле принтера, процесс использует множители
===============================================================================
"""

from __future__ import annotations

import json
import math
import re
import time
import zipfile
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace, asdict, fields
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

APP_VERSION = "3.3"
CONFIG_DIR = Path.home() / ".fasttestcoil"
DEFAULTS_FILE = CONFIG_DIR / "defaults.json"


class GCodeError(Exception):
    """Нарушение границ или инварианта при генерации."""


# =============================================================================
# 1. ПРОФИЛИ
# =============================================================================

@dataclass
class PrinterProfile:
    """
    Стол задаётся двумя противоположными углами (A и B) в машинных координатах.
    Ноль может быть где угодно; границы и центр считаются автоматически.
    """
    name: str
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 220.0
    y1: float = 220.0
    max_z: float = 220.0
    nozzle_diameter: float = 0.4
    max_nozzle_temp: int = 280
    max_bed_temp: int = 110
    travel_speed: float = 150.0
    z_speed: float = 10.0
    park_x: float = 5.0
    park_y: float = 5.0
    start_gcode: str = ""
    end_gcode: str = ""

    @property
    def x_min(self) -> float:
        return min(self.x0, self.x1)

    @property
    def x_max(self) -> float:
        return max(self.x0, self.x1)

    @property
    def y_min(self) -> float:
        return min(self.y0, self.y1)

    @property
    def y_max(self) -> float:
        return max(self.y0, self.y1)

    @property
    def size_x(self) -> float:
        return self.x_max - self.x_min

    @property
    def size_y(self) -> float:
        return self.y_max - self.y_min

    @property
    def center(self) -> Tuple[float, float]:
        return ((self.x_min + self.x_max) / 2.0,
                (self.y_min + self.y_max) / 2.0)

    def usable(self, margin: float) -> Tuple[float, float, float, float]:
        return (self.x_min + margin, self.y_min + margin,
                self.x_max - margin, self.y_max - margin)

    def inside(self, x: float, y: float, pad: float = 0.0) -> bool:
        return (self.x_min + pad <= x <= self.x_max - pad and
                self.y_min + pad <= y <= self.y_max - pad)

    def geometry_errors(self) -> List[str]:
        e: List[str] = []
        if self.size_x < 20:
            e.append("Размер стола по X меньше 20 мм")
        if self.size_y < 20:
            e.append("Размер стола по Y меньше 20 мм")
        if self.max_z < 5:
            e.append("Максимальная Z меньше 5 мм")
        if not self.inside(self.park_x, self.park_y, 1.0):
            e.append(f"Точка парковки ({self.park_x:g}, {self.park_y:g}) вне стола")
        return e


@dataclass
class MaterialProfile:
    name: str
    nozzle_temp: int = 210
    bed_temp: int = 60
    fan_speed: int = 255
    fan_first_layers: int = 1
    print_speed: float = 60.0
    first_layer_speed: float = 20.0
    retract_len: float = 0.8
    retract_speed: float = 40.0
    deretract_speed: float = 30.0
    filament_diameter: float = 1.75
    density: float = 1.24
    min_temp: int = 180
    max_temp: int = 230
    temp_test_start: int = 190
    temp_test_end: int = 230


@dataclass
class ProcessProfile:
    """Геометрия слоя. Ширина линии = сопло принтера × множитель."""
    name: str = "Default"
    layer_height: float = 0.2
    first_layer_height: float = 0.2
    line_width_factor: float = 1.05
    first_layer_width_factor: float = 1.25
    z_hop: float = 0.6
    clearance: float = 2.0
    prime_enable: bool = True
    prime_len: float = 60.0
    bed_margin: float = 12.0

    @staticmethod
    def filament_area(diameter: float) -> float:
        d = max(0.1, diameter)
        return math.pi * (d / 2.0) ** 2


_START = (
    "M107\n"
    "G21\n"
    "G90\n"
    "M83\n"
    "M140 S{bed_temp}\n"
    "M104 S{nozzle_temp}\n"
    "G28\n"
    "M190 S{bed_temp}\n"
    "M109 S{nozzle_temp}\n"
    "G90\n"
    "G0 Z5 F1200\n"
)

_END = (
    "M107\n"
    "M104 S0\n"
    "M140 S0\n"
    "G90\n"
    "G0 X{park_x} Y{park_y} F6000\n"
    "M84\n"
)

PRINTERS: Dict[str, PrinterProfile] = {
    "Flashforge Adventurer 5M": PrinterProfile(
        name="Flashforge Adventurer 5M",
        x0=-110, y0=-110, x1=110, y1=110,
        max_z=220, nozzle_diameter=0.4,
        max_nozzle_temp=280, max_bed_temp=110,
        travel_speed=200, z_speed=10,
        park_x=-105, park_y=-105,
        start_gcode=_START, end_gcode=_END),
    "Flashforge AD5X": PrinterProfile(
        name="Flashforge AD5X",
        x0=0, y0=0, x1=220, y1=220,
        max_z=220, nozzle_diameter=0.4,
        max_nozzle_temp=300, max_bed_temp=110,
        travel_speed=200, z_speed=10,
        park_x=5, park_y=5,
        start_gcode=_START, end_gcode=_END),
    "FlyingBear Reborn 2": PrinterProfile(
        name="FlyingBear Reborn 2",
        x0=0, y0=0, x1=330, y1=330,
        max_z=350, nozzle_diameter=0.4,
        max_nozzle_temp=300, max_bed_temp=120,
        travel_speed=150, z_speed=10,
        park_x=5, park_y=5,
        start_gcode=_START, end_gcode=_END),
}

MATERIALS: Dict[str, MaterialProfile] = {
    "PLA": MaterialProfile("PLA", 210, 60, 255, 1, 60, 20, 0.8, 40, 30,
                           1.75, 1.24, 180, 230, 190, 230),
    "PETG": MaterialProfile("PETG", 240, 80, 100, 3, 50, 18, 1.2, 35, 25,
                            1.75, 1.27, 220, 260, 225, 260),
    "ABS": MaterialProfile("ABS", 250, 100, 25, 4, 55, 20, 1.0, 40, 30,
                           1.75, 1.04, 230, 270, 235, 270),
    "ASA": MaterialProfile("ASA", 255, 100, 30, 4, 55, 18, 1.0, 40, 30,
                           1.75, 1.07, 235, 275, 240, 275),
    "Nylon (PA)": MaterialProfile("Nylon (PA)", 260, 80, 30, 5, 40, 15, 1.5, 30, 25,
                                  1.75, 1.14, 240, 285, 245, 280),
    "PC": MaterialProfile("PC", 280, 110, 10, 5, 40, 15, 1.2, 35, 25,
                          1.75, 1.20, 260, 310, 265, 300),
    "TPU 95A": MaterialProfile("TPU 95A", 230, 50, 150, 2, 25, 15, 0.6, 25, 20,
                               1.75, 1.21, 210, 250, 215, 245),
    "TPU 85A": MaterialProfile("TPU 85A", 225, 50, 140, 2, 20, 12, 0.5, 22, 18,
                               1.75, 1.20, 205, 245, 210, 240),
    "TPU 60A": MaterialProfile("TPU 60A", 220, 45, 128, 2, 15, 10, 0.4, 20, 15,
                               1.75, 1.18, 200, 240, 205, 235),
}


# =============================================================================
# 2. ПИСАТЕЛЬ G-CODE
# =============================================================================

class GCodeWriter:
    """
    Примитивы G0/G1 + термика. M83: E — приращение на отрезке.
    Печать при активном ретракте запрещена на уровне писателя.
    Любая команда вне стола/Z вызывает GCodeError.
    """

    def __init__(self, printer: PrinterProfile, process: ProcessProfile):
        self.p = printer
        self.pr = process
        self.lines: List[str] = []
        self.x = self.y = self.z = 0.0
        self.printed_max_z = 0.0
        self._retracted = False

    def _check(self, x, y, z, where):
        p = self.p
        if x is not None and not (p.x_min - 1e-6 <= x <= p.x_max + 1e-6):
            raise GCodeError(f"{where}: X={x:.3f} вне [{p.x_min:g}, {p.x_max:g}]")
        if y is not None and not (p.y_min - 1e-6 <= y <= p.y_max + 1e-6):
            raise GCodeError(f"{where}: Y={y:.3f} вне [{p.y_min:g}, {p.y_max:g}]")
        if z is not None and not (-1e-6 <= z <= p.max_z + 1e-6):
            raise GCodeError(f"{where}: Z={z:.3f} вне [0, {p.max_z:g}]")

    def comment(self, text=""):
        self.lines.append(f"; {text}" if text else ";")

    def block(self, title):
        self.comment("-" * 58)
        self.comment(title)
        self.comment("-" * 58)

    def raw(self, code):
        for ln in code.strip().splitlines():
            if ln.strip():
                self.lines.append(ln.rstrip())

    def travel(self, x, y, z=None, speed=None):
        self._check(x, y, z, "холостой")
        f = (speed if speed is not None else self.p.travel_speed) * 60.0
        cmd = f"G0 X{x:.3f} Y{y:.3f}"
        if z is not None:
            cmd += f" Z{z:.3f}"
        self.lines.append(f"{cmd} F{f:.0f}")
        self.x, self.y = x, y
        if z is not None:
            self.z = z

    def lift(self, z):
        self._check(None, None, z, "подъём Z")
        self.lines.append(f"G0 Z{z:.3f} F{self.p.z_speed * 60:.0f}")
        self.z = z

    def print_to(self, x, y, e, speed, z=None, note=""):
        self._check(x, y, z, "печать")
        if e > 0 and self._retracted:
            raise GCodeError("Экструзия при активном ретракте")
        cmd = f"G1 X{x:.3f} Y{y:.3f}"
        if z is not None:
            cmd += f" Z{z:.3f}"
        cmd += f" E{max(0.0, e):.5f} F{max(1.0, speed) * 60:.0f}"
        if note:
            cmd += f"  ; {note}"
        self.lines.append(cmd)
        self.x, self.y = x, y
        if z is not None:
            self.z = z
        if e > 0:
            self.printed_max_z = max(self.printed_max_z, self.z)

    def safe_travel(self, x, y, z_target):
        need = max(self.printed_max_z, self.z, z_target) + self.pr.clearance
        need = min(need, self.p.max_z - 0.5)
        if need < self.printed_max_z:
            raise GCodeError("Не хватает Z для безопасного перехода")
        if self.z < need - 1e-6:
            self.lift(need)
        self.travel(x, y)
        if abs(self.z - z_target) > 1e-6:
            self.lift(z_target)

    def retract(self, length, speed):
        if self._retracted or length <= 0:
            return
        self.lines.append(f"G1 E-{length:.3f} F{max(1.0, speed) * 60:.0f}")
        self._retracted = True

    def deretract(self, length, speed):
        if not self._retracted or length <= 0:
            return
        self.lines.append(f"G1 E{length:.3f} F{max(1.0, speed) * 60:.0f}")
        self._retracted = False

    def reset_e(self):
        self.lines.append("G92 E0")

    def nozzle(self, temp, wait=False):
        t = int(round(temp))
        if t > self.p.max_nozzle_temp:
            raise GCodeError(f"Сопло {t} °C выше предела {self.p.max_nozzle_temp}")
        self.lines.append(f"{'M109' if wait else 'M104'} S{t}")

    def bed(self, temp, wait=False):
        t = int(temp)
        if t > self.p.max_bed_temp:
            raise GCodeError(f"Стол {t} °C выше предела {self.p.max_bed_temp}")
        self.lines.append(f"{'M190' if wait else 'M140'} S{t}")

    def fan(self, speed):
        s = int(max(0, min(255, speed)))
        self.lines.append(f"M106 S{s}" if s else "M107")

    def dwell(self, ms, note=""):
        ms = int(max(0, ms))
        if ms:
            self.lines.append(f"G4 P{ms}" + (f"  ; {note}" if note else ""))

    def home(self, axes=""):
        self.lines.append(f"G28 {axes}".strip())
        if not axes:
            self.x = self.y = self.z = 0.0
        if "Z" in axes:
            self.z = 0.0

    def text(self):
        return "\n".join(self.lines) + "\n"


# =============================================================================
# 3. АУДИТ G-CODE
# =============================================================================

def audit_gcode(text: str, printer: PrinterProfile) -> Tuple[List[str], List[str]]:
    """
    Независимая проверка текста.
    Важно: G1 E+… БЕЗ движения XYZ — это возврат ретракта, а не экструзия.
    """
    errors: List[str] = []
    warns: List[str] = []
    pos = {"X": None, "Y": None, "Z": None}
    absolute = True
    rel_e = True
    e_abs = 0.0
    retracted = False
    homed = False
    nozzle_target = 0
    printed_max_z = None
    first_extrusion = False
    lim = {"X": (printer.x_min, printer.x_max),
           "Y": (printer.y_min, printer.y_max),
           "Z": (0.0, printer.max_z)}

    for n, raw_line in enumerate(text.splitlines(), 1):
        code = raw_line.split(";")[0].strip()
        if not code:
            continue
        up = code.upper()

        if up.startswith("G28"):
            homed = True
            axes = up[3:].strip()
            for a in ("X", "Y", "Z"):
                if not axes or a in axes:
                    pos[a] = None
            continue
        if up.startswith("G90"):
            absolute = True
            continue
        if up.startswith("G91"):
            absolute = False
            continue
        if up.startswith("M82"):
            rel_e = False
            continue
        if up.startswith("M83"):
            rel_e = True
            continue
        if up.startswith("G92"):
            m = re.search(r"E(-?\d*\.?\d+)", up)
            if m:
                e_abs = float(m.group(1))
            continue
        m = re.match(r"^M(104|109)\b", up)
        if m:
            s = re.search(r"S(-?\d*\.?\d+)", up)
            if s:
                nozzle_target = int(float(s.group(1)))
                if nozzle_target > printer.max_nozzle_temp:
                    errors.append(f"стр.{n}: сопло {nozzle_target} выше предела "
                                  f"{printer.max_nozzle_temp}")
            continue
        m = re.match(r"^M(140|190)\b", up)
        if m:
            s = re.search(r"S(-?\d*\.?\d+)", up)
            if s and int(float(s.group(1))) > printer.max_bed_temp:
                errors.append(f"стр.{n}: стол выше предела {printer.max_bed_temp}")
            continue
        if not re.match(r"^G0?[01]\b", up):
            continue

        vals = {k: float(v) for k, v in re.findall(r"([XYZE])(-?\d*\.?\d+)", up)}
        new = dict(pos)
        for a in ("X", "Y", "Z"):
            if a in vals:
                if absolute:
                    new[a] = vals[a]
                elif pos[a] is not None:
                    new[a] = pos[a] + vals[a]
        for a in ("X", "Y", "Z"):
            v = new[a]
            if v is None:
                continue
            lo, hi = lim[a]
            if v < lo - 1e-6 or v > hi + 1e-6:
                errors.append(f"стр.{n}: {a}={v:.3f} вне [{lo:g}, {hi:g}]")

        e_delta = 0.0
        if "E" in vals:
            e_delta = vals["E"] if rel_e else vals["E"] - e_abs
            if not rel_e:
                e_abs = vals["E"]

        has_motion = any(a in vals for a in ("X", "Y", "Z"))
        if e_delta < -1e-9:
            retracted = True
        elif e_delta > 1e-9:
            if has_motion:
                # настоящая экструзия
                if retracted:
                    errors.append(f"стр.{n}: экструзия при активном ретракте")
                retracted = False
                if not homed:
                    errors.append(f"стр.{n}: экструзия до G28")
                if nozzle_target < 150:
                    errors.append(f"стр.{n}: экструзия при T={nozzle_target}")
                z = new["Z"]
                if z is not None:
                    if z < 0.02:
                        errors.append(f"стр.{n}: экструзия на Z={z:.3f}")
                    printed_max_z = z if printed_max_z is None else max(printed_max_z, z)
                first_extrusion = True
            else:
                # возврат ретракта (E без движения)
                retracted = False
        else:
            moved = any(new[a] is not None and pos[a] is not None
                        and abs(new[a] - pos[a]) > 1e-6 for a in ("X", "Y"))
            z = new["Z"]
        pos = new

    if not first_extrusion:
        warns.append("Нет ни одной экструзии")
    if not homed:
        errors.append("Нет G28")
    return errors, warns

# =============================================================================
# 3b. РАСЧЁТ ВРЕМЕНИ ПЕЧАТИ
# =============================================================================

EST_ACCEL = 3000.0        # мм/с², оценка разгона
EST_NOZZLE_HEAT = 2.0     # °C/с
EST_NOZZLE_COOL = 0.6
EST_BED_HEAT = 0.8
EST_BED_COOL = 0.3
EST_HOME_ALL = 15.0       # с, G28 всех осей
EST_HOME_ONE = 5.0
EST_AMBIENT = 25.0


def _move_time(dist: float, v: float, a: float) -> float:
    """Трапецеидальный/треугольный профиль разгона."""
    if dist <= 0 or v <= 0:
        return 0.0
    if dist >= v * v / a:
        return dist / v + v / a
    return 2.0 * math.sqrt(dist / a)


def format_duration(sec: float) -> str:
    sec = int(round(sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def estimate_time(text: str, printer: PrinterProfile,
                  material: MaterialProfile) -> dict:
    pos = {"X": None, "Y": None, "Z": None}
    feed = printer.travel_speed * 60.0
    absolute, rel_e, e_abs = True, True, 0.0
    t = 0.0
    acc = {"print": 0.0, "travel": 0.0, "retract": 0.0,
           "heat": 0.0, "dwell": 0.0, "home": 0.0}
    nozzle, bed = EST_AMBIENT, EST_AMBIENT
    pend_n = pend_b = None
    filament_mm = 0.0

    for raw_line in text.splitlines():
        code = raw_line.split(";")[0].strip()
        if not code:
            continue
        up = code.upper()

        if up.startswith("G28"):
            axes = up[3:].strip()
            dt = EST_HOME_ALL if not axes else EST_HOME_ONE
            acc["home"] += dt; t += dt
            for a in ("X", "Y", "Z"):
                if not axes or a in axes:
                    pos[a] = None
            continue
        if up.startswith("G90"):
            absolute = True; continue
        if up.startswith("G91"):
            absolute = False; continue
        if up.startswith("M82"):
            rel_e = False; continue
        if up.startswith("M83"):
            rel_e = True; continue
        if up.startswith("G92"):
            m = re.search(r"E(-?\d*\.?\d+)", up)
            if m:
                e_abs = float(m.group(1))
            continue

        m = re.match(r"^M(104|109)\b", up)
        if m:
            s = re.search(r"S(-?\d*\.?\d+)", up)
            if s:
                target = float(s.group(1))
                if m.group(1) == "104":
                    pend_n = (target, t, nozzle)
                    nozzle = target
                else:
                    if pend_n and abs(pend_n[0] - target) < 0.5:
                        tgt, st, t0 = pend_n
                        rate = EST_NOZZLE_HEAT if tgt >= t0 else EST_NOZZLE_COOL
                        wait = max(0.0, st + abs(tgt - t0) / rate - t)
                    else:
                        rate = EST_NOZZLE_HEAT if target >= nozzle else EST_NOZZLE_COOL
                        wait = abs(target - nozzle) / rate
                    acc["heat"] += wait; t += wait
                    nozzle, pend_n = target, None
            continue

        m = re.match(r"^M(140|190)\b", up)
        if m:
            s = re.search(r"S(-?\d*\.?\d+)", up)
            if s:
                target = float(s.group(1))
                if m.group(1) == "140":
                    pend_b = (target, t, bed)
                    bed = target
                else:
                    if pend_b and abs(pend_b[0] - target) < 0.5:
                        tgt, st, t0 = pend_b
                        rate = EST_BED_HEAT if tgt >= t0 else EST_BED_COOL
                        wait = max(0.0, st + abs(tgt - t0) / rate - t)
                    else:
                        rate = EST_BED_HEAT if target >= bed else EST_BED_COOL
                        wait = abs(target - bed) / rate
                    acc["heat"] += wait; t += wait
                    bed, pend_b = target, None
            continue

        if up.startswith("G4"):
            p = re.search(r"P(\d+)", up)
            s = re.search(r"S(\d+)", up)
            dt = int(p.group(1)) / 1000.0 if p else (float(s.group(1)) if s else 0.0)
            acc["dwell"] += dt; t += dt
            continue

        if not re.match(r"^G0?[01]\b", up):
            continue

        vals = {k: float(v) for k, v in
                re.findall(r"([XYZEF])(-?\d*\.?\d+)", up)}
        if "F" in vals:
            feed = vals["F"]
        new = dict(pos)
        for a in ("X", "Y", "Z"):
            if a in vals:
                new[a] = vals[a] if absolute else (
                    pos[a] + vals[a] if pos[a] is not None else None)
        d2, has_xyz = 0.0, False
        for a in ("X", "Y", "Z"):
            if a in vals:
                has_xyz = True
                if pos[a] is not None and new[a] is not None:
                    d2 += (new[a] - pos[a]) ** 2
        dist = math.sqrt(d2)

        e_delta = 0.0
        if "E" in vals:
            e_delta = vals["E"] if rel_e else vals["E"] - e_abs
            if not rel_e:
                e_abs = vals["E"]

        v = feed / 60.0
        if has_xyz and dist > 0:
            dt = _move_time(dist, v, EST_ACCEL)
            acc["print" if e_delta > 0 else "travel"] += dt
        elif abs(e_delta) > 0:
            dt = abs(e_delta) / max(1.0, v)
            acc["retract"] += dt
        else:
            dt = 0.0
        t += dt
        filament_mm += e_delta
        pos = new

    filament_mm = max(0.0, filament_mm)
    area = ProcessProfile.filament_area(material.filament_diameter)
    grams = filament_mm * area / 1000.0 * material.density
    return {"total": t, "parts": acc,
            "filament_mm": filament_mm, "filament_g": grams}


def annotate_gcode(text: str, est: dict) -> str:
    p = est["parts"]
    block = [
        "",
        "; ---- FASTTESTCOIL TIME ESTIMATE ----",
        f"; print moves : {p['print']:.1f} s",
        f"; travel      : {p['travel']:.1f} s",
        f"; retract     : {p['retract']:.1f} s",
        f"; heat/wait   : {p['heat']:.1f} s",
        f"; dwell       : {p['dwell']:.1f} s",
        f"; home        : {p['home']:.1f} s",
        f"; TOTAL       : {est['total']:.1f} s  ({format_duration(est['total'])})",
        f"; filament    : {est['filament_mm']:.1f} mm / {est['filament_g']:.2f} g",
        f"; model       : accel {EST_ACCEL:g} mm/s2, nozzle {EST_NOZZLE_HEAT:g} C/s, "
        f"bed {EST_BED_HEAT:g} C/s, home {EST_HOME_ALL:g} s",
        f"; estimated printing time (normal mode) = {format_duration(est['total'])}",
        f"; total filament used [g] = {est['filament_g']:.2f}",
    ]
    return text.rstrip("\n") + "\n" + "\n".join(block) + "\n"

# =============================================================================
# 4. БАЗОВЫЙ ГЕНЕРАТОР
# =============================================================================

class TestGenerator(ABC):
    def __init__(self, printer, material, process):
        self.printer = printer
        self.material = material
        self.process = process
        self.w = GCodeWriter(printer, process)
        self.nozzle = printer.nozzle_diameter
        self.lw = max(0.05, self.nozzle * process.line_width_factor)
        self.flw = max(0.05, self.nozzle * process.first_layer_width_factor)

    @property
    def MARGIN(self):
        return max(1.0, self.process.bed_margin)

    @abstractmethod
    def parameters(self): ...
    @abstractmethod
    def validate(self, p): ...
    @abstractmethod
    def generate(self, p): ...
    @abstractmethod
    def bounds(self, p): ...

    def info_line(self, p):
        b = self.bounds(p)
        return f"Габарит: {b[0]:.2f} × {b[1]:.2f} × {b[2]:.2f} мм"

    def title(self):
        return type(self).__name__.replace("TestGenerator", "")

    def place_centered(self, w, d):
        cx, cy = self.printer.center
        return cx - w / 2.0, cy - d / 2.0

    def placement_errors(self, w, d):
        e = []
        ux0, uy0, ux1, uy1 = self.printer.usable(self.MARGIN)
        if w > (ux1 - ux0):
            e.append(f"По X нужно {w:.2f}, доступно {ux1-ux0:.1f} мм")
        if d > (uy1 - uy0):
            e.append(f"По Y нужно {d:.2f}, доступно {uy1-uy0:.1f} мм")
        return e

    def common_errors(self):
        e = list(self.printer.geometry_errors())
        if self.process.layer_height > self.nozzle * 0.85:
            e.append(f"Высота слоя {self.process.layer_height:g} > 0.85×Ø сопла")
        if self.process.first_layer_height > self.nozzle:
            e.append("Высота 1-го слоя больше диаметра сопла")
        return e

    def e_for(self, length, flow=1.0, width=None, height=None):
        lw = width if width is not None else self.lw
        lh = height if height is not None else self.process.layer_height
        area = ProcessProfile.filament_area(self.material.filament_diameter)
        return max(0.0, length) * lw * lh * flow / area

    def z_of(self, layer):
        if layer <= 1:
            return self.process.first_layer_height
        return self.process.first_layer_height + (layer - 1) * self.process.layer_height

    def lh_of(self, layer):
        return self.process.first_layer_height if layer <= 1 else self.process.layer_height

    def speed_of(self, layer):
        return self.material.first_layer_speed if layer <= 1 else self.material.print_speed

    def _subst(self, code, nozzle, bed):
        p = self.printer
        cx, cy = p.center
        rep = {
            "{nozzle_temp}": str(int(round(nozzle))),
            "{bed_temp}": str(int(bed)),
            "{park_x}": f"{p.park_x:.3f}", "{park_y}": f"{p.park_y:.3f}",
            "{x_min}": f"{p.x_min:.3f}", "{x_max}": f"{p.x_max:.3f}",
            "{y_min}": f"{p.y_min:.3f}", "{y_max}": f"{p.y_max:.3f}",
            "{center_x}": f"{cx:.3f}", "{center_y}": f"{cy:.3f}",
            "{bed_x}": f"{p.size_x:.3f}", "{bed_y}": f"{p.size_y:.3f}",
            "{max_z}": f"{p.max_z:.3f}",
        }
        for k, v in rep.items():
            code = code.replace(k, v)
        return code

    def header(self, p, extra):
        w = self.w
        pr = self.printer
        w.comment("=" * 62)
        w.comment(f"FastTestCoil Pro v{APP_VERSION} — {self.title().upper()}")
        w.comment("=" * 62)
        w.comment(f"Printer : {pr.name}")
        w.comment(f"Bed     : X[{pr.x_min:g},{pr.x_max:g}] "
                  f"Y[{pr.y_min:g},{pr.y_max:g}] Z[0,{pr.max_z:g}]")
        w.comment(f"Center  : ({pr.center[0]:.2f}, {pr.center[1]:.2f})")
        w.comment(f"Material: {self.material.name}")
        for ln in extra:
            w.comment(ln)
        w.comment(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        w.comment()

    def start(self, nozzle_temp, bed_temp):
        w = self.w
        w.block("START")
        w.raw(self._subst(self.printer.start_gcode, nozzle_temp, bed_temp))
        w.nozzle(nozzle_temp, wait=True)
        w.bed(bed_temp, wait=True)
        w.fan(0)
        w.reset_e()

    def finish(self):
        w, p = self.w, self.printer
        w.block("END")
        w.retract(self.material.retract_len, self.material.retract_speed)
        z_top = min(max(w.z, w.printed_max_z) + 10.0, p.max_z - 2.0)
        w.lift(z_top)
        w.travel(p.park_x, p.park_y)
        w.raw(self._subst(self.printer.end_gcode, 0, 0))

    def prime_line(self, flow=1.0):
        pr, w, m = self.process, self.w, self.material
        if not pr.prime_enable:
            return
        ux0, _, ux1, _ = self.printer.usable(self.MARGIN)
        y = self.printer.y_min + max(3.0, self.MARGIN * 0.4)
        x0 = ux0
        x1 = min(x0 + max(10.0, pr.prime_len), ux1)
        if x1 - x0 < 10.0:
            return
        z = pr.first_layer_height
        w.comment("Прочисточная линия")
        w.safe_travel(x0, y, z)
        w.deretract(m.retract_len, m.deretract_speed)
        w.print_to(x1, y, self.e_for(x1 - x0, flow, self.flw,
                                     pr.first_layer_height),
                   m.first_layer_speed, z, "prime")
        w.retract(m.retract_len, m.retract_speed)

    def fan_for_layer(self, layer):
        self.w.fan(0 if layer <= self.material.fan_first_layers
                   else self.material.fan_speed)


# =============================================================================
# 4a. ТЕСТ ПОТОКА
# =============================================================================

class FlowTestGenerator(TestGenerator):
    """
    Сплошная дорожка: длина по X, ширина по Y.
    Линии идут вдоль Y, шаг между линиями по X, градиент потока по X.
    """

    def parameters(self):
        return {
            "length_mm": dict(label="Длина дорожки (X)", default=80.0,
                              type="float", min=20.0, max=300.0, unit="мм"),
            "width_mm": dict(label="Ширина дорожки (Y)", default=24.0,
                             type="float", min=20.0, max=200.0, unit="мм"),
            "layers": dict(label="Слоёв", default=4, type="int", min=1, max=40),
            "flow_start": dict(label="Поток от", default=0.85,
                               type="float", min=0.40, max=2.0),
            "flow_end": dict(label="Поток до", default=1.15,
                             type="float", min=0.40, max=2.0),
            "nozzle_temp": dict(label="Температура сопла",
                                default=self.material.nozzle_temp,
                                type="int", min=150, max=400, unit="°C"),
            "bed_temp": dict(label="Температура стола",
                             default=self.material.bed_temp,
                             type="int", min=0, max=150, unit="°C"),
        }

    def n_lines(self, p):
        return max(2, int(round(p["length_mm"] / self.lw)))

    def actual_length(self, p):
        return self.n_lines(p) * self.lw

    def validate(self, p):
        e = self.common_errors()
        e += self.placement_errors(self.actual_length(p), p["width_mm"])
        if p["flow_start"] >= p["flow_end"]:
            e.append("Поток «от» должен быть меньше «до»")
        if p["nozzle_temp"] > self.printer.max_nozzle_temp:
            e.append(f"Сопло: предел {self.printer.max_nozzle_temp} °C")
        if p["bed_temp"] > self.printer.max_bed_temp:
            e.append(f"Стол: предел {self.printer.max_bed_temp} °C")
        if self.z_of(p["layers"]) + self.process.clearance > self.printer.max_z - 2:
            e.append("Высота + просвет превышает Z")
        return e

    def bounds(self, p):
        return (self.actual_length(p), p["width_mm"], self.z_of(p["layers"]))

    def info_line(self, p):
        return (f"Длина {self.actual_length(p):.2f} мм по X, "
                f"ширина {p['width_mm']:.1f} мм по Y  |  "
                f"{self.n_lines(p)} линий вдоль Y, шаг {self.lw:.3f} мм  |  "
                f"градиент потока по X")

    def generate(self, p):
        w, pr, m = self.w, self.process, self.material
        n = self.n_lines(p)
        length = self.actual_length(p)   # X
        width = p["width_mm"]            # Y
        x0, y0 = self.place_centered(length, width)

        self.header(p, [
            f"Block : {length:.2f} (X) x {width:.1f} (Y) x "
            f"{self.z_of(p['layers']):.2f} мм",
            f"Place : X[{x0:.2f},{x0+length:.2f}] Y[{y0:.2f},{y0+width:.2f}]",
            f"Lines : {n} вдоль Y, шаг {self.lw:.3f} мм, градиент по X",
            f"Flow  : {p['flow_start']:.3f} -> {p['flow_end']:.3f}",
        ])
        self.start(p["nozzle_temp"], p["bed_temp"])
        self.prime_line(1.0)

        for layer in range(1, p["layers"] + 1):
            w.comment()
            w.block(f"LAYER {layer}/{p['layers']}  Z={self.z_of(layer):.3f}")
            self.fan_for_layer(layer)
            z, speed, lh = self.z_of(layer), self.speed_of(layer), self.lh_of(layer)
            for i in range(n):
                t = i / (n - 1)
                flow = p["flow_start"] + (p["flow_end"] - p["flow_start"]) * t
                x = x0 + (i + 0.5) * self.lw
                ya, yb = (y0, y0 + width) if i % 2 == 0 else (y0 + width, y0)
                w.retract(m.retract_len, m.retract_speed)
                if i == 0:
                    w.safe_travel(x, ya, z)
                else:
                    w.lift(min(z + pr.z_hop, self.printer.max_z - 0.5))
                    w.travel(x, ya)
                    w.lift(z)
                w.deretract(m.retract_len, m.deretract_speed)
                w.print_to(x, yb, self.e_for(width, flow, self.lw, lh),
                           speed, z, f"flow={flow:.3f}")
            w.retract(m.retract_len, m.retract_speed)
        self.finish()
        return w.text()

# =============================================================================
# 4b. ТЕМПЕРАТУРНЫЙ ТЕСТ — ДИАГОНАЛЬНАЯ ВОЛНА
# =============================================================================

class TemperatureWaveTestGenerator(TestGenerator):
    """
    Прямоугольный брусок, диагональная волна.
    Температура линейно растёт по ходу печати (по номеру сегмента).
    """

    def parameters(self):
        return {
            "length_mm": dict(label="Длина бруска (X)", default=40.0,
                              type="float", min=4.0, max=300.0, unit="мм"),
            "width_mm": dict(label="Ширина бруска (Y)", default=20.0,
                             type="float", min=4.0, max=300.0, unit="мм"),
            "height_mm": dict(label="Высота бруска", default=8.0,
                              type="float", min=0.4, max=300.0, unit="мм"),
            "step_size": dict(label="Ступенька (в Ø сопла)", default=1.0,
                              type="float", min=0.25, max=20.0),
            "temp_start": dict(label="T начала печати",
                               default=self.material.temp_test_start,
                               type="int", min=150, max=400, unit="°C"),
            "temp_end": dict(label="T конца печати",
                             default=self.material.temp_test_end,
                             type="int", min=150, max=400, unit="°C"),
            "flow_percent": dict(label="Поток", default=100.0,
                                 type="float", min=40.0, max=200.0, unit="%"),
            "temp_hysteresis": dict(label="Порог смены T", default=2.0,
                                    type="float", min=0.1, max=20.0, unit="°C"),
            "wait_temp": dict(label="Ждать нагрев (M109)", default=1, type="bool"),
            "park_on_temp": dict(label="Парковать при смене T", default=1, type="bool"),
            "heatsoak_s_per_deg": dict(label="Выдержка на 1 °C", default=0.3,
                                       type="float", min=0.0, max=5.0, unit="с"),
            "print_speed": dict(label="Скорость (0=профиль)", default=0.0,
                                type="float", min=0.0, max=400.0, unit="мм/с"),
            "bed_temp": dict(label="Температура стола",
                             default=self.material.bed_temp,
                             type="int", min=0, max=150, unit="°C"),
            "smart_clearance": dict(label="Просвет по карте колонок", default=1, type="bool"),
        }

    def geom(self, p):
        d = self.nozzle
        step_size = max(0.25, p["step_size"])
        step_w = step_size * d
        n_lines = max(1, int(round(step_size)))
        spacing = step_w / n_lines
        n_cols = max(1, int(round(p["length_mm"] / step_w)))
        n_layers = max(1, int(round(p["height_mm"] / self.process.layer_height)))
        return dict(step_w=step_w, n_lines=n_lines, spacing=spacing,
                    n_cols=n_cols, n_layers=n_layers, total_w=n_cols * step_w,
                    n_segments=n_cols * n_layers * n_lines)

    @staticmethod
    def wave_steps(n_cols, n_layers):
        steps = [(c, l) for c in range(1, n_cols + 1)
                        for l in range(1, n_layers + 1)]
        steps.sort(key=lambda q: (q[0] + q[1], -q[0]))
        return steps

    @staticmethod
    def _col_at(x, x_start, step_w, n_cols):
        return max(1, min(n_cols, int((x - x_start) / step_w) + 1))

    def validate(self, p):
        e = self.common_errors()
        g = self.geom(p)
        e += self.placement_errors(g["total_w"], p["width_mm"])
        if p["temp_start"] == p["temp_end"]:
            e.append("Температуры начала и конца должны различаться")
        hi = max(p["temp_start"], p["temp_end"])
        lo = min(p["temp_start"], p["temp_end"])
        if hi > self.printer.max_nozzle_temp:
            e.append(f"T {hi} выше предела {self.printer.max_nozzle_temp}")
        if p["bed_temp"] > self.printer.max_bed_temp:
            e.append(f"Стол: предел {self.printer.max_bed_temp}")
        if lo < self.material.min_temp - 15:
            e.append(f"Нижняя T {lo} холодна для {self.material.name}")
        if hi > self.material.max_temp + 15:
            e.append(f"Верхняя T {hi} горяча для {self.material.name}")
        if self.z_of(g["n_layers"]) + self.process.clearance > self.printer.max_z - 2:
            e.append("Высота + просвет превышает Z")
        if g["n_segments"] > 30000:
            e.append(f"{g['n_segments']} сегментов — слишком долго")
        x0, y0 = self.place_centered(g["total_w"], p["width_mm"])
        if (x0 - 1 <= self.printer.park_x <= x0 + g["total_w"] + 1 and
                y0 - 1 <= self.printer.park_y <= y0 + p["width_mm"] + 1):
            e.append("Парковка попадает на деталь")
        return e

    def bounds(self, p):
        g = self.geom(p)
        return (g["total_w"], p["width_mm"], self.z_of(g["n_layers"]))

    def info_line(self, p):
        g = self.geom(p)
        x0, y0 = self.place_centered(g["total_w"], p["width_mm"])
        n_changes = int(abs(p["temp_end"] - p["temp_start"]) /
                        max(0.1, p["temp_hysteresis"]))
        return (f"{g['n_cols']} кол. × {g['n_layers']} сл. × {g['n_lines']} лин. "
                f"= {g['n_segments']} сегм.  |  X[{x0:.1f},{x0+g['total_w']:.1f}] "
                f"Y[{y0:.1f},{y0+p['width_mm']:.1f}]  |  ≈{n_changes} смен T")

    def generate(self, p):
        w, pr, m = self.w, self.process, self.material
        g = self.geom(p)
        step_w, spacing = g["step_w"], g["spacing"]
        n_cols, n_layers, n_lines = g["n_cols"], g["n_layers"], g["n_lines"]
        total_w = g["total_w"]
        width = p["width_mm"]
        flow = p["flow_percent"] / 100.0
        hyst = max(0.1, p["temp_hysteresis"])
        soak = max(0.0, p["heatsoak_s_per_deg"])
        wait = bool(p["wait_temp"])
        park = bool(p["park_on_temp"])
        smart = bool(p["smart_clearance"])
        speed_ovr = p["print_speed"]

        x_start, y_min = self.place_centered(total_w, width)
        y_max = y_min + width
        steps = self.wave_steps(n_cols, n_layers)
        total_seg = len(steps) * n_lines

        self.header(p, [
            "Shape : прямоугольный брусок, диагональная волна",
            f"Block : {total_w:.2f} x {width:.1f} x {self.z_of(n_layers):.2f} мм",
            f"Place : X[{x_start:.2f},{x_start+total_w:.2f}] "
            f"Y[{y_min:.2f},{y_max:.2f}]",
            f"Grid  : {n_cols} колонок × {n_layers} слоёв, {n_lines} линий/ступеньку",
            f"Temp  : {p['temp_start']} -> {p['temp_end']} °C ЛИНЕЙНО ПО ХОДУ ПЕЧАТИ",
            f"Flow  : {p['flow_percent']:g} %   Bed: {p['bed_temp']} °C",
        ])
        self.start(p["temp_start"], p["bed_temp"])
        self.prime_line(flow)

        col_top = [0.0] * (n_cols + 1)
        y_dir = 1
        last_temp = None
        fan_state = None
        retract_thr = max(1.5, 2.0 * spacing)
        seg_no = 0
        park_z = min(self.printer.max_z - 1.0, 5.0)

        for step_no, (col, layer) in enumerate(steps, 1):
            z = self.z_of(layer)
            lh = self.lh_of(layer)
            speed = speed_ovr if speed_ovr > 0 else self.speed_of(layer)
            x_col0 = x_start + (col - 1) * step_w
            front = col + layer

            want_fan = front > 1 + m.fan_first_layers
            if fan_state is None or want_fan != fan_state:
                w.fan(m.fan_speed if want_fan else 0)
                fan_state = want_fan

            w.comment()
            w.block(f"STEP {step_no}/{len(steps)}  col={col} layer={layer} "
                    f"Z={z:.3f} front={front}")

            for i in range(n_lines):
                seg_no += 1
                x = x_col0 + (i + 0.5) * spacing

                frac = (seg_no - 1) / max(1, total_seg - 1)
                temp_now = p["temp_start"] + (p["temp_end"] - p["temp_start"]) * frac

                if last_temp is None or abs(temp_now - last_temp) >= hyst:
                    delta = 0.0 if last_temp is None else abs(temp_now - last_temp)
                    if park and last_temp is not None:
                        w.retract(m.retract_len, m.retract_speed)
                        w.safe_travel(self.printer.park_x, self.printer.park_y, park_z)
                        w.comment(f"Нагрев {last_temp:.0f} -> {temp_now:.0f} на парковке")
                    w.nozzle(temp_now, wait=False)
                    if soak > 0 and delta > 0:
                        w.dwell(int(soak * delta * 1000), f"прогрев dT={delta:.1f}")
                    if wait:
                        w.nozzle(temp_now, wait=True)
                    last_temp = temp_now

                ys, ye = (y_min, y_max) if y_dir == 1 else (y_max, y_min)

                if smart:
                    c_from = self._col_at(w.x, x_start, step_w, n_cols)
                    c_to = self._col_at(x, x_start, step_w, n_cols)
                    lo, hi = min(c_from, c_to), max(c_from, c_to)
                    top = max(col_top[c] for c in range(lo, hi + 1))
                else:
                    top = w.printed_max_z
                need_z = min(max(top, w.z, z) + pr.clearance, self.printer.max_z - 0.5)
                if need_z < top:
                    raise GCodeError("Не хватает Z для безопасного перехода")
                need_lift = w.z < need_z - 1e-6
                dist = math.hypot(x - w.x, ys - w.y)
                do_retract = need_lift or dist > retract_thr

                if do_retract:
                    w.retract(m.retract_len, m.retract_speed)
                if need_lift:
                    w.lift(need_z)
                w.travel(x, ys)
                if abs(w.z - z) > 1e-6:
                    w.lift(z)
                w.deretract(m.retract_len, m.deretract_speed)
                w.print_to(x, ye, self.e_for(width, flow, spacing, lh),
                           speed, z, f"{seg_no}/{total_seg} T={temp_now:.0f}")
                col_top[col] = max(col_top[col], z)
                y_dir *= -1

        w.retract(m.retract_len, m.retract_speed)
        self.finish()
        return w.text()


# =============================================================================
# 4c. ТЕСТ Z-OFFSET
# =============================================================================

class ZOffsetTestGenerator(TestGenerator):
    """
    Полоса первого слоя: длина по X, ширина по Y, линии вдоль Y.
    Z постоянна в пределах линии и линейно меняется от линии к линии вдоль X.
    Число ступеней градиента = число линий.
    """

    def parameters(self):
        return {
            "length_mm": dict(label="Длина полосы (X)", default=100.0,
                              type="float", min=30.0, max=300.0, unit="мм"),
            "width_mm": dict(label="Ширина полосы (Y)", default=20.0,
                             type="float", min=20.0, max=200.0, unit="мм"),
            "z_start": dict(label="Z-offset от (X=начало)", default=-0.10,
                            type="float", min=-0.30, max=0.50, unit="мм"),
            "z_end": dict(label="Z-offset до (X=конец)", default=0.10,
                          type="float", min=-0.30, max=0.50, unit="мм"),
            "rehome_z": dict(label="Обнулить Z касанием (G28 Z)", default=1,
                             type="bool"),
            "flow": dict(label="Поток (множитель)", default=1.0,
                         type="float", min=0.40, max=2.0),
            "nozzle_temp": dict(label="Температура сопла",
                                default=self.material.nozzle_temp,
                                type="int", min=150, max=400, unit="°C"),
            "bed_temp": dict(label="Температура стола",
                             default=self.material.bed_temp,
                             type="int", min=0, max=150, unit="°C"),
        }

    def n_lines(self, p):
        return max(2, int(round(p["length_mm"] / self.flw)))

    def actual_length(self, p):
        return self.n_lines(p) * self.flw

    def validate(self, p):
        e = self.common_errors()
        e += self.placement_errors(self.actual_length(p), p["width_mm"])
        if abs(p["z_end"] - p["z_start"]) < 0.02:
            e.append("Диапазон Z-offset меньше 0.02 мм")
        zmin = self.process.first_layer_height + min(p["z_start"], p["z_end"])
        if zmin < 0.04:
            e.append(f"Минимальная Z {zmin:.3f} — сопло будет царапать стол")
        if p["nozzle_temp"] > self.printer.max_nozzle_temp:
            e.append(f"Сопло: предел {self.printer.max_nozzle_temp} °C")
        if p["bed_temp"] > self.printer.max_bed_temp:
            e.append(f"Стол: предел {self.printer.max_bed_temp} °C")
        return e

    def bounds(self, p):
        return (self.actual_length(p), p["width_mm"],
                self.process.first_layer_height)

    def info_line(self, p):
        n = self.n_lines(p)
        length = self.actual_length(p)
        span = p["z_end"] - p["z_start"]
        dz = span / (n - 1)
        rate = span / length
        return (f"Длина {length:.2f} мм по X, ширина {p['width_mm']:.1f} мм по Y  |  "
                f"{n} линий вдоль Y  |  Z {p['z_start']:+.3f}→{p['z_end']:+.3f}, "
                f"шаг {dz:+.5f} мм/линия ({rate:+.5f} мм на 1 мм X)")

    def generate(self, p):
        w, pr, m = self.w, self.process, self.material
        n = self.n_lines(p)
        length = n * self.flw        # X
        width = p["width_mm"]        # Y
        lh = pr.first_layer_height
        z_start, z_end = p["z_start"], p["z_end"]
        span = z_end - z_start
        dz = span / (n - 1)
        rate = span / length
        x0, y0 = self.place_centered(length, width)

        self.header(p, [
            f"Strip : {length:.2f} (X) x {width:.1f} (Y) мм, первый слой",
            f"Place : X[{x0:.2f},{x0+length:.2f}] Y[{y0:.2f},{y0+width:.2f}]",
            f"Lines : {n} вдоль Y, шаг {self.flw:.3f} мм",
            f"Z-off : {z_start:+.3f} -> {z_end:+.3f} мм вдоль X, "
            f"постоянна в пределах линии",
            f"Scale : {rate:+.5f} мм Z на 1 мм X, шаг {dz:+.5f} мм на линию",
            f"Zero  : {'G28 Z касание' if p.get('rehome_z') else 'вручную'}",
            f"Temp  : сопло {p['nozzle_temp']} °C, стол {p['bed_temp']} °C, "
            f"поток {p['flow']:.3f}",
        ])
        w.comment("Опорные точки градиента (ищите лучшую зону и сверяйте X):")
        for k in range(5):
            off = z_start + span * k / 4.0
            xmark = x0 + (off - z_start) / span * length
            w.comment(f"  X={xmark:8.2f}  ->  offset {off:+.3f} мм  "
                      f"(Z={lh + off:.3f})")

        self.start(p["nozzle_temp"], p["bed_temp"])
        if p.get("rehome_z"):
            w.block("Z ZERO (touch)")
            w.lift(max(5.0, w.z))
            w.home("Z")
            w.lift(5.0)

        self.fan_for_layer(1)
        w.comment()
        w.block("Z-OFFSET GRADIENT (первый слой)")

        for i in range(n):
            off = z_start + i * dz
            z = max(0.04, lh + off)
            x = x0 + (i + 0.5) * self.flw
            ya, yb = (y0, y0 + width) if i % 2 == 0 else (y0 + width, y0)

            if i == 0:
                w.retract(m.retract_len, m.retract_speed)
                w.safe_travel(x, ya, z)
                w.deretract(m.retract_len, m.deretract_speed)
            else:
                # переход к соседней линии без ретракта: давление в сопле
                # сохраняется, все линии печатаются в одинаковых условиях
                z_safe = min(max(w.printed_max_z, w.z, z) + pr.z_hop,
                             self.printer.max_z - 0.5)
                w.lift(z_safe)
                w.travel(x, ya)
                w.lift(z)

            w.print_to(x, yb, self.e_for(width, p["flow"], self.flw, lh),
                       m.first_layer_speed, z,
                       f"line {i+1}/{n} off={off:+.4f} Z={z:.3f}")

        w.retract(m.retract_len, m.retract_speed)
        self.finish()
        return w.text()


GENERATORS = {
    "Поток": FlowTestGenerator,
    "Температура (волна)": TemperatureWaveTestGenerator,
    "Z-offset": ZOffsetTestGenerator,
}


# =============================================================================
# 5. 3MF
# =============================================================================

class ThreeMFBuilder:
    VERSION = "2.5.0"

    @staticmethod
    def _box(w, d, h):
        w, d, h = max(1.0, w), max(1.0, d), max(0.2, h)
        v = [(0,0,0),(w,0,0),(w,d,0),(0,d,0),(0,0,h),(w,0,h),(w,d,h),(0,d,h)]
        t = [(0,2,1),(0,3,2),(4,5,6),(4,6,7),(0,1,5),(0,5,4),
             (2,3,7),(2,7,6),(0,4,7),(0,7,3),(1,2,6),(1,6,5)]
        vx = "\n".join(f'          <vertex x="{a:.3f}" y="{b:.3f}" z="{c:.3f}"/>'
                       for a, b, c in v)
        tx = "\n".join(f'          <triangle v1="{a}" v2="{b}" v3="{c}"/>'
                       for a, b, c in t)
        return vx, tx

    def build(self, path, gcode, printer, material, process,
              test_name, params, size, est=None):
        vx, tx = self._box(*size)
        nozzle = params.get("nozzle_temp",
                            params.get("temp_start", material.nozzle_temp))
        bed = params.get("bed_temp", material.bed_temp)
        flow = (params["flow_percent"] / 100.0 if "flow_percent" in params
                else params.get("flow", params.get("flow_end", 1.0)))
        lw = printer.nozzle_diameter * process.line_width_factor
        flw = printer.nozzle_diameter * process.first_layer_width_factor
        cx, cy = printer.center
        settings = {
            "printer_model": printer.name,
            "printer_variant": f"{printer.nozzle_diameter:.1f}",
            "printer_settings_id": printer.name,
            "printable_area": [f"{printer.x_min:g}x{printer.y_min:g}",
                               f"{printer.x_max:g}x{printer.y_min:g}",
                               f"{printer.x_max:g}x{printer.y_max:g}",
                               f"{printer.x_min:g}x{printer.y_max:g}"],
            "printable_height": str(printer.max_z),
            "nozzle_diameter": [str(printer.nozzle_diameter)],
            "filament_type": [material.name],
            "filament_colour": ["#FF7A00"],
            "filament_diameter": [str(material.filament_diameter)],
            "filament_density": [str(material.density)],
            "filament_flow_ratio": [str(round(flow, 4))],
            "nozzle_temperature": [str(nozzle)],
            "nozzle_temperature_initial_layer": [str(nozzle)],
            "bed_temperature": [str(bed)],
            "bed_temperature_initial_layer": [str(bed)],
            "fan_max_speed": [str(round(material.fan_speed / 255 * 100))],
            "fan_min_speed": [str(round(material.fan_speed / 255 * 100))],
            "layer_height": str(process.layer_height),
            "initial_layer_print_height": str(process.first_layer_height),
            "line_width": str(round(lw, 4)),
            "initial_layer_line_width": str(round(flw, 4)),
            "default_speed": str(material.print_speed),
            "initial_layer_speed": str(material.first_layer_speed),
            "travel_speed": str(printer.travel_speed),
            "retraction_length": [str(material.retract_len)],
            "retract_speed": [str(material.retract_speed)],
            "deretract_speed": [str(material.deretract_speed)],
            "z_hop": [str(process.z_hop)],
            "print_settings_id": f"FastTestCoil {test_name}",
            "filament_settings_id": material.name,
            "enable_support": "0",
            "enable_prime_tower": "0",
        }
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml",
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
                '<Default Extension="gcode" ContentType="text/x.gcode"/>'
                '<Default Extension="config" ContentType="text/xml"/>'
                '<Default Extension="json" ContentType="application/json"/>'
                '</Types>')
            z.writestr("_rels/.rels",
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rel0" Target="/3D/3dmodel.model" '
                'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
                '</Relationships>')
            z.writestr("3D/3dmodel.model",
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<model unit="millimeter" xml:lang="en-US" '
                'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
                f'  <metadata name="Application">OrcaSlicer-{self.VERSION}</metadata>\n'
                f'  <metadata name="Title">FastTestCoil {test_name}</metadata>\n'
                f'  <metadata name="CreationDate">{time.strftime("%Y-%m-%d")}</metadata>\n'
                '  <resources>\n'
                '    <object id="1" name="CalibrationTest" type="model">\n'
                '      <mesh>\n        <vertices>\n'
                f'{vx}\n        </vertices>\n        <triangles>\n'
                f'{tx}\n        </triangles>\n      </mesh>\n'
                '    </object>\n  </resources>\n'
                f'  <build><item objectid="1" transform="1 0 0 0 0 1 0 0 0 0 1 '
                f'{cx:.3f} {cy:.3f} 0"/></build>\n'
                '</model>')
            z.writestr("Metadata/project_settings.config",
                       json.dumps(settings, indent=2, ensure_ascii=False))
            z.writestr("Metadata/model_settings.config",
                '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
                '  <plate>\n'
                '    <metadata key="plater_id" value="1"/>\n'
                '    <metadata key="plater_name" value="Calibration"/>\n'
                '    <metadata key="gcode_file" value="Metadata/plate_1.gcode"/>\n'
                '  </plate>\n'
                '  <object id="1">\n'
                '    <metadata key="name" value="CalibrationTest"/>\n'
                '    <metadata key="extruder" value="1"/>\n'
                '  </object>\n</config>')
            z.writestr("Metadata/plate_1.gcode", gcode)
            secs = est["total"] if est else max(60, gcode.count("\n") * 0.08)
            grams = est["filament_g"] if est else 5.0
            used_m = est["filament_mm"] / 1000.0 if est else 1.5
            z.writestr("Metadata/slice_info.config",
                       '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
                       '  <header><header_item key="X-BBL-Client-Type" value="slicer"/>'
                       f'<header_item key="X-BBL-Client-Version" value="{self.VERSION}"/></header>\n'
                       '  <plate>\n'
                       '    <metadata key="index" value="1"/>\n'
                       f'    <metadata key="prediction" value="{secs:.0f}"/>\n'
                       f'    <metadata key="weight" value="{grams:.2f}"/>\n'
                       '    <metadata key="support_used" value="false"/>\n'
                       '    <metadata key="outside" value="false"/>\n'
                       f'    <metadata key="printer_model_id" value="{printer.name}"/>\n'
                       f'    <metadata key="nozzle_diameters" value="{printer.nozzle_diameter}"/>\n'
                       f'    <filament id="1" type="{material.name}" color="#FF7A00" '
                       f'used_m="{used_m:.3f}" used_g="{grams:.2f}"/>\n'
                       '  </plate>\n</config>')


# =============================================================================
# 6. СПЕЦИФИКАЦИИ ПОЛЕЙ
# =============================================================================

PRINTER_SPEC = [
    ("x0", "Угол A X, мм", "f", -2000, 2000),
    ("y0", "Угол A Y, мм", "f", -2000, 2000),
    ("x1", "Угол B X, мм", "f", -2000, 2000),
    ("y1", "Угол B Y, мм", "f", -2000, 2000),
    ("max_z", "Максимум Z, мм", "f", 5, 1500),
    ("nozzle_diameter", "Сопло, мм", "f", 0.1, 2.0),
    ("max_nozzle_temp", "Предел сопла, °C", "i", 150, 500),
    ("max_bed_temp", "Предел стола, °C", "i", 0, 200),
    ("travel_speed", "Холостые, мм/с", "f", 1, 1000),
    ("z_speed", "Скорость Z, мм/с", "f", 0.5, 100),
    ("park_x", "Парковка X, мм", "f", -2000, 2000),
    ("park_y", "Парковка Y, мм", "f", -2000, 2000),
]

MATERIAL_SPEC = [
    ("nozzle_temp", "Сопло, °C", "i", 150, 400),
    ("bed_temp", "Стол, °C", "i", 0, 150),
    ("fan_speed", "Обдув, 0..255", "i", 0, 255),
    ("fan_first_layers", "Фронтов без обдува", "i", 0, 20),
    ("print_speed", "Печать, мм/с", "f", 1, 400),
    ("first_layer_speed", "1-й слой, мм/с", "f", 1, 200),
    ("retract_len", "Ретракт, мм", "f", 0, 10),
    ("retract_speed", "Скорость ретракта, мм/с", "f", 1, 150),
    ("deretract_speed", "Скорость возврата, мм/с", "f", 1, 150),
    ("filament_diameter", "Диаметр прутка, мм", "f", 1.0, 3.5),
    ("density", "Плотность, г/см³", "f", 0.5, 3.0),
    ("min_temp", "Рекоменд. минимум, °C", "i", 150, 400),
    ("max_temp", "Рекоменд. максимум, °C", "i", 150, 400),
    ("temp_test_start", "Тест: T от, °C", "i", 150, 400),
    ("temp_test_end", "Тест: T до, °C", "i", 150, 400),
]

PROCESS_SPEC = [
    ("layer_height", "Высота слоя, мм", "f", 0.02, 1.5),
    ("first_layer_height", "Высота 1-го слоя, мм", "f", 0.02, 1.5),
    ("line_width_factor", "Множитель ширины линии", "f", 0.5, 3.0),
    ("first_layer_width_factor", "Множитель ширины 1-го слоя", "f", 0.5, 3.0),
    ("z_hop", "Z-hop, мм", "f", 0.0, 10.0),
    ("clearance", "Запас над печатью, мм", "f", 0.2, 20.0),
    ("bed_margin", "Отступ от края, мм", "f", 1.0, 50.0),
    ("prime_len", "Длина прочистки, мм", "f", 10.0, 200.0),
    ("prime_enable", "Печатать прочистку", "b", 0, 1),
]


# =============================================================================
# 7. ХРАНИЛИЩЕ
# =============================================================================

def load_defaults() -> Optional[dict]:
    try:
        if not DEFAULTS_FILE.exists():
            return None
        data = json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("app") != "FastTestCoil":
            return None
        return data
    except Exception:
        return None


def save_defaults(data: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = dict(data, app="FastTestCoil", version=APP_VERSION,
                saved=time.strftime("%Y-%m-%d %H:%M:%S"))
    tmp = DEFAULTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(DEFAULTS_FILE)


def clear_defaults():
    try:
        if DEFAULTS_FILE.exists():
            DEFAULTS_FILE.unlink()
    except Exception:
        pass


def profile_from_dict(cls, base, data):
    obj = replace(base)
    names = {f.name for f in fields(cls)}
    for k, v in (data or {}).items():
        if k in names:
            try:
                setattr(obj, k, v)
            except Exception:
                pass
    return obj


# =============================================================================
# 8. GUI
# =============================================================================

class App:
    def __init__(self, root):
        self.root = root
        root.title(f"FastTestCoil Pro v{APP_VERSION}")
        root.geometry("1000x860")
        root.minsize(860, 660)

        self.printers = {k: replace(v) for k, v in PRINTERS.items()}
        self.materials = {k: replace(v) for k, v in MATERIALS.items()}
        self.printer = replace(next(iter(self.printers.values())))
        self.material = replace(self.materials["PLA"])
        self.process = replace(ProcessProfile())

        self.pv = {}
        self.mv = {}
        self.cv = {}
        self.tv = {}
        self.entries = {}
        self.cache = {}
        self.gcode = None
        self.last_params = {}
        self.last_test = ""
        self.last_bounds = (50.0, 30.0, 5.0)
        self.last_est = None
        self._loading = False
        self._saved_note = ""
        self._pending_test = None

        self._apply_saved()
        self._build()
        self._load_printer()
        self._load_material()
        self._load_process()
        self._rebuild_test()
        if self._saved_note:
            self.status.set(self._saved_note)

    def _apply_saved(self):
        d = load_defaults()
        if not d:
            return
        try:
            pn = d.get("printer_name")
            mn = d.get("material_name")
            if d.get("printer"):
                base = self.printers.get(pn, self.printer)
                self.printer = profile_from_dict(PrinterProfile, base, d["printer"])
                if pn and pn not in self.printers:
                    self.printers[pn] = replace(self.printer)
            if d.get("material"):
                base = self.materials.get(mn, self.material)
                self.material = profile_from_dict(MaterialProfile, base, d["material"])
                if mn and mn not in self.materials:
                    self.materials[mn] = replace(self.material)
            if d.get("process"):
                self.process = profile_from_dict(ProcessProfile, self.process, d["process"])
            if isinstance(d.get("tests"), dict):
                self.cache = {k: v for k, v in d["tests"].items() if isinstance(v, dict)}
            self._pending_test = d.get("test_name")
            self._saved_note = f"Загружены значения оператора от {d.get('saved','?')}"
        except Exception as ex:
            self._saved_note = f"Сохранённые значения повреждены: {ex}"

    def on_save_defaults(self):
        try:
            errs = self._validate_all_groups()
            if errs:
                messagebox.showerror("Нельзя сохранить",
                                     "\n".join("• " + e for e in errs))
                return
            self._sync_all()
            if self.tv:
                self.cache[self.test_name.get()] = self._read_test(silent=True) or {}
            data = {
                "printer_name": self.printer.name,
                "material_name": self.material.name,
                "test_name": self.test_name.get(),
                "printer": asdict(self.printer),
                "material": asdict(self.material),
                "process": asdict(self.process),
                "tests": self.cache,
            }
            save_defaults(data)
            self.printers[self.printer.name] = replace(self.printer)
            self.materials[self.material.name] = replace(self.material)
            self.status.set(f"Сохранено: {DEFAULTS_FILE}")
            messagebox.showinfo("Сохранено",
                                "Текущие значения станут значениями по умолчанию "
                                f"при следующем запуске.\n\n{DEFAULTS_FILE}")
        except Exception as ex:
            messagebox.showerror("Ошибка сохранения", str(ex))

    def on_factory_reset(self):
        if not messagebox.askyesno("Заводские значения",
                                   "Удалить сохранённые значения и вернуть пресеты?"):
            return
        clear_defaults()
        self.printers = {k: replace(v) for k, v in PRINTERS.items()}
        self.materials = {k: replace(v) for k, v in MATERIALS.items()}
        self.printer = replace(next(iter(self.printers.values())))
        self.material = replace(self.materials["PLA"])
        self.process = replace(ProcessProfile())
        self.cache.clear()
        self.printer_cb["values"] = list(self.printers)
        self.material_cb["values"] = list(self.materials)
        self.printer_name.set(self.printer.name)
        self.material_name.set(self.material.name)
        self._load_printer()
        self._load_material()
        self._load_process()
        self._rebuild_test()
        self.status.set("Возвращены заводские пресеты")

    def _grid(self, parent, spec, store, on_change=None):
        for i, (key, label, kind, vmin, vmax) in enumerate(spec):
            r, c = divmod(i, 2)
            ttk.Label(parent, text=label).grid(row=r, column=c*2, sticky="w",
                                               padx=(6, 4), pady=3)
            if kind == "b":
                var = tk.BooleanVar()
                store[key] = var
                ttk.Checkbutton(parent, variable=var,
                                command=on_change).grid(row=r, column=c*2+1,
                                                        sticky="w", pady=3)
            else:
                var = tk.StringVar()
                store[key] = var
                e = ttk.Entry(parent, textvariable=var, width=12)
                e.grid(row=r, column=c*2+1, sticky="w", padx=(0, 18), pady=3)
                self.entries[f"{id(store)}:{key}"] = e
                if on_change:
                    var.trace_add("write", lambda *a: on_change())

    def _build(self):
        st = ttk.Style()
        st.configure("H.TLabel", font=("Segoe UI", 10, "bold"))
        st.configure("Calc.TLabel", foreground="#0a6")
        st.configure("Warn.TLabel", foreground="#b00")
        st.configure("Bad.TEntry", fieldbackground="#ffd9d9")

        top = ttk.LabelFrame(self.root, text="Профили", padding=8)
        top.pack(fill="x", padx=6, pady=(6, 3))
        row = ttk.Frame(top); row.pack(fill="x")
        ttk.Label(row, text="Принтер:", style="H.TLabel").pack(side="left")
        self.printer_name = tk.StringVar(value=self.printer.name)
        self.printer_cb = ttk.Combobox(row, textvariable=self.printer_name,
                                       values=list(self.printers),
                                       state="readonly", width=28)
        self.printer_cb.pack(side="left", padx=(6, 18))
        self.printer_cb.bind("<<ComboboxSelected>>", self.on_printer)

        ttk.Label(row, text="Материал:", style="H.TLabel").pack(side="left")
        self.material_name = tk.StringVar(value=self.material.name)
        self.material_cb = ttk.Combobox(row, textvariable=self.material_name,
                                        values=list(self.materials),
                                        state="readonly", width=14)
        self.material_cb.pack(side="left", padx=6)
        self.material_cb.bind("<<ComboboxSelected>>", self.on_material)

        ttk.Button(row, text="Заводские",
                   command=self.on_factory_reset).pack(side="right")
        ttk.Button(row, text="Сохранить как мои значения",
                   command=self.on_save_defaults).pack(side="right", padx=6)

        self.info = ttk.Label(top, text="", foreground="#555", wraplength=950)
        self.info.pack(anchor="w", pady=(6, 0))

        pw = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        pw.pack(fill="both", expand=True, padx=6, pady=3)
        nbw = ttk.Frame(pw); pw.add(nbw, weight=3)
        self.nb = ttk.Notebook(nbw); self.nb.pack(fill="both", expand=True)

        # Тест
        t = ttk.Frame(self.nb, padding=8); self.nb.add(t, text="Тест")
        tr = ttk.Frame(t); tr.pack(fill="x")
        ttk.Label(tr, text="Тип теста:", style="H.TLabel").pack(side="left")
        default_test = self._pending_test
        if default_test not in GENERATORS:
            default_test = next(iter(GENERATORS))
        self.test_name = tk.StringVar(value=default_test)
        cb = ttk.Combobox(tr, textvariable=self.test_name,
                          values=list(GENERATORS), state="readonly", width=26)
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self._rebuild_test(preserve=True))
        self.test_info = ttk.Label(t, text="", foreground="#0a6", wraplength=940)
        self.test_info.pack(anchor="w", pady=(6, 4))
        self.test_box = ttk.LabelFrame(t, text="Параметры", padding=6)
        self.test_box.pack(fill="both", expand=True)

        # Принтер
        pf = ttk.Frame(self.nb, padding=8); self.nb.add(pf, text="Принтер")
        ttk.Label(pf, text="Стол задаётся двумя противоположными углами A и B. "
                  "Ноль может быть где угодно.", foreground="#555",
                  wraplength=920).grid(row=0, column=0, columnspan=4,
                                       sticky="w", pady=(0, 6))
        self._grid_at(pf, PRINTER_SPEC, self.pv, self._live, row0=1)
        nr = (len(PRINTER_SPEC) + 1) // 2 + 1
        self.bed_info = ttk.Label(pf, text="", style="Calc.TLabel", wraplength=920)
        self.bed_info.grid(row=nr, column=0, columnspan=4, sticky="w", pady=(10, 0))
        self.bed_err = ttk.Label(pf, text="", style="Warn.TLabel", wraplength=920)
        self.bed_err.grid(row=nr+1, column=0, columnspan=4, sticky="w")

        # Материал
        mf = ttk.Frame(self.nb, padding=8); self.nb.add(mf, text="Материал")
        self._grid(mf, MATERIAL_SPEC, self.mv, self._live)

        # Процесс
        cf = ttk.Frame(self.nb, padding=8); self.nb.add(cf, text="Процесс")
        self._grid(cf, PROCESS_SPEC, self.cv, self._live)
        nr = (len(PROCESS_SPEC) + 1) // 2
        self.calc = ttk.LabelFrame(cf, text="Производные", padding=6)
        self.calc.grid(row=nr, column=0, columnspan=4, sticky="w", pady=(12, 0))
        self.calc_lw = ttk.Label(self.calc, style="Calc.TLabel", text="")
        self.calc_lw.grid(row=0, column=0, sticky="w", padx=6)
        self.calc_flw = ttk.Label(self.calc, style="Calc.TLabel", text="")
        self.calc_flw.grid(row=0, column=1, sticky="w", padx=18)

        # G-code принтера
        gf = ttk.Frame(self.nb, padding=6); self.nb.add(gf, text="G-code принтера")
        ttk.Label(gf, foreground="#555", wraplength=920,
                  text="Плейсхолдеры: {nozzle_temp} {bed_temp} {park_x} {park_y} "
                       "{x_min} {x_max} {y_min} {y_max} {center_x} {center_y} "
                       "{bed_x} {bed_y} {max_z}").pack(anchor="w")
        sp = ttk.PanedWindow(gf, orient=tk.HORIZONTAL)
        sp.pack(fill="both", expand=True, pady=4)
        s1 = ttk.LabelFrame(sp, text="START", padding=3)
        s2 = ttk.LabelFrame(sp, text="END", padding=3)
        sp.add(s1, weight=1); sp.add(s2, weight=1)
        self.start_txt = tk.Text(s1, wrap="none", font=("Consolas", 9), height=12)
        self.start_txt.pack(fill="both", expand=True)
        self.end_txt = tk.Text(s2, wrap="none", font=("Consolas", 9), height=12)
        self.end_txt.pack(fill="both", expand=True)

        # низ
        bot = ttk.Frame(pw); pw.add(bot, weight=2)
        br = ttk.Frame(bot); br.pack(fill="x", pady=(0, 4))
        ttk.Button(br, text="Сгенерировать", command=self.on_generate).pack(side="left")
        ttk.Button(br, text="Сохранить .3mf", command=self.on_save_3mf).pack(side="left", padx=4)
        ttk.Button(br, text="Сохранить .gcode", command=self.on_save_gcode).pack(side="left")
        ttk.Button(br, text="Аудит", command=self.on_audit).pack(side="left", padx=4)
        ttk.Button(br, text="Копировать", command=self.on_copy).pack(side="right")

        pv = ttk.LabelFrame(bot, text="G-code", padding=3)
        pv.pack(fill="both", expand=True)
        self.out = tk.Text(pv, wrap="none", font=("Consolas", 9),
                           bg="#1e1e1e", fg="#d4d4d4", insertbackground="#fff")
        sy = ttk.Scrollbar(pv, orient="vertical", command=self.out.yview)
        sx = ttk.Scrollbar(pv, orient="horizontal", command=self.out.xview)
        self.out.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        self.out.grid(row=0, column=0, sticky="nsew")
        sy.grid(row=0, column=1, sticky="ns"); sx.grid(row=1, column=0, sticky="ew")
        pv.rowconfigure(0, weight=1); pv.columnconfigure(0, weight=1)

        self.status = tk.StringVar(value="Готов")
        ttk.Label(self.root, textvariable=self.status, relief="sunken",
                  anchor="w", padding=3).pack(fill="x", side="bottom")

    def _grid_at(self, parent, spec, store, on_change, row0):
        for i, (key, label, kind, vmin, vmax) in enumerate(spec):
            r, c = divmod(i, 2)
            ttk.Label(parent, text=label).grid(row=row0+r, column=c*2, sticky="w",
                                               padx=(6, 4), pady=3)
            var = tk.StringVar()
            store[key] = var
            e = ttk.Entry(parent, textvariable=var, width=12)
            e.grid(row=row0+r, column=c*2+1, sticky="w", padx=(0, 18), pady=3)
            self.entries[f"{id(store)}:{key}"] = e
            if on_change:
                var.trace_add("write", lambda *a: on_change())

    def _fill(self, store, spec, obj):
        for key, _l, kind, _a, _b in spec:
            v = getattr(obj, key)
            if kind == "b":
                store[key].set(bool(v))
            else:
                store[key].set(f"{v:g}" if isinstance(v, float) else str(v))

    def _load_printer(self):
        self._loading = True
        self._fill(self.pv, PRINTER_SPEC, self.printer)
        self.start_txt.delete("1.0", "end")
        self.start_txt.insert("1.0", self.printer.start_gcode)
        self.end_txt.delete("1.0", "end")
        self.end_txt.insert("1.0", self.printer.end_gcode)
        self._loading = False
        self._refresh_all()

    def _load_material(self):
        self._loading = True
        self._fill(self.mv, MATERIAL_SPEC, self.material)
        self._loading = False
        self._refresh_all()

    def _load_process(self):
        self._loading = True
        self._fill(self.cv, PROCESS_SPEC, self.process)
        self._loading = False
        self._refresh_all()

    def _read_group(self, store, spec, title):
        vals, errs = {}, []
        for key, label, kind, vmin, vmax in spec:
            var = store.get(key)
            if var is None:
                continue
            ek = f"{id(store)}:{key}"
            if kind == "b":
                vals[key] = bool(var.get())
                continue
            raw = str(var.get()).strip().replace(",", ".")
            try:
                v = float(raw)
                if kind == "i":
                    v = int(round(v))
                if not math.isfinite(v):
                    raise ValueError
            except (ValueError, tk.TclError):
                errs.append(f"{title}: «{label}» — не число")
                self._mark(ek, True)
                continue
            if v < vmin or v > vmax:
                errs.append(f"{title}: «{label}» = {v:g} вне [{vmin:g}, {vmax:g}]")
                self._mark(ek, True)
                continue
            self._mark(ek, False)
            vals[key] = v
        return vals, errs

    def _mark(self, entry_key, bad):
        e = self.entries.get(entry_key)
        if e is None:
            return
        try:
            e.configure(style="Bad.TEntry" if bad else "TEntry")
        except Exception:
            pass

    def _validate_all_groups(self):
        errs = []
        for store, spec, title in ((self.pv, PRINTER_SPEC, "Принтер"),
                                   (self.mv, MATERIAL_SPEC, "Материал"),
                                   (self.cv, PROCESS_SPEC, "Процесс")):
            _, e = self._read_group(store, spec, title)
            errs += e
        return errs

    def _sync_all(self):
        for store, spec, obj in ((self.pv, PRINTER_SPEC, self.printer),
                                 (self.mv, MATERIAL_SPEC, self.material),
                                 (self.cv, PROCESS_SPEC, self.process)):
            vals, _ = self._read_group(store, spec, "")
            for k, v in vals.items():
                setattr(obj, k, v)
        self.printer.start_gcode = self.start_txt.get("1.0", "end-1c")
        self.printer.end_gcode = self.end_txt.get("1.0", "end-1c")

    def _live(self):
        if self._loading:
            return
        try:
            self._sync_all()
            self._refresh_all()
        except Exception:
            pass

    def _refresh_all(self):
        self._refresh_info()
        self._refresh_bed()
        self._refresh_calc()
        self._refresh_test_info()

    def _refresh_info(self):
        p, m, c = self.printer, self.material, self.process
        lw = p.nozzle_diameter * c.line_width_factor
        self.info.config(text=(
            f"{p.name}: X[{p.x_min:g},{p.x_max:g}] Y[{p.y_min:g},{p.y_max:g}] "
            f"Z≤{p.max_z:g}, центр ({p.center[0]:g},{p.center[1]:g})  |  "
            f"{m.name}: {m.nozzle_temp}/{m.bed_temp} °C, обдув {m.fan_speed}, "
            f"{m.print_speed:g} мм/с  |  сопло {p.nozzle_diameter:g}, "
            f"слой {c.layer_height:g}, линия {lw:.3f}"))

    def _refresh_bed(self):
        p = self.printer
        ux0, uy0, ux1, uy1 = p.usable(self.process.bed_margin)
        self.bed_info.config(text=(
            f"Размер {p.size_x:.1f} × {p.size_y:.1f} мм   |   "
            f"центр ({p.center[0]:.2f}, {p.center[1]:.2f})   |   "
            f"рабочая зона X[{ux0:.1f},{ux1:.1f}] Y[{uy0:.1f},{uy1:.1f}]"))
        errs = p.geometry_errors()
        self.bed_err.config(text="\n".join("• " + e for e in errs) if errs else "")

    def _refresh_calc(self):
        c = self.process
        lw = self.printer.nozzle_diameter * c.line_width_factor
        flw = self.printer.nozzle_diameter * c.first_layer_width_factor
        self.calc_lw.config(text=f"Ширина линии: {lw:.3f} мм")
        self.calc_flw.config(text=f"Ширина 1-го слоя: {flw:.3f} мм")

    def _refresh_test_info(self):
        try:
            gen = self._make_gen()
            if gen is None:
                return
            p = self._read_test(silent=True)
            if p:
                self.test_info.config(text=gen.info_line(p))
        except Exception:
            pass

    def on_printer(self, _=None):
        try:
            name = self.printer_name.get()
            self.printer = replace(self.printers[name])
            self._load_printer()
            self._rebuild_test(preserve=True)
            self.status.set(f"Принтер: {name} — поля обновлены")
        except Exception as ex:
            messagebox.showerror("Ошибка", str(ex))

    def on_material(self, _=None):
        try:
            name = self.material_name.get()
            self.material = replace(self.materials[name])
            self._load_material()
            self._rebuild_test(preserve=True, refresh_temps=True)
            self.status.set(f"Материал: {name} — поля обновлены")
        except Exception as ex:
            messagebox.showerror("Ошибка", str(ex))

    def _make_gen(self):
        cls = GENERATORS.get(self.test_name.get())
        return cls(self.printer, self.material, self.process) if cls else None

    def _rebuild_test(self, preserve=False, refresh_temps=False):
        if preserve and self.tv:
            self.cache[self.test_name.get()] = self._read_test(silent=True) or {}
        for wdg in self.test_box.winfo_children():
            wdg.destroy()
        self.tv.clear()
        gen = self._make_gen()
        if gen is None:
            return
        params = gen.parameters()
        cached = self.cache.get(self.test_name.get(), {})
        temp_keys = {"temp_start", "temp_end", "nozzle_temp", "bed_temp"}
        for i, (key, meta) in enumerate(params.items()):
            r, c = divmod(i, 2)
            label = meta["label"] + (f", {meta['unit']}" if meta.get("unit") else "")
            ttk.Label(self.test_box, text=label).grid(row=r, column=c*3,
                                                      sticky="w", padx=(6, 4), pady=3)
            if meta["type"] == "bool":
                var = tk.BooleanVar(value=bool(meta["default"]))
                if preserve and key in cached:
                    var.set(bool(cached[key]))
                ttk.Checkbutton(self.test_box, variable=var,
                                command=self._refresh_test_info).grid(
                    row=r, column=c*3+1, sticky="w")
            else:
                var = tk.StringVar()
                val = meta["default"]
                if preserve and key in cached and not (refresh_temps and key in temp_keys):
                    val = cached[key]
                var.set(f"{val:g}" if isinstance(val, float) else str(val))
                e = ttk.Entry(self.test_box, textvariable=var, width=11)
                e.grid(row=r, column=c*3+1, sticky="w", pady=3)
                self.entries[f"test:{key}"] = e
                var.trace_add("write", lambda *a: self._refresh_test_info())
            hint = (f"{meta['min']}…{meta['max']}"
                    if "min" in meta and "max" in meta else "")
            ttk.Label(self.test_box, text=hint, foreground="#999").grid(
                row=r, column=c*3+2, sticky="w", padx=(4, 16))
            self.tv[key] = var
        self._refresh_test_info()

    def _read_test(self, silent=False):
        gen = self._make_gen()
        if gen is None:
            return None
        out, errs = {}, []
        for key, meta in gen.parameters().items():
            var = self.tv.get(key)
            ek = f"test:{key}"
            if var is None:
                out[key] = meta["default"]
                continue
            try:
                if meta["type"] == "bool":
                    out[key] = int(bool(var.get()))
                    continue
                v = float(str(var.get()).strip().replace(",", "."))
                if not math.isfinite(v):
                    raise ValueError
                if meta["type"] == "int":
                    v = int(round(v))
            except (ValueError, tk.TclError):
                self._mark(ek, True)
                if silent:
                    out[key] = meta["default"]
                else:
                    errs.append(f"«{meta['label']}» — не число")
                continue
            lo, hi = meta.get("min"), meta.get("max")
            if lo is not None and hi is not None and not (lo <= v <= hi):
                self._mark(ek, True)
                if silent:
                    out[key] = meta["default"]
                else:
                    errs.append(f"«{meta['label']}» = {v:g} вне [{lo:g}, {hi:g}]")
                continue
            self._mark(ek, False)
            out[key] = v
        if errs and not silent:
            raise ValueError("\n".join("• " + e for e in errs))
        return out

    def on_generate(self):
        try:
            errs = self._validate_all_groups()
            if errs:
                messagebox.showerror("Проверка профилей",
                                     "\n".join("• " + e for e in errs))
                return
            self._sync_all()
            gen = self._make_gen()
            if gen is None:
                raise ValueError("Генератор не выбран")
            p = self._read_test()
            verr = gen.validate(p)
            if verr:
                messagebox.showerror("Проверка параметров",
                                     "\n".join("• " + e for e in verr))
                return
            text = gen.generate(p)
            aerr, awarn = audit_gcode(text, self.printer)
            if aerr:
                self.out.delete("1.0", "end")
                self.out.insert("1.0", text)
                self.gcode = None
                messagebox.showerror(
                    "Аудит G-code: ОПАСНО",
                    "Файл не сохранится. Найдены нарушения:\n\n" +
                    "\n".join("• " + e for e in aerr[:25]) +
                    ("\n…" if len(aerr) > 25 else ""))
                self.status.set(f"Аудит: {len(aerr)} ошибок")
                return
            est = estimate_time(text, self.printer, self.material)
            text = annotate_gcode(text, est)
            self.gcode = text
            self.last_est = est
            self.last_params, self.last_test = p, self.test_name.get()
            self.last_bounds = gen.bounds(p)
            self.out.delete("1.0", "end")
            self.out.insert("1.0", text)
            msg = (f"{self.last_test}: {format_duration(est['total'])}, "
                   f"{est['filament_g']:.1f} g, аудит пройден")
            if awarn:
                msg += " | " + "; ".join(awarn)
            self.status.set(msg)
        except GCodeError as ex:
            self.gcode = None
            messagebox.showerror("Нарушение границ", str(ex))
        except Exception as ex:
            self.gcode = None
            messagebox.showerror("Ошибка", str(ex))

    def on_audit(self):
        txt = self.out.get("1.0", "end-1c")
        if not txt.strip():
            messagebox.showinfo("Нет данных", "Сначала сгенерируйте G-code")
            return
        self._sync_all()
        e, w = audit_gcode(txt, self.printer)
        if not e and not w:
            messagebox.showinfo("Аудит", "Нарушений не найдено")
        else:
            messagebox.showwarning("Аудит",
                                   ("ОШИБКИ:\n" + "\n".join("• " + x for x in e[:25])
                                    if e else "") +
                                   ("\n\nПРЕДУПРЕЖДЕНИЯ:\n" +
                                    "\n".join("• " + x for x in w) if w else ""))

    def on_save_3mf(self):
        if not self.gcode:
            messagebox.showinfo("Нет данных", "Сначала сгенерируйте G-code")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".3mf",
            initialfile=f"FastTest_{self.printer.name.replace(' ','_')}.3mf",
            filetypes=[("3MF проект", "*.3mf"), ("Все файлы", "*.*")])
        if not path:
            return
        try:
            ThreeMFBuilder().build(path, self.gcode, self.printer, self.material,
                                   self.process, self.last_test,
                                   self.last_params, self.last_bounds,
                                   est=self.last_est)
            self.status.set(f"Сохранено: {path}")
        except Exception as ex:
            messagebox.showerror("Ошибка сохранения", str(ex))

    def on_save_gcode(self):
        if not self.gcode:
            messagebox.showinfo("Нет данных", "Сначала сгенерируйте G-code")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".gcode",
            initialfile=f"FastTest_{self.printer.name.replace(' ','_')}.gcode",
            filetypes=[("G-code", "*.gcode"), ("Все файлы", "*.*")])
        if not path:
            return
        try:
            Path(path).write_text(self.gcode, encoding="utf-8", newline="\n")
            self.status.set(f"Сохранено: {path}")
        except Exception as ex:
            messagebox.showerror("Ошибка сохранения", str(ex))

    def on_copy(self):
        if self.gcode:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.gcode)
            self.status.set("G-code в буфере обмена")


def main():
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()