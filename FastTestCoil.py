"""
===============================================================================
FastTestCoil Pro v3.0 — модульная система калибровки 3D-принтеров
===============================================================================
Слои архитектуры (строгая однонаправленная зависимость):

    Profiles      — данные: принтер / материал / процесс (dataclasses)
    GCodeWriter   — НИЗКИЙ уровень: только линейные перемещения и параметры.
                    Владеет режимом E, границами стола, статистикой.
    TestBase      — СРЕДНИЙ уровень: линии, змейки, безопасные переезды.
    *Test         — ВЫСОКИЙ уровень: алгоритмы тестов.
    ThreeMFBuilder— упаковка: реальная геометрия + настройки + G-code.
    App           — GUI.

Ключевые инварианты безопасности:
  * E всегда ОТНОСИТЕЛЬНЫЙ (M83). В строке пишется приращение, не накопление.
  * Любой выход за габариты стола/оси Z -> исключение, а не «молчаливый» код.
  * Температуры ограничены паспортными максимумами принтера.
  * Переезд вниз всегда через подъём над максимальной напечатанной высотой.
  * Первый слой считается по first_layer_height / first_layer_line_width.
===============================================================================
"""

from __future__ import annotations

import copy
import json
import math
import time
import zipfile
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# =============================================================================
# 1. ПРОФИЛИ
# =============================================================================

@dataclass
class PrinterProfile:
    """Паспорт принтера: габариты, лимиты, прошивка, пресеты старта/финиша."""
    name: str
    bed_x: float
    bed_y: float
    max_z: float
    nozzle_diameter: float = 0.4
    max_nozzle_temp: int = 280
    max_bed_temp: int = 110
    max_speed: float = 300.0          # мм/с, паспортный предел
    max_accel: float = 10000.0        # мм/с²
    firmware: str = "Marlin"
    kinematics: str = "corexy"        # corexy | bedslinger
    park_x: float = 5.0
    park_y: float = 5.0
    # Плейсхолдеры: {bed_temp} {nozzle_temp} {park_x} {park_y} {bed_y}
    start_gcode: str = ""
    end_gcode: str = ""
    notes: str = ""


@dataclass
class MaterialProfile:
    """Паспорт филамента. Все числа — рабочие, ненулевые."""
    name: str
    nozzle_temp: int
    bed_temp: int
    fan_speed: int                    # 0..255
    fan_first_layers: int = 0         # сколько первых слоёв без обдува
    print_speed: float = 50.0         # мм/с
    first_layer_speed: float = 20.0   # мм/с
    retract_length: float = 0.8       # мм
    retract_speed: float = 35.0       # мм/с
    deretract_speed: float = 35.0     # мм/с
    filament_diameter: float = 1.75   # мм
    density: float = 1.24             # г/см³
    min_temp: int = 180
    max_temp: int = 230
    temp_test_start: int = 190
    temp_test_end: int = 230
    flow_ratio: float = 1.0           # базовый множитель потока материала


@dataclass
class ProcessProfile:
    """
    Параметры процесса. Ширина линии НЕ задаётся напрямую —
    она вычисляется из диаметра сопла и множителя (как в OrcaSlicer).
    """
    nozzle_diameter: float = 0.4
    line_width_factor: float = 1.05            # 0.4 -> 0.42 мм
    first_layer_line_width_factor: float = 1.25  # 0.4 -> 0.50 мм
    layer_height: float = 0.20
    first_layer_height: float = 0.20
    travel_speed: float = 150.0        # мм/с
    z_hop: float = 0.40                # мм, подъём при переезде
    prime_length: float = 40.0         # мм, длина прайм-линии
    min_safe_z: float = 0.05           # мм, ниже — риск царапания стола

    @property
    def line_width(self) -> float:
        return max(0.05, self.nozzle_diameter * self.line_width_factor)

    @property
    def first_layer_line_width(self) -> float:
        return max(0.05, self.nozzle_diameter * self.first_layer_line_width_factor)

    @property
    def max_layer_height(self) -> float:
        """Физический предел высоты слоя для сопла (эмпирика 0.75·D)."""
        return 0.75 * self.nozzle_diameter


# --- Пресеты старта/финиша ---------------------------------------------------

_START_COREXY = (
    "; --- START (CoreXY / Marlin-compatible) ---\n"
    "M107\n"
    "G21\n"
    "G90\n"
    "M83\n"
    "M140 S{bed_temp}\n"
    "G28\n"
    "M190 S{bed_temp}\n"
    "M109 S{nozzle_temp}\n"
    "G92 E0\n"
)

_START_BEDSLINGER = (
    "; --- START (bed-slinger / Marlin) ---\n"
    "M107\n"
    "G21\n"
    "G90\n"
    "M83\n"
    "M140 S{bed_temp}\n"
    "G28\n"
    "M190 S{bed_temp}\n"
    "M109 S{nozzle_temp}\n"
    "G92 E0\n"
)

_END_COMMON = (
    "; --- END ---\n"
    "M107\n"
    "G91\n"
    "G1 E-2.0 F2100\n"
    "G1 Z5 F1200\n"
    "G90\n"
    "G0 X{park_x} Y{park_y} F6000\n"
    "M104 S0\n"
    "M140 S0\n"
    "M84\n"
)

PRINTERS: Dict[str, PrinterProfile] = {
    "Flashforge Adventurer 5M": PrinterProfile(
        name="Flashforge Adventurer 5M",
        bed_x=220, bed_y=220, max_z=220,
        nozzle_diameter=0.4,
        max_nozzle_temp=280, max_bed_temp=110,
        max_speed=600, max_accel=20000,
        firmware="Klipper (Marlin-compatible)", kinematics="corexy",
        park_x=5, park_y=5,
        start_gcode=_START_COREXY, end_gcode=_END_COMMON,
        notes="Закрытый корпус не штатный; ABS/ASA — с оговорками.",
    ),
    "Flashforge Adventurer 5M Pro": PrinterProfile(
        name="Flashforge Adventurer 5M Pro",
        bed_x=220, bed_y=220, max_z=220,
        nozzle_diameter=0.4,
        max_nozzle_temp=280, max_bed_temp=110,
        max_speed=600, max_accel=20000,
        firmware="Klipper (Marlin-compatible)", kinematics="corexy",
        park_x=5, park_y=5,
        start_gcode=_START_COREXY, end_gcode=_END_COMMON,
        notes="Закрытый корпус + фильтрация, пригоден для ABS/ASA.",
    ),
    "Flashforge AD5X": PrinterProfile(
        name="Flashforge AD5X",
        bed_x=220, bed_y=220, max_z=220,
        nozzle_diameter=0.4,
        max_nozzle_temp=300, max_bed_temp=110,
        max_speed=600, max_accel=20000,
        firmware="Klipper (Marlin-compatible)", kinematics="corexy",
        park_x=5, park_y=5,
        start_gcode=_START_COREXY, end_gcode=_END_COMMON,
        notes="Мультифиламент (IFS). Тест печатается одним экструдером T0.",
    ),
    "FlyingBear Reborn 2": PrinterProfile(
        name="FlyingBear Reborn 2",
        bed_x=255, bed_y=210, max_z=220,
        nozzle_diameter=0.4,
        max_nozzle_temp=260, max_bed_temp=110,
        max_speed=150, max_accel=3000,
        firmware="Marlin", kinematics="bedslinger",
        park_x=5, park_y=205,
        start_gcode=_START_BEDSLINGER, end_gcode=_END_COMMON,
        notes="Хотэнд до 260 °C: PC/нейлон выше 260 недоступны.",
    ),
}

