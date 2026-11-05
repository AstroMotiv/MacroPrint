01.07.2026
# Технические характеристики

Область печати: 220х220х250
Коорднаты (0, 0, 0): Левый нижний угол стола
Ось Х: слева направо
Ось Y: сзади вперёд??
Тип сопла: E3D 0.2
Тип хотэнда: 
Тип кинематики:
Тип прошивки: Marlin


# M503 - Заводские настройки
```Console
  
SENDING:M503 
echo: G21 ; Units in mm (mm) 
echo: M149 C ; Units in Celsius 
echo:; Filament settings: Disabled 
echo: M200 S0 D1.75 
echo:; Steps per unit: 
echo: M92 X80.63 Y80.03 Z405.86 E397.96 
echo:; Maximum feedrates (units/s): 
echo: M203 X300.00 Y300.00 Z4.00 E25.00 
echo:; Maximum Acceleration (units/s2): 
echo: M201 X1000.00 Y1000.00 Z100.00 E500.00 
echo:; Acceleration (units/s2): P<print_accel> R<retract_accel> T<travel_accel> 
echo: M204 P500.00 R500.00 T1000.00 
echo:; Advanced: B<min_segment_time_us> S<min_feedrate> T<min_travel_feedrate> X<max_x_jerk> Y<max_y_jerk> Z<max_z_jerk> E<max_e_jerk> 
echo: M205 B20000.00 S0.00 T0.00 X8.00 Y8.00 Z5.00 E5.00 
echo:; Home offset: 
echo: M206 X0.00 Y0.00 Z0.00 
echo:; Auto Bed Leveling: 
echo: M420 S0 Z0.00 
echo:; Material heatup parameters: 
echo: M145 S0 H190 B60 F0 
echo: M145 S1 H240 B110 F0 
echo:; PID settings: 
echo: M301 P10.08 I0.39 D65.84 
echo: M304 P97.10 I1.41 D1675.16 ; Controller Fan 
echo: M710 S5 I0 A1 D60 ; (1% 0%) 
echo:; Power-Loss Recovery: 
echo: M413 S1 echo:; Z-Probe Offset (mm): 
echo: M851 X37.50 Y0.00 Z0.00 
echo:; Stepper driver current: 
echo: M906 X0 Y0 Z0 
echo: M906 T0 E0 
echo:; Driver stepping mode: 
echo: M569 S1 X Y Z 
echo: M569 S1 T0 E 
echo:; Linear Advance: 
echo: M900 K0.12 
echo:; Filament load/unload lengths: 
echo: M603 L0.00 U100.00 
echo:; Filament runout sensor: 
echo: M412 S1
```

# M503 - Калибровочные настройки

```Console

```

# g-код для настройки

```

```


Стартовый g-код
```
G90 ; Используем абсолютные координаты
M83 ; относительный режим экструдера
G28 ; Домой
M104 S[first_layer_temperature] ; Установка температуры экструдера
M140 S[first_layer_bed_temperature] ; установка температуры стола
M190 S[first_layer_bed_temperature] ; Ожидание нагрева стола
M109 S[first_layer_temperature] ; Ожидание нагрева экструдера
G1 E7 F4000; Капля при старте
G1 X50 Y1 Z0.3 E-2; Переход к начальной точке очистки
G1 X150 E15; Печать линии
G0 Z0.5 F4000; Подъём вверх
G1 X50 E15; Печать второго слоя линии
G0 Z0.7; Подъём вверх
G1 X150 E15; Печать третьего слоя линии
G0 Z0.9; Подъём вверх
G1 X50 E15; Печать четвёртого слоя линии
G1 E-3; Приостановка течи из экструдера
G92 E0 ; Установка относительных координат филамента
```

конечный
```
M104 S0; Сбрасывает температуру экструдера
M140 S0; Сбрасывает температуру стола
G92 E1; Установка относительных координат филамента
G1 E-5 F1000; Откат для корректного завершения печати
G1 Z240; Подъём экструдера вверх
G90; Используем абсолютные координаты
G0 X0 Y200 F3000; Выдвижение стола вперёд для удобства съёма модели
M84; Выключает все двигатели
```