MATERIALS: Dict[str, MaterialProfile] = {
    "PLA": MaterialProfile(
        "PLA", nozzle_temp=210, bed_temp=60, fan_speed=255, fan_first_layers=1,
        print_speed=60, first_layer_speed=20, retract_length=0.8,
        retract_speed=35, deretract_speed=35, density=1.24,
        min_temp=180, max_temp=230, temp_test_start=190, temp_test_end=230),
    "PETG": MaterialProfile(
        "PETG", nozzle_temp=240, bed_temp=80, fan_speed=110, fan_first_layers=3,
        print_speed=45, first_layer_speed=18, retract_length=1.2,
        retract_speed=30, deretract_speed=30, density=1.27,
        min_temp=220, max_temp=260, temp_test_start=225, temp_test_end=260),
    "ABS": MaterialProfile(
        "ABS", nozzle_temp=250, bed_temp=100, fan_speed=25, fan_first_layers=4,
        print_speed=50, first_layer_speed=20, retract_length=1.0,
        retract_speed=35, deretract_speed=35, density=1.04,
        min_temp=230, max_temp=270, temp_test_start=235, temp_test_end=270),
    "ASA": MaterialProfile(
        "ASA", nozzle_temp=255, bed_temp=100, fan_speed=30, fan_first_layers=4,
        print_speed=50, first_layer_speed=18, retract_length=1.0,
        retract_speed=35, deretract_speed=35, density=1.07,
        min_temp=235, max_temp=275, temp_test_start=240, temp_test_end=275),
    "Nylon (PA)": MaterialProfile(
        "Nylon (PA)", nozzle_temp=255, bed_temp=80, fan_speed=25, fan_first_layers=5,
        print_speed=35, first_layer_speed=15, retract_length=1.5,
        retract_speed=30, deretract_speed=30, density=1.14,
        min_temp=235, max_temp=260, temp_test_start=235, temp_test_end=260),
    "PC": MaterialProfile(
        "PC", nozzle_temp=280, bed_temp=110, fan_speed=0, fan_first_layers=6,
        print_speed=35, first_layer_speed=15, retract_length=1.2,
        retract_speed=30, deretract_speed=30, density=1.20,
        min_temp=260, max_temp=300, temp_test_start=265, temp_test_end=300),
    "TPU 95A": MaterialProfile(
        "TPU 95A", nozzle_temp=230, bed_temp=50, fan_speed=150, fan_first_layers=2,
        print_speed=25, first_layer_speed=15, retract_length=0.6,
        retract_speed=25, deretract_speed=25, density=1.21,
        min_temp=210, max_temp=250, temp_test_start=215, temp_test_end=245),
    "TPU 85A": MaterialProfile(
        "TPU 85A", nozzle_temp=225, bed_temp=50, fan_speed=140, fan_first_layers=2,
        print_speed=20, first_layer_speed=12, retract_length=0.5,
        retract_speed=20, deretract_speed=20, density=1.20,
        min_temp=205, max_temp=245, temp_test_start=210, temp_test_end=240),
    "TPU 60A": MaterialProfile(
        "TPU 60A", nozzle_temp=220, bed_temp=45, fan_speed=128, fan_first_layers=2,
        print_speed=15, first_layer_speed=10, retract_length=0.4,
        retract_speed=15, deretract_speed=15, density=1.18,
        min_temp=200, max_temp=240, temp_test_start=205, temp_test_end=235),
}


# =============================================================================
# 2. НИЗКОУРОВНЕВЫЙ WRITER
# =============================================================================

class GCodeError(Exception):
    """Нарушение границ/режимов — генерация прерывается."""


class GCodeWriter:
    """
    Только примитивы. Никакой геометрии тестов.

    Гарантии:
      * режим E — относительный (M83), в строку идёт приращение;
      * координаты проверяются по габаритам принтера;
      * подачи ограничены max_speed;
      * ведётся статистика: длина филамента, время, максимум Z.
    """

    def __init__(self, printer: PrinterProfile):
        self.p = printer
        self.lines: List[str] = []
        self.x = self.y = self.z = 0.0
        self.feed = 1200.0            # мм/мин, последняя использованная
        self.max_print_z = 0.0        # максимальная высота ЭКСТРУЗИИ
        self.total_e = 0.0            # мм филамента
        self.time_s = 0.0             # оценка времени
        self._retracted = False

    # --- вывод ---------------------------------------------------------------
    def comment(self, text: str = "") -> None:
        for ln in str(text).split("\n"):
            self.lines.append(f"; {ln}" if ln else ";")

    def raw(self, text: str) -> None:
        for ln in text.strip().split("\n"):
            if ln.strip():
                self.lines.append(ln.rstrip())

    # --- проверки ------------------------------------------------------------
    def _check(self, x: float, y: float, z: Optional[float]) -> None:
        eps = 1e-6
        if not (-eps <= x <= self.p.bed_x + eps):
            raise GCodeError(f"X={x:.3f} вне стола 0..{self.p.bed_x}")
        if not (-eps <= y <= self.p.bed_y + eps):
            raise GCodeError(f"Y={y:.3f} вне стола 0..{self.p.bed_y}")
        if z is not None and not (-eps <= z <= self.p.max_z + eps):
            raise GCodeError(f"Z={z:.3f} вне диапазона 0..{self.p.max_z}")

    def _feed(self, f: Optional[float]) -> float:
        lim = self.p.max_speed * 60.0
        return max(60.0, min(f if f else self.feed, lim))

    def _tick(self, x: float, y: float, z: Optional[float], f: float) -> None:
        zz = self.z if z is None else z
        d = math.dist((self.x, self.y, self.z), (x, y, zz))
        self.time_s += d / (f / 60.0)
        self.x, self.y, self.z, self.feed = x, y, zz, f

    # --- перемещения ---------------------------------------------------------
    def travel(self, x: float, y: float, z: Optional[float] = None,
               f: Optional[float] = None) -> None:
        """G0 — холостое линейное перемещение."""
        self._check(x, y, z)
        f = self._feed(f)
        cmd = f"G0 X{x:.3f} Y{y:.3f}"
        if z is not None:
            cmd += f" Z{z:.3f}"
        self.lines.append(f"{cmd} F{f:.0f}")
        self._tick(x, y, z, f)

    def move_z(self, z: float, f: float = 900.0) -> None:
        """Только Z."""
        self._check(self.x, self.y, z)
        f = self._feed(f)
        self.lines.append(f"G0 Z{z:.3f} F{f:.0f}")
        self._tick(self.x, self.y, z, f)

    def print_move(self, x: float, y: float, z: float, e: float,
                   f: float, note: str = "") -> None:
        """
        G1 — рабочее линейное перемещение с экструзией.
        `e` — ПРИРАЩЕНИЕ филамента для этого отрезка (мм), не накопление.
        """
        self._check(x, y, z)
        if e < 0:
            raise GCodeError("Отрицательное E в рабочем перемещении")
        f = self._feed(f)
        cmd = f"G1 X{x:.3f} Y{y:.3f} Z{z:.3f} E{e:.5f} F{f:.0f}"
        if note:
            cmd += f" ; {note}"
        self.lines.append(cmd)
        self.total_e += e
        self.max_print_z = max(self.max_print_z, z)
        self._tick(x, y, z, f)

    # --- экструдер -----------------------------------------------------------
    def retract(self, length: float, speed: float) -> None:
        if self._retracted or length <= 0:
            return
        f = self._feed(speed * 60.0)
        self.lines.append(f"G1 E-{length:.3f} F{f:.0f}")
        self.time_s += length / (f / 60.0)
        self._retracted = True

    def deretract(self, length: float, speed: float) -> None:
        if not self._retracted or length <= 0:
            return
        f = self._feed(speed * 60.0)
        self.lines.append(f"G1 E{length:.3f} F{f:.0f}")
        self.time_s += length / (f / 60.0)
        self._retracted = False

    def reset_e(self) -> None:
        self.lines.append("G92 E0")

    # --- температуры / прочее ------------------------------------------------
    def nozzle(self, temp: int, wait: bool = False) -> None:
        t = int(round(temp))
        if t > self.p.max_nozzle_temp:
            raise GCodeError(f"Сопло {t} °C > лимита принтера {self.p.max_nozzle_temp} °C")
        self.lines.append(f"{'M109' if wait else 'M104'} S{t}")

    def bed(self, temp: int, wait: bool = False) -> None:
        t = int(round(temp))
        if t > self.p.max_bed_temp:
            raise GCodeError(f"Стол {t} °C > лимита принтера {self.p.max_bed_temp} °C")
        self.lines.append(f"{'M190' if wait else 'M140'} S{t}")

    def fan(self, pwm: int) -> None:
        pwm = max(0, min(255, int(pwm)))
        self.lines.append(f"M106 S{pwm}" if pwm else "M107")

    def dwell(self, ms: int, note: str = "") -> None:
        ms = max(0, int(ms))
        self.lines.append(f"G4 P{ms}" + (f" ; {note}" if note else ""))
        self.time_s += ms / 1000.0

    def home(self, axes: str = "") -> None:
        self.lines.append(f"G28 {axes}".strip())
        self.x = self.y = self.z = 0.0

    def modes(self) -> None:
        """Привести режимы к заведомо известным (после пресета)."""
        self.lines += ["G21", "G90", "M83"]

    def build(self) -> str:
        return "\n".join(self.lines) + "\n"


# =============================================================================
# 3. СРЕДНИЙ УРОВЕНЬ: БАЗА ТЕСТОВ
# =============================================================================

Box = Tuple[float, float, float, float, float, float]  # x,y,z,w,d,h


class TestBase(ABC):
    """Общие механизмы: старт/финиш, линии, змейки, безопасные переезды."""

    MARGIN = 12.0

    def __init__(self, printer: PrinterProfile, material: MaterialProfile,
                 process: ProcessProfile):
        self.p = printer
        self.m = material
        self.pr = process
        self.w = GCodeWriter(printer)
        self.boxes: List[Box] = []      # реальная геометрия для 3MF

    # --- обязательный интерфейс ---------------------------------------------
    @abstractmethod
    def key(self) -> str: ...
    @abstractmethod
    def title(self) -> str: ...
    @abstractmethod
    def params(self) -> Dict[str, Dict[str, Any]]: ...
    @abstractmethod
    def validate(self, p: Dict[str, Any]) -> List[str]: ...
    @abstractmethod
    def _run(self, p: Dict[str, Any]) -> None: ...

    # --- расчёты -------------------------------------------------------------
    def e_for(self, length: float, width: float, height: float,
              flow: float) -> float:
        """Объём дорожки -> длина филамента."""
        area = math.pi * (self.m.filament_diameter / 2.0) ** 2
        return (length * width * height * flow * self.m.flow_ratio) / area

    def z_of(self, layer: int) -> float:
        """Z для 1-based номера слоя с учётом высоты первого слоя."""
        if layer <= 1:
            return self.pr.first_layer_height
        return self.pr.first_layer_height + (layer - 1) * self.pr.layer_height

    def lh_of(self, layer: int) -> float:
        return self.pr.first_layer_height if layer == 1 else self.pr.layer_height

    def lw_of(self, layer: int) -> float:
        return (self.pr.first_layer_line_width if layer == 1
                else self.pr.line_width)

    def speed_of(self, layer: int) -> float:
        return self.m.first_layer_speed if layer == 1 else self.m.print_speed

    # --- переезды ------------------------------------------------------------
    def travel_safe(self, x: float, y: float, z: float,
                    long_move: bool = False) -> None:
        """
        Безопасный переезд.
          * вниз  — сначала подъём над max_print_z + z_hop, потом XY, потом спуск;
          * вверх — сначала подъём до целевой Z, потом XY;
          * короткий боковой шаг (< 2 мм на той же Z) — без ретракта.
        """
        w, pr, m = self.w, self.pr, self.m
        dist = math.hypot(x - w.x, y - w.y)
        same_z = abs(z - w.z) < 1e-6

        if same_z and dist < 2.0 and not long_move:
            w.travel(x, y, z, pr.travel_speed * 60.0)
            return

        w.retract(m.retract_length, m.retract_speed)
        if z < w.z - 1e-6 or long_move:
            safe = min(max(w.max_print_z, w.z) + pr.z_hop, self.p.max_z)
            if safe > w.z + 1e-6:
                w.move_z(safe)
            w.travel(x, y, None, pr.travel_speed * 60.0)
            w.move_z(z)
        else:
            if z > w.z + 1e-6:
                w.move_z(z)
            w.travel(x, y, None, pr.travel_speed * 60.0)
        w.deretract(m.retract_length, m.deretract_speed)

    # --- печать --------------------------------------------------------------
    def line(self, x0: float, y0: float, x1: float, y1: float, z: float,
             width: float, height: float, flow: float, speed: float,
             note: str = "") -> None:
        length = math.hypot(x1 - x0, y1 - y0)
        self.w.print_move(x1, y1, z, self.e_for(length, width, height, flow),
                          speed * 60.0, note)

    def snake(self, x0: float, y0: float, z: float, n: int, length: float,
              width: float, height: float, flow: float, speed: float,
              long_move: bool = False) -> None:
        """`n` параллельных дорожек вдоль Y, шаг по X = width, змейкой."""
        ya, yb = y0, y0 + length
        for i in range(n):
            x = x0 + i * width
            sy, ey = (ya, yb) if i % 2 == 0 else (yb, ya)
            self.travel_safe(x, sy, z, long_move=(long_move and i == 0))
            self.line(x, sy, x, ey, z, width, height, flow, speed)
        self.boxes.append((x0, y0, z - height, n * width, length, height))

    # --- старт / финиш -------------------------------------------------------
    def start(self, nozzle: int, bed: int, prime: bool = True) -> None:
        w = self.w
        w.comment("=" * 62)
        w.comment(f"{self.title()}")
        w.comment(f"Printer : {self.p.name}  ({self.p.bed_x}x{self.p.bed_y}x{self.p.max_z})")
        w.comment(f"Material: {self.m.name}   Nozzle {nozzle} C / Bed {bed} C")
        w.comment(f"Nozzle D={self.pr.nozzle_diameter}  LW={self.pr.line_width:.3f}"
                  f"  LH={self.pr.layer_height:.3f}  1st LH={self.pr.first_layer_height:.3f}")
        w.comment(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        w.comment("Extrusion mode: RELATIVE (M83)")
        w.comment("=" * 62)

        subs = dict(bed_temp=int(bed), nozzle_temp=int(nozzle),
                    park_x=self.p.park_x, park_y=self.p.park_y,
                    bed_y=self.p.bed_y)
        w.raw(self.p.start_gcode.format(**subs))
        w.modes()                       # гарантируем G21/G90/M83
        w.nozzle(nozzle, wait=True)     # контроль после пресета
        w.bed(bed, wait=True)
        w.fan(0)

        if prime and self.pr.prime_length > 0:
            L = min(self.pr.prime_length, self.p.bed_x - 2 * self.MARGIN)
            x0, y = self.MARGIN, 4.0
            z = self.pr.first_layer_height
            w.comment("Prime line")
            w.travel(x0, y, z + 2.0, self.pr.travel_speed * 60.0)
            w.move_z(z)
            self.line(x0, y, x0 + L, y, z, self.pr.first_layer_line_width,
                      self.pr.first_layer_height, 1.0, self.m.first_layer_speed)
            w.reset_e()
            w.move_z(z + 2.0)

    def finish(self) -> None:
        w = self.w
        w.comment("=" * 62)
        w.comment(f"Filament: {w.total_e:.1f} mm "
                  f"({w.total_e * math.pi * (self.m.filament_diameter/2)**2 / 1000 * self.m.density:.2f} g)")
        w.comment(f"Est. time: {w.time_s/60:.1f} min")
        w.comment("=" * 62)
        w.retract(self.m.retract_length, self.m.retract_speed)
        subs = dict(park_x=self.p.park_x, park_y=self.p.park_y,
                    bed_y=self.p.bed_y, bed_temp=0, nozzle_temp=0)
        w.raw(self.p.end_gcode.format(**subs))

    def apply_fan(self, layer: int) -> None:
        self.w.fan(0 if layer <= self.m.fan_first_layers else self.m.fan_speed)

    # --- публичный вход ------------------------------------------------------
    def generate(self, p: Dict[str, Any]) -> str:
        self._run(p)
        return self.w.build()


# =============================================================================
# 4. ТЕСТ ПОТОКА
# =============================================================================

class FlowTest(TestBase):
    """Сплошной брусок: градиент потока вдоль X, фиксирован по всем слоям."""

    def key(self) -> str: return "flow"
    def title(self) -> str: return "FLOW GRADIENT TEST"

    def params(self):
        return {
            "length_mm":  dict(label="Длина бруска (Y)", default=60.0, type="float",
                               min=20, max=200, unit="мм"),
            "width_mm":   dict(label="Ширина бруска (X)", default=30.0, type="float",
                               min=20, max=200, unit="мм"),
            "layers":     dict(label="Слоёв", default=5, type="int", min=1, max=60),
            "flow_start": dict(label="Поток от", default=0.85, type="float",
                               min=0.3, max=2.0),
            "flow_end":   dict(label="Поток до", default=1.15, type="float",
                               min=0.3, max=2.0),
            "nozzle_temp_override": dict(label="Сопло °C (0 = из профиля)", default=0,
                                         type="int", min=0, max=350, unit="°C"),
            "bed_temp_override":    dict(label="Стол °C (0 = из профиля)", default=0,
                                         type="int", min=0, max=150, unit="°C"),
        }

    def validate(self, p):
        e = []
        avail_x = self.p.bed_x - 2 * self.MARGIN
        avail_y = self.p.bed_y - 2 * self.MARGIN - 8
        if p["width_mm"] > avail_x:
            e.append(f"Ширина > доступных {avail_x:.0f} мм по X")
        if p["length_mm"] > avail_y:
            e.append(f"Длина > доступных {avail_y:.0f} мм по Y")
        if p["flow_start"] >= p["flow_end"]:
            e.append("Поток «от» должен быть меньше «до»")
        if p["layers"] * self.pr.layer_height > self.p.max_z - 10:
            e.append("Высота бруска превышает ось Z")
        t = p["nozzle_temp_override"]
        if t and not (self.m.min_temp <= t <= min(self.m.max_temp, self.p.max_nozzle_temp)):
            e.append(f"Сопло: допустимо {self.m.min_temp}.."
                     f"{min(self.m.max_temp, self.p.max_nozzle_temp)} °C")
        return e

    def _run(self, p):
        nozzle = p["nozzle_temp_override"] or self.m.nozzle_temp
        bed = p["bed_temp_override"] or self.m.bed_temp
        lw = self.pr.line_width
        n = max(2, int(round(p["width_mm"] / lw)))
        W, L, layers = n * lw, p["length_mm"], int(p["layers"])
        x0 = (self.p.bed_x - W) / 2
        y0 = (self.p.bed_y - L) / 2 + 4

        self.start(nozzle, bed)
        self.w.comment(f"Lines/layer={n}  step={lw:.3f}  flow {p['flow_start']}->{p['flow_end']}")

        for layer in range(1, layers + 1):
            z, h, spd = self.z_of(layer), self.lh_of(layer), self.speed_of(layer)
            self.apply_fan(layer)
            self.w.comment(f"--- LAYER {layer}/{layers}  Z={z:.3f} ---")
            ya, yb = y0, y0 + L
            for i in range(n):
                t = i / (n - 1)
                flow = p["flow_start"] + (p["flow_end"] - p["flow_start"]) * t
                x = x0 + i * lw
                sy, ey = (ya, yb) if i % 2 == 0 else (yb, ya)
                self.travel_safe(x, sy, z, long_move=(i == 0))
                self.line(x, sy, x, ey, z, self.lw_of(layer), h, flow, spd,
                          f"flow={flow:.3f}")
            self.boxes.append((x0, y0, z - h, W, L, h))
        self.finish()


# =============================================================================
# 5. ТЕМПЕРАТУРНЫЙ ТЕСТ — ЛЕСТНИЦА (по авторскому алгоритму)
# =============================================================================

class TemperatureStaircaseTest(TestBase):
    """
    Лестница-пирамидка. Группа = `n_lines` дорожек вдоль Y, ширина n·LW.

    Проход `s` (1-based) печатает группы g = s - j + 1 на слоях j = 1..min(H, s+1).
    Дополнительно в проходе 1 группа 0 получает базу на слое 1
    (отсюда «шесть линий на столе» при n = 3).

    Перед КАЖДЫМ проходом (т.е. перед новой тройкой на столе) сопло
    нагревается на ΔT = (T_end - T_start) / (passes - 1) с выдержкой.

    Потолок H: как только группа 0 достигла слоя H, выше не поднимаемся —
    дальнейшие проходы просто идут «от стола до H» (extra_steps).
    """

    def key(self) -> str: return "temperature"
    def title(self) -> str: return "TEMPERATURE STAIRCASE TEST"

    def params(self):
        n_def = 3
        ang = math.degrees(math.atan2(self.pr.layer_height,
                                      n_def * self.pr.line_width))
        return {
            "max_layers": dict(label="Высота пирамидки H", default=10, type="int",
                               min=2, max=60, unit="слоёв"),
            "n_lines_per_step": dict(label="Линий в ступеньке", default=n_def,
                                     type="int", min=1, max=20),
            "angle_deg": dict(label="Угол поверхности (инфо/ввод)", default=round(ang, 2),
                              type="float", min=1, max=80, unit="°"),
            "angle_drives_n": dict(label="Считать линии из угла", default=False,
                                   type="bool"),
            "extra_steps": dict(label="Доп. проходов до H", default=0, type="int",
                                min=0, max=40),
            "line_length_mm": dict(label="Длина дорожки (Y)", default=40.0,
                                   type="float", min=10, max=180, unit="мм"),
            "temp_start": dict(label="Температура от", default=self.m.temp_test_start,
                               type="int", min=140, max=350, unit="°C"),
            "temp_end": dict(label="Температура до", default=self.m.temp_test_end,
                             type="int", min=140, max=350, unit="°C"),
            "flow_override": dict(label="Поток (база оператора)", default=1.0,
                                  type="float", min=0.3, max=2.0),
            "heat_soak_s_per_deg": dict(label="Выдержка", default=0.30, type="float",
                                        min=0.0, max=5.0, unit="с/°C"),
            "n_prime_lines": dict(label="Подготовительных линий", default=6,
                                  type="int", min=0, max=30),
            "bed_temp_override": dict(label="Стол °C (0 = из профиля)", default=0,
                                      type="int", min=0, max=150, unit="°C"),
        }

    # --- геометрия -----------------------------------------------------------
    def resolve_n(self, p) -> int:
        if p.get("angle_drives_n"):
            a = math.radians(max(0.5, min(80.0, p["angle_deg"])))
            n = self.pr.layer_height / (self.pr.line_width * math.tan(a))
            return max(1, int(round(n)))
        return max(1, int(p["n_lines_per_step"]))

    def passes(self, p) -> int:
        return (int(p["max_layers"]) - 1) + int(p["extra_steps"])

    def validate(self, p):
        e = []
        H, n = int(p["max_layers"]), self.resolve_n(p)
        passes = self.passes(p)
        if passes < 1:
            e.append("Число проходов должно быть ≥ 1")
        if p["temp_start"] >= p["temp_end"]:
            e.append("Температура «от» должна быть меньше «до»")
        hi = min(self.m.max_temp, self.p.max_nozzle_temp)
        if p["temp_end"] > hi:
            e.append(f"Температура «до» {p['temp_end']} °C > предела {hi} °C "
                     f"({self.m.name} / {self.p.name})")
        if p["temp_start"] < self.m.min_temp - 15:
            e.append(f"Температура «от» слишком низкая для {self.m.name} "
                     f"(мин. {self.m.min_temp} °C)")

        gw = n * self.pr.line_width
        total_x = (passes + 1) * gw + int(p["n_prime_lines"]) * self.pr.line_width + 6
        if total_x + 2 * self.MARGIN > self.p.bed_x:
            e.append(f"Ширина теста {total_x:.1f} мм > стола "
                     f"{self.p.bed_x - 2*self.MARGIN:.0f} мм. "
                     f"Уменьшите H, линии в ступеньке или доп. проходы.")
        if p["line_length_mm"] + 2 * self.MARGIN > self.p.bed_y:
            e.append("Длина дорожки не помещается по Y")
        if self.z_of(H) > self.p.max_z - 10:
            e.append("Высота пирамидки превышает ось Z")
        if self.pr.layer_height > self.pr.max_layer_height + 1e-9:
            e.append(f"Высота слоя {self.pr.layer_height} > предела сопла "
                     f"{self.pr.max_layer_height:.2f} мм")
        return e

    def _run(self, p):
        H = int(p["max_layers"])
        n = self.resolve_n(p)
        L = p["line_length_mm"]
        flow = p["flow_override"]
        passes = self.passes(p)
        bed = p["bed_temp_override"] or self.m.bed_temp
        t0, t1 = int(p["temp_start"]), int(p["temp_end"])
        dT = (t1 - t0) / (passes - 1) if passes > 1 else 0.0
        soak = p["heat_soak_s_per_deg"]

        lw = self.pr.line_width
        gw = n * lw                                   # ширина группы
        nprime = int(p["n_prime_lines"])
        x_start = self.MARGIN
        y0 = (self.p.bed_y - L) / 2
        origin_x = x_start + nprime * lw + (6.0 if nprime else 0.0)

        angle = math.degrees(math.atan2(self.pr.layer_height, gw))

        self.start(t0, bed)
        self.w.comment(f"H={H} layers (top Z={self.z_of(H):.3f} mm), n={n} lines/step")
        self.w.comment(f"Group width={gw:.3f} mm, surface angle={angle:.2f} deg")
        self.w.comment(f"Passes={passes}, dT={dT:.2f} C/pass, groups={passes+1}")
        self.w.comment("Order: pass s -> group g=s-j+1 on layer j=1..min(H,s+1)")

        # --- подготовительные линии на столе ---
        if nprime:
            self.w.comment(f"PRIME: {nprime} lines @ {t0} C")
            self.apply_fan(1)
            self.snake(x_start, y0, self.z_of(1), nprime, L,
                       self.pr.first_layer_line_width, self.pr.first_layer_height,
                       flow, self.m.first_layer_speed, long_move=True)

        tops: Dict[int, int] = {}
        prev_t = t0

        for s in range(1, passes + 1):
            temp = int(round(t0 + dT * (s - 1)))

            # --- смена температуры ПЕРЕД тройкой на столе ---
            if s > 1:
                self.w.comment(f"[PASS {s}] heat {prev_t} -> {temp} C")
                self.w.nozzle(temp, wait=False)
                delta = abs(temp - prev_t)
                if soak > 0 and delta >= 0.5:
                    self.w.dwell(int(max(500, soak * delta * 1000)),
                                 f"soak dT={delta:.1f}C")
                self.w.nozzle(temp, wait=True)
            prev_t = temp

            seq: List[Tuple[int, int]] = []
            if s == 1:
                seq.append((0, 1))                    # база группы 0
            for j in range(1, min(H, s + 1) + 1):
                g = s - j + 1
                if g >= 0:
                    seq.append((g, j))

            self.w.comment(f"=== PASS {s}/{passes}  T={temp} C  "
                           f"steps={[(g, j) for g, j in seq]} ===")

            for idx, (g, j) in enumerate(seq):
                z, h = self.z_of(j), self.lh_of(j)
                wdt, spd = self.lw_of(j), self.speed_of(j)
                self.apply_fan(j)
                gx = origin_x + g * gw
                self.w.comment(f"  group {g} @ layer {j}  Z={z:.3f}")
                # первый переход прохода — длинный (спуск к столу)
                self.snake(gx, y0, z, n, L, wdt, h, flow, spd,
                           long_move=(idx == 0))
                tops[g] = max(tops.get(g, 0), j)

        # геометрия для 3MF
        for g, jmax in sorted(tops.items()):
            gx = origin_x + g * gw
            self.boxes.append((gx, y0, 0.0, gw, L, self.z_of(jmax)))

        self.finish()


# =============================================================================
# 6. ТЕСТ Z-OFFSET
# =============================================================================

class ZOffsetTest(TestBase):
    """Однослойная полоса: Z-offset линейно меняется вдоль X."""

    def key(self) -> str: return "zoffset"
    def title(self) -> str: return "Z-OFFSET STRIP TEST"

    def params(self):
        return {
            "strip_length_mm": dict(label="Длина полосы (X)", default=100.0,
                                    type="float", min=30, max=220, unit="мм"),
            "strip_width_mm":  dict(label="Ширина полосы (Y)", default=20.0,
                                    type="float", min=5, max=80, unit="мм"),
            "z_start": dict(label="Z-offset от", default=0.05, type="float",
                            min=-0.5, max=0.5, unit="мм"),
            "z_end":   dict(label="Z-offset до", default=-0.10, type="float",
                            min=-0.5, max=0.5, unit="мм"),
            "n_segments": dict(label="Сегментов", default=20, type="int", min=4, max=60),
            "flow_override": dict(label="Поток (база оператора)", default=1.0,
                                  type="float", min=0.3, max=2.0),
            "nozzle_temp_override": dict(label="Сопло °C (0 = из профиля)", default=0,
                                         type="int", min=0, max=350, unit="°C"),
            "bed_temp_override": dict(label="Стол °C (0 = из профиля)", default=0,
                                      type="int", min=0, max=150, unit="°C"),
        }

    def validate(self, p):
        e = []
        if p["strip_length_mm"] + 2 * self.MARGIN > self.p.bed_x:
            e.append("Длина полосы не помещается по X")
        if p["strip_width_mm"] + 2 * self.MARGIN > self.p.bed_y:
            e.append("Ширина полосы не помещается по Y")
        if abs(p["z_end"] - p["z_start"]) < 0.02:
            e.append("Диапазон Z-offset должен быть ≥ 0.02 мм")
        zmin = self.pr.first_layer_height + min(p["z_start"], p["z_end"])
        if zmin < self.pr.min_safe_z:
            e.append(f"Минимальный Z = {zmin:.3f} мм < {self.pr.min_safe_z} мм — "
                     f"сопло будет царапать стол. Поднимите z_start/z_end "
                     f"или уменьшите высоту первого слоя.")
        t = p["nozzle_temp_override"]
        if t and t > min(self.m.max_temp, self.p.max_nozzle_temp):
            e.append("Температура сопла выше допустимой")
        return e

    def _run(self, p):
        nozzle = p["nozzle_temp_override"] or self.m.nozzle_temp
        bed = p["bed_temp_override"] or self.m.bed_temp
        L, W = p["strip_length_mm"], p["strip_width_mm"]
        segs = int(p["n_segments"])
        lw1, h1 = self.pr.first_layer_line_width, self.pr.first_layer_height
        n = max(1, int(round(W / lw1)))
        W = n * lw1
        x0 = (self.p.bed_x - L) / 2
        y0 = (self.p.bed_y - W) / 2
        seg = L / segs
        dz = (p["z_end"] - p["z_start"]) / segs

        self.start(nozzle, bed, prime=False)
        self.w.comment(f"Z-offset {p['z_start']:+.3f} -> {p['z_end']:+.3f} mm, "
                       f"{segs} segments, {n} lines")
        self.apply_fan(1)

        for i in range(n):
            y = y0 + i * lw1
            for s in range(segs):
                off = p["z_start"] + (s + 0.5) * dz
                z = h1 + off
                xa = x0 + s * seg
                xb = xa + seg
                if i % 2 == 0:
                    self.travel_safe(xa, y, z, long_move=(s == 0))
                    self.line(xa, y, xb, y, z, lw1, h1, p["flow_override"],
                              self.m.first_layer_speed, f"z_off={off:+.3f}")
                else:
                    self.travel_safe(xb, y, z, long_move=(s == 0))
                    self.line(xb, y, xa, y, z, lw1, h1, p["flow_override"],
                              self.m.first_layer_speed, f"z_off={off:+.3f}")
        self.boxes.append((x0, y0, 0.0, L, W, h1))
        self.finish()


TESTS = {t.key(): t for t in (FlowTest, TemperatureStaircaseTest, ZOffsetTest)}
TEST_LABELS = {"flow": "Поток", "temperature": "Температура (лестница)",
               "zoffset": "Z-offset"}
TEMP_KEYS = {"temp_start", "temp_end", "nozzle_temp_override", "bed_temp_override"}


# =============================================================================
# 7. УПАКОВКА 3MF
# =============================================================================

class ThreeMFBuilder:
    """Реальная геометрия (набор замкнутых боксов) + настройки + G-code."""

    VERSION = "2.5.0"

    @staticmethod
    def _box_mesh(w: float, d: float, h: float) -> str:
        v = [(0,0,0),(w,0,0),(w,d,0),(0,d,0),(0,0,h),(w,0,h),(w,d,h),(0,d,h)]
        tr = [(0,2,1),(0,3,2),(4,5,6),(4,6,7),(0,1,5),(0,5,4),
              (1,2,6),(1,6,5),(2,3,7),(2,7,6),(3,0,4),(3,4,7)]
        vs = "\n".join(f'      <vertex x="{a:.4f}" y="{b:.4f}" z="{c:.4f}"/>'
                       for a, b, c in v)
        ts = "\n".join(f'      <triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tr)
        return f"    <mesh>\n     <vertices>\n{vs}\n     </vertices>\n" \
               f"     <triangles>\n{ts}\n     </triangles>\n    </mesh>"

    def build(self, path: str, gcode: str, boxes: List[Box],
              printer: PrinterProfile, material: MaterialProfile,
              process: ProcessProfile, test_key: str,
              stats: Dict[str, float]) -> None:
        objs, items = [], []
        if not boxes:
            boxes = [(0, 0, 0, 40, 20, 2)]
        for i, (x, y, z, w, d, h) in enumerate(boxes, start=1):
            objs.append(f'  <object id="{i}" type="model" name="step_{i}">\n'
                        f'{self._box_mesh(max(w,0.1), max(d,0.1), max(h,0.1))}\n  </object>')
            items.append(f'  <item objectid="{i}" '
                         f'transform="1 0 0 0 1 0 0 0 1 {x:.4f} {y:.4f} {z:.4f}"/>')

        model = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<model unit="millimeter" xml:lang="en-US" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
            f' <metadata name="Application">OrcaSlicer-{self.VERSION}</metadata>\n'
            f' <metadata name="Title">FastTestCoil {test_key}</metadata>\n'
            f' <metadata name="CreationDate">{time.strftime("%Y-%m-%d")}</metadata>\n'
            ' <resources>\n' + "\n".join(objs) + '\n </resources>\n'
            ' <build>\n' + "\n".join(items) + '\n </build>\n</model>\n')

        settings = {
            "printer_model": printer.name,
            "printer_variant": str(process.nozzle_diameter),
            "printer_settings_id": printer.name,
            "nozzle_diameter": [str(process.nozzle_diameter)],
            "printable_area": [f"0x0", f"{printer.bed_x}x0",
                               f"{printer.bed_x}x{printer.bed_y}", f"0x{printer.bed_y}"],
            "printable_height": str(printer.max_z),
            "filament_type": [material.name],
            "filament_colour": ["#FF7A00"],
            "filament_diameter": [str(material.filament_diameter)],
            "filament_density": [str(material.density)],
            "filament_flow_ratio": [str(material.flow_ratio)],
            "nozzle_temperature": [str(material.nozzle_temp)],
            "nozzle_temperature_initial_layer": [str(material.nozzle_temp)],
            "hot_plate_temp": [str(material.bed_temp)],
            "hot_plate_temp_initial_layer": [str(material.bed_temp)],
            "bed_temperature": [str(material.bed_temp)],
            "fan_max_speed": [str(round(material.fan_speed / 255 * 100))],
            "fan_min_speed": [str(round(material.fan_speed / 255 * 100))],
            "filament_retraction_length": [str(material.retract_length)],
            "filament_retraction_speed": [str(material.retract_speed)],
            "filament_deretraction_speed": [str(material.deretract_speed)],
            "layer_height": str(process.layer_height),
            "initial_layer_print_height": str(process.first_layer_height),
            "line_width": str(round(process.line_width, 4)),
            "initial_layer_line_width": str(round(process.first_layer_line_width, 4)),
            "default_speed": str(material.print_speed),
            "initial_layer_speed": str(material.first_layer_speed),
            "travel_speed": str(process.travel_speed),
            "enable_support": "0",
            "enable_prime_tower": "0",
        }

        used_g = stats.get("grams", 0.0)
        plate_objs = "\n".join(
            f'   <object id="{i}"/>' for i in range(1, len(boxes) + 1))
        model_settings = (
            '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n <plate>\n'
            '  <metadata key="plater_id" value="1"/>\n'
            '  <metadata key="plater_name" value="Calibration"/>\n'
            '  <metadata key="gcode_file" value="Metadata/plate_1.gcode"/>\n'
            '  <metadata key="thumbnail_file" value=""/>\n'
            f'{plate_objs}\n </plate>\n</config>\n')

        slice_info = (
            '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n <header>\n'
            '  <header_item key="X-BBL-Client-Type" value="slicer"/>\n'
            f'  <header_item key="X-BBL-Client-Version" value="{self.VERSION}"/>\n'
            ' </header>\n <plate>\n'
            '  <metadata key="index" value="1"/>\n'
            f'  <metadata key="prediction" value="{stats.get("seconds", 0):.0f}"/>\n'
            f'  <metadata key="weight" value="{used_g:.2f}"/>\n'
            '  <metadata key="support_used" value="false"/>\n'
            '  <metadata key="outside" value="false"/>\n'
            f'  <metadata key="printer_model_id" value="{printer.name}"/>\n'
            f'  <metadata key="nozzle_diameters" value="{process.nozzle_diameter}"/>\n'
            f'  <filament id="1" type="{material.name}" color="#FF7A00" '
            f'used_m="{stats.get("meters", 0):.3f}" used_g="{used_g:.2f}"/>\n'
            ' </plate>\n</config>\n')

        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml",
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
                '<Default Extension="gcode" ContentType="text/x.gcode"/>'
                '<Default Extension="config" ContentType="text/plain"/>'
                '<Default Extension="xml" ContentType="text/xml"/></Types>')
            z.writestr("_rels/.rels",
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rel0" Target="/3D/3dmodel.model" '
                'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
            z.writestr("3D/3dmodel.model", model)
            z.writestr("Metadata/project_settings.config", json.dumps(settings, indent=2))
            z.writestr("Metadata/model_settings.config", model_settings)
            z.writestr("Metadata/slice_info.config", slice_info)
            z.writestr("Metadata/plate_1.gcode", gcode)


# =============================================================================
# 8. GUI
# =============================================================================

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("FastTestCoil Pro v3.0")
        root.geometry("980x820")
        root.minsize(760, 600)

        self.printer = copy.deepcopy(PRINTERS["Flashforge Adventurer 5M"])
        self.material = copy.deepcopy(MATERIALS["PLA"])
        self.process = ProcessProfile(nozzle_diameter=self.printer.nozzle_diameter)

        self.pv: Dict[str, tk.Variable] = {}   # printer vars
        self.mv: Dict[str, tk.Variable] = {}   # material vars
        self.cv: Dict[str, tk.Variable] = {}   # process vars
        self.tv: Dict[str, tk.Variable] = {}   # test vars
        self.gcode: Optional[str] = None
        self.boxes: List[Box] = []
        self.stats: Dict[str, float] = {}
        self.test_key = "flow"
        self._busy = False

        self._ui()
        self._pull_printer()
        self._pull_material()
        self._pull_process()
        self._rebuild_test()

    # ---------- построение ----------
    def _ui(self):
        s = ttk.Style()
        s.configure("H.TLabel", font=("Segoe UI", 10, "bold"))
        s.configure("Warn.TLabel", foreground="#b00020")

        top = ttk.LabelFrame(self.root, text="Профили", padding=6)
        top.pack(fill="x", padx=6, pady=(6, 3))

        r = ttk.Frame(top); r.pack(fill="x")
        ttk.Label(r, text="Принтер:", style="H.TLabel").pack(side="left")
        self.printer_var = tk.StringVar(value=self.printer.name)
        cb = ttk.Combobox(r, textvariable=self.printer_var, values=list(PRINTERS),
                          state="readonly", width=30)
        cb.pack(side="left", padx=(4, 16))
        cb.bind("<<ComboboxSelected>>", self.on_printer)

        ttk.Label(r, text="Материал:", style="H.TLabel").pack(side="left")
        self.material_var = tk.StringVar(value=self.material.name)
        cb2 = ttk.Combobox(r, textvariable=self.material_var, values=list(MATERIALS),
                           state="readonly", width=16)
        cb2.pack(side="left", padx=4)
        cb2.bind("<<ComboboxSelected>>", self.on_material)

        ttk.Button(r, text="Сбросить всё к пресетам",
                   command=self.on_reset).pack(side="right")

        self.info = ttk.Label(top, text="", foreground="#555")
        self.info.pack(anchor="w", pady=(4, 0))
        self.warn = ttk.Label(top, text="", style="Warn.TLabel", wraplength=940)
        self.warn.pack(anchor="w")

        nb = ttk.Notebook(self.root)
        nb.pack(fill="x", padx=6, pady=3)
        self.nb = nb

        t = ttk.Frame(nb, padding=6); nb.add(t, text="Тест")
        rr = ttk.Frame(t); rr.pack(fill="x", pady=(0, 6))
        ttk.Label(rr, text="Тип теста:", style="H.TLabel").pack(side="left")
        self.test_var = tk.StringVar(value=TEST_LABELS["flow"])
        cb3 = ttk.Combobox(rr, textvariable=self.test_var,
                           values=list(TEST_LABELS.values()), state="readonly", width=26)
        cb3.pack(side="left", padx=4)
        cb3.bind("<<ComboboxSelected>>", self.on_test)
        self.geom_info = ttk.Label(t, text="", foreground="#0a5")
        self.geom_info.pack(anchor="w", pady=(0, 4))
        self.test_box = ttk.LabelFrame(t, text="Параметры", padding=6)
        self.test_box.pack(fill="x")

        self.mat_tab = ttk.Frame(nb, padding=6); nb.add(self.mat_tab, text="Материал")
        self.proc_tab = ttk.Frame(nb, padding=6); nb.add(self.proc_tab, text="Процесс")
        self.prn_tab = ttk.Frame(nb, padding=6); nb.add(self.prn_tab, text="Принтер")
        code_tab = ttk.Frame(nb, padding=6); nb.add(code_tab, text="Старт / Финиш")

        self._material_fields()
        self._process_fields()
        self._printer_fields()
        self._code_fields(code_tab)

        bot = ttk.Frame(self.root); bot.pack(fill="both", expand=True, padx=6, pady=3)
        b = ttk.Frame(bot); b.pack(fill="x", pady=(0, 4))
        ttk.Button(b, text="Сгенерировать", command=self.on_generate).pack(side="left")
        ttk.Button(b, text="Сохранить .3mf", command=self.on_3mf).pack(side="left", padx=3)
        ttk.Button(b, text="Сохранить .gcode", command=self.on_gcode).pack(side="left", padx=3)
        ttk.Button(b, text="Копировать", command=self.on_copy).pack(side="right")

        pf = ttk.LabelFrame(bot, text="G-code", padding=3)
        pf.pack(fill="both", expand=True)
        self.txt = tk.Text(pf, wrap="none", font=("Consolas", 9),
                           bg="#1e1e1e", fg="#d4d4d4", insertbackground="white")
        sy = ttk.Scrollbar(pf, orient="vertical", command=self.txt.yview)
        sx = ttk.Scrollbar(pf, orient="horizontal", command=self.txt.xview)
        self.txt.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        self.txt.grid(row=0, column=0, sticky="nsew")
        sy.grid(row=0, column=1, sticky="ns")
        sx.grid(row=1, column=0, sticky="ew")
        pf.rowconfigure(0, weight=1); pf.columnconfigure(0, weight=1)

        self.status = tk.StringVar(value="Готов")
        ttk.Label(self.root, textvariable=self.status, relief="sunken",
                  anchor="w", padding=3).pack(fill="x", side="bottom")

    def _field(self, parent, i, key, label, value, store, kind="float", hint=""):
        row, col = divmod(i, 2)
        ttk.Label(parent, text=label).grid(row=row, column=col*3, sticky="w",
                                           padx=(4, 2), pady=2)
        if kind == "int":
            var = tk.IntVar(value=int(value))
        elif kind == "bool":
            var = tk.BooleanVar(value=bool(value))
        elif kind == "str":
            var = tk.StringVar(value=str(value))
        else:
            var = tk.DoubleVar(value=float(value))
        if kind == "bool":
            w = ttk.Checkbutton(parent, variable=var)
        else:
            w = ttk.Entry(parent, textvariable=var, width=11)
        w.grid(row=row, column=col*3+1, sticky="w", pady=2)
        ttk.Label(parent, text=hint, foreground="#888").grid(
            row=row, column=col*3+2, sticky="w", padx=(3, 14))
        store[key] = var
        return var

    def _material_fields(self):
        f = [
            ("nozzle_temp", "Сопло, °C", "int"), ("bed_temp", "Стол, °C", "int"),
            ("fan_speed", "Обдув 0..255", "int"),
            ("fan_first_layers", "Слоёв без обдува", "int"),
            ("print_speed", "Печать, мм/с", "float"),
            ("first_layer_speed", "1-й слой, мм/с", "float"),
            ("retract_length", "Ретракт, мм", "float"),
            ("retract_speed", "Скорость ретракта, мм/с", "float"),
            ("deretract_speed", "Скорость возврата, мм/с", "float"),
            ("filament_diameter", "Диаметр прутка, мм", "float"),
            ("density", "Плотность, г/см³", "float"),
            ("flow_ratio", "Базовый поток материала", "float"),
            ("min_temp", "Мин. темп., °C", "int"), ("max_temp", "Макс. темп., °C", "int"),
            ("temp_test_start", "Тест: от, °C", "int"),
            ("temp_test_end", "Тест: до, °C", "int"),
        ]
        for i, (k, l, t) in enumerate(f):
            self._field(self.mat_tab, i, k, l, getattr(self.material, k), self.mv, t)

    def _process_fields(self):
        f = [
            ("nozzle_diameter", "Диаметр сопла, мм", "float"),
            ("line_width_factor", "Множитель ширины линии", "float"),
            ("first_layer_line_width_factor", "Множитель ширины 1-го слоя", "float"),
            ("layer_height", "Высота слоя, мм", "float"),
            ("first_layer_height", "Высота 1-го слоя, мм", "float"),
            ("travel_speed", "Холостой ход, мм/с", "float"),
            ("z_hop", "Z-hop, мм", "float"),
            ("prime_length", "Прайм-линия, мм", "float"),
            ("min_safe_z", "Мин. безопасный Z, мм", "float"),
        ]
        for i, (k, l, t) in enumerate(f):
            v = self._field(self.proc_tab, i, k, l, getattr(self.process, k), self.cv, t)
            v.trace_add("write", lambda *_: self.on_process_edit())
        box = ttk.LabelFrame(self.proc_tab, text="Вычисляемые", padding=5)
        box.grid(row=6, column=0, columnspan=6, sticky="w", pady=(8, 0))
        self.calc = ttk.Label(box, text="", foreground="#06c")
        self.calc.pack(anchor="w")

    def _printer_fields(self):
        f = [
            ("bed_x", "Стол X, мм", "float"), ("bed_y", "Стол Y, мм", "float"),
            ("max_z", "Макс. Z, мм", "float"),
            ("nozzle_diameter", "Сопло (штатное), мм", "float"),
            ("max_nozzle_temp", "Лимит сопла, °C", "int"),
            ("max_bed_temp", "Лимит стола, °C", "int"),
            ("max_speed", "Лимит скорости, мм/с", "float"),
            ("max_accel", "Лимит ускорения, мм/с²", "float"),
            ("park_x", "Парковка X, мм", "float"),
            ("park_y", "Парковка Y, мм", "float"),
            ("firmware", "Прошивка", "str"), ("kinematics", "Кинематика", "str"),
        ]
        for i, (k, l, t) in enumerate(f):
            self._field(self.prn_tab, i, k, l, getattr(self.printer, k), self.pv, t)
        self.prn_note = ttk.Label(self.prn_tab, text="", foreground="#888",
                                  wraplength=900)
        self.prn_note.grid(row=7, column=0, columnspan=6, sticky="w", pady=(8, 0))

    def _code_fields(self, parent):
        ttk.Label(parent, text="Плейсхолдеры: {bed_temp} {nozzle_temp} "
                               "{park_x} {park_y} {bed_y}",
                  foreground="#888").pack(anchor="w")
        pan = ttk.PanedWindow(parent, orient="horizontal")
        pan.pack(fill="both", expand=True, pady=4)
        lf = ttk.LabelFrame(pan, text="START G-code", padding=3)
        rf = ttk.LabelFrame(pan, text="END G-code", padding=3)
        pan.add(lf, weight=1); pan.add(rf, weight=1)
        self.start_txt = tk.Text(lf, height=12, wrap="none", font=("Consolas", 9))
        self.start_txt.pack(fill="both", expand=True)
        self.end_txt = tk.Text(rf, height=12, wrap="none", font=("Consolas", 9))
        self.end_txt.pack(fill="both", expand=True)

    # ---------- синхронизация ----------
    def _pull_printer(self):
        for k, v in self.pv.items():
            try:
                setattr(self.printer, k, v.get())
            except tk.TclError:
                pass
        self.printer.start_gcode = self.start_txt.get("1.0", "end-1c")
        self.printer.end_gcode = self.end_txt.get("1.0", "end-1c")

    def _pull_material(self):
        for k, v in self.mv.items():
            try:
                setattr(self.material, k, v.get())
            except tk.TclError:
                pass

    def _pull_process(self):
        for k, v in self.cv.items():
            try:
                setattr(self.process, k, v.get())
            except tk.TclError:
                pass
        self._refresh_calc()

    def _push_printer(self):
        for k, v in self.pv.items():
            v.set(getattr(self.printer, k))
        self.start_txt.delete("1.0", "end"); self.start_txt.insert("1.0", self.printer.start_gcode)
        self.end_txt.delete("1.0", "end"); self.end_txt.insert("1.0", self.printer.end_gcode)
        self.prn_note.config(text=self.printer.notes)

    def _push_material(self):
        for k, v in self.mv.items():
            v.set(getattr(self.material, k))

    def _push_process(self):
        for k, v in self.cv.items():
            v.set(getattr(self.process, k))
        self._refresh_calc()

    def _refresh_calc(self):
        if not hasattr(self, "calc"):
            return
        pr = self.process
        self.calc.config(text=(
            f"Ширина линии: {pr.line_width:.3f} мм    "
            f"Ширина 1-го слоя: {pr.first_layer_line_width:.3f} мм    "
            f"Предел слоя для сопла: {pr.max_layer_height:.2f} мм"))

    def _refresh_info(self):
        p, m, c = self.printer, self.material, self.process
        self.info.config(text=(
            f"{p.bed_x:.0f}×{p.bed_y:.0f}×{p.max_z:.0f} мм  |  "
            f"лимиты {p.max_nozzle_temp}/{p.max_bed_temp} °C, {p.max_speed:.0f} мм/с  |  "
            f"сопло {c.nozzle_diameter} мм → линия {c.line_width:.3f} мм  |  "
            f"{m.name}: {m.nozzle_temp}/{m.bed_temp} °C, обдув {m.fan_speed}, "
            f"{m.print_speed} мм/с"))
        warns = []
        if m.nozzle_temp > p.max_nozzle_temp:
            warns.append(f"{m.name} требует {m.nozzle_temp} °C, "
                         f"принтер даёт {p.max_nozzle_temp} °C")
        if m.max_temp > p.max_nozzle_temp:
            warns.append(f"верх теста будет ограничен {p.max_nozzle_temp} °C")
        if c.layer_height > c.max_layer_height:
            warns.append(f"слой {c.layer_height} мм > предела сопла {c.max_layer_height:.2f} мм")
        if m.bed_temp > p.max_bed_temp:
            warns.append(f"стол {m.bed_temp} °C > лимита {p.max_bed_temp} °C")
        self.warn.config(text=("⚠ " + "; ".join(warns)) if warns else "")

    # ---------- обработчики ----------
    def on_printer(self, _=None):
        if self._busy:
            return
        self._busy = True
        try:
            self.printer = copy.deepcopy(PRINTERS[self.printer_var.get()])
            self._push_printer()
            # сопло принтера -> процесс
            self.process.nozzle_diameter = self.printer.nozzle_diameter
            self.process.travel_speed = min(self.process.travel_speed, self.printer.max_speed)
            self._push_process()
            # ограничиваем материал паспортом принтера
            self.material.nozzle_temp = min(self.material.nozzle_temp, self.printer.max_nozzle_temp)
            self.material.bed_temp = min(self.material.bed_temp, self.printer.max_bed_temp)
            self.material.temp_test_end = min(self.material.temp_test_end,
                                              self.printer.max_nozzle_temp)
            self.material.temp_test_start = min(self.material.temp_test_start,
                                                self.material.temp_test_end - 10)
            self._push_material()
            self._refresh_info()
            self._rebuild_test(keep_all=True)
            self.status.set(f"Принтер: {self.printer.name} — процесс и лимиты обновлены")
        finally:
            self._busy = False

    def on_material(self, _=None):
        if self._busy:
            return
        self._busy = True
        try:
            self.material = copy.deepcopy(MATERIALS[self.material_var.get()])
            self.material.nozzle_temp = min(self.material.nozzle_temp, self.printer.max_nozzle_temp)
            self.material.bed_temp = min(self.material.bed_temp, self.printer.max_bed_temp)
            self.material.temp_test_end = min(self.material.temp_test_end,
                                              self.printer.max_nozzle_temp)
            self.material.temp_test_start = min(self.material.temp_test_start,
                                                self.material.temp_test_end - 10)
            self._push_material()
            self._refresh_info()
            self._rebuild_test(keep_all=False)
            self.status.set(f"Материал: {self.material.name} — все параметры обновлены")
        finally:
            self._busy = False

    def on_process_edit(self):
        if self._busy:
            return
        self._pull_process()
        self._refresh_info()
        self._update_geom_info()

    def on_test(self, _=None):
        label = self.test_var.get()
        self.test_key = next(k for k, v in TEST_LABELS.items() if v == label)
        self._rebuild_test(keep_all=False)

    def on_reset(self):
        self._busy = True
        try:
            self.printer = copy.deepcopy(PRINTERS[self.printer_var.get()])
            self.material = copy.deepcopy(MATERIALS[self.material_var.get()])
            self.process = ProcessProfile(nozzle_diameter=self.printer.nozzle_diameter)
            self._push_printer(); self._push_material(); self._push_process()
            self._refresh_info(); self._rebuild_test(keep_all=False)
            self.status.set("Все вкладки сброшены к пресетам")
        finally:
            self._busy = False

    # ---------- параметры теста ----------
    def _make_test(self) -> TestBase:
        return TESTS[self.test_key](self.printer, self.material, self.process)

    def _rebuild_test(self, keep_all: bool = False):
        old = {}
        for k, v in self.tv.items():
            try:
                old[k] = v.get()
            except tk.TclError:
                pass
        for w in self.test_box.winfo_children():
            w.destroy()
        self.tv.clear()

        self._pull_process(); self._pull_material(); self._pull_printer()
        t = self._make_test()
        for i, (k, info) in enumerate(t.params().items()):
            hint = ""
            if "min" in info and "max" in info:
                hint = f"{info['min']}…{info['max']}"
            if info.get("unit"):
                hint += f" {info['unit']}"
            var = self._field(self.test_box, i, k, info["label"], info["default"],
                              self.tv, info["type"], hint)
            if k in ("angle_deg", "n_lines_per_step", "angle_drives_n",
                     "max_layers", "extra_steps"):
                var.trace_add("write", lambda *_: self._update_geom_info())
            if keep_all and k in old:
                try: var.set(old[k])
                except tk.TclError: pass
            elif not keep_all and k in old and k not in TEMP_KEYS:
                try: var.set(old[k])
                except tk.TclError: pass
        self._update_geom_info()

    def _update_geom_info(self):
        if self.test_key != "temperature" or not self.tv:
            self.geom_info.config(text="")
            return
        try:
            p = {k: v.get() for k, v in self.tv.items()}
            t = self._make_test()
            n = t.resolve_n(p)
            H = int(p["max_layers"]); passes = t.passes(p)
            gw = n * self.process.line_width
            ang = math.degrees(math.atan2(self.process.layer_height, gw))
            total_x = (passes + 1) * gw
            dT = ((p["temp_end"] - p["temp_start"]) / (passes - 1)) if passes > 1 else 0
            self.geom_info.config(text=(
                f"Ступенька: {n} линий × {self.process.line_width:.3f} = {gw:.2f} мм, "
                f"угол {ang:.2f}°  |  проходов {passes}, ΔT {dT:.2f} °C/проход  |  "
                f"групп {passes+1}, ширина {total_x:.1f} мм, "
                f"высота {self.process.first_layer_height + (H-1)*self.process.layer_height:.2f} мм"))
        except Exception:
            self.geom_info.config(text="")

    # ---------- генерация / экспорт ----------
    def on_generate(self):
        try:
            self._pull_printer(); self._pull_material(); self._pull_process()
            t = self._make_test()
            p = {k: v.get() for k, v in self.tv.items()}
            errs = t.validate(p)
            if errs:
                messagebox.showerror("Проверка параметров",
                                     "\n".join("• " + e for e in errs))
                return
            gcode = t.generate(p)
            self.gcode, self.boxes = gcode, t.boxes
            area = math.pi * (self.material.filament_diameter / 2) ** 2
            self.stats = {
                "seconds": t.w.time_s,
                "meters": t.w.total_e / 1000.0,
                "grams": t.w.total_e * area / 1000.0 * self.material.density,
            }
            self.txt.delete("1.0", "end"); self.txt.insert("1.0", gcode)
            self.status.set(
                f"{len(gcode.splitlines())} строк | "
                f"{self.stats['meters']*1000:.0f} мм филамента "
                f"({self.stats['grams']:.2f} г) | ~{self.stats['seconds']/60:.1f} мин | "
                f"max Z {t.w.max_print_z:.2f} мм")
        except GCodeError as e:
            messagebox.showerror("Нарушение границ", str(e))
        except Exception as e:
            messagebox.showerror("Ошибка генерации", f"{type(e).__name__}: {e}")

    def on_3mf(self):
        if not self.gcode:
            messagebox.showinfo("Нет данных", "Сначала сгенерируйте G-code")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".3mf",
            initialfile=f"FTC_{self.test_key}_{self.printer.name.replace(' ', '_')}.3mf",
            filetypes=[("3MF project", "*.3mf"), ("Все файлы", "*.*")])
        if not path:
            return
        try:
            ThreeMFBuilder().build(path, self.gcode, self.boxes, self.printer,
                                   self.material, self.process, self.test_key, self.stats)
            self.status.set(f"Сохранено: {path}")
        except Exception as e:
            messagebox.showerror("Ошибка сохранения", str(e))

    def on_gcode(self):
        if not self.gcode:
            messagebox.showinfo("Нет данных", "Сначала сгенерируйте G-code")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".gcode",
            initialfile=f"FTC_{self.test_key}_{self.printer.name.replace(' ', '_')}.gcode",
            filetypes=[("G-code", "*.gcode"), ("Все файлы", "*.*")])
        if path:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.gcode)
            self.status.set(f"Сохранено: {path}")

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