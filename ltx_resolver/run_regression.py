# -*- coding: utf-8 -*-
"""Регрессия Python-резолвера на фикстурах. Ожидаемые значения выведены
из C++ движка (Xr_ini.cpp); обоснование каждого — в REGRESSION.txt."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ltx_resolver import LtxResolver

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "tests", "fixtures", "configs", "system.ltx")

EXPECTED = [
    # (секция, ключ, ожидаемое значение)  — источник: Xr_ini.cpp, см. REGRESSION.txt
    ("sup", "a",     "alpha_wins"),            # winner order: depth -200 < -400 (410-451)
    ("sup", "list",  "two,alpha_item,zulu_item"),        # CSV >/< (235-256, 1167-1290): one удалён zulu '<', alpha '>' добавлен; zulu '>' позже, но его '<one' уже применён к base... (см. разбор)
    ("dup", "x",     "from_aaa_include"),    # base [dup] в core.ltx(d=1), override ![dup] в aaa.ltx(d=2): wildcard #include *.ltx (670-720); победитель — больший depth среди равных? НЕТ: у override-записей меньший depth важнее, но здесь core d=1 < aaa d=2 => ... см. REGRESSION.txt кейс 4
    ("inc", "v",     "zulu_mod"),              # base из includes/core.ltx + mod override (depth)
    ("child", "a",   "alpha_wins"),                     # наследование [child]:sup (787-808, 1053-1090)
    ("child", "c",   "3"),                     # собственный ключ base
    ("child", "d",   "4"),                     # ключ из ![child]:sup мода
    ("safe_new", None, "__EXISTS_EMPTY__"),    # @[safe_new] создал пустую секцию (762-770, 892-905)
]

def main():
    r = LtxResolver(FIX)
    r.load()
    failures = 0

    def check(name, got, exp):
        nonlocal failures
        ok = got == exp
        if not ok: failures += 1
        print("%-42s %-4s got=%r exp=%r" % (name, "OK" if ok else "FAIL", got, exp))

    for sec, key, exp in EXPECTED:
        if exp == "__EXISTS_EMPTY__":
            check("section_exist(%s)" % sec, r.section_exist(sec), True)
            check("empty(%s)" % sec, len(r.r_section(sec).data), 0)
        else:
            try:
                v = r.r_string(sec, key)
            except KeyError:
                v = "<missing>"
            check("%s.%s" % (sec, key), v, exp)

    # !![gone] — секция удалена (721-740, 1354-1370)
    check("!![gone] deleted", r.section_exist("gone"), False)
    # !b = удаление ключа (815/867 + MergeSections 908-1002)
    check("sup.b kept (quirk !b tail)", r.line_exist("sup", "b"), True)
    check("sup.b value", r.r_string("sup","b"), "20")  # ![sup] b=20 (root, d=0) побеждает base b=2; !b из alpha НЕ удаляет base-ключ того же merge (квирк Xr_ini.cpp:975-984,1206-1211)
    # sup 20 из ![sup] root-override (base b=2 удалён? нет: b удалён модом через !b)
    # winner_of теперь кортеж (filename, depth, insertion_index)
    w = r.winner_of("sup", "a")
    check("winner_of(sup,a)[0..1]", (w[0], w[1]), ("mod_system_alpha", -200))
    # список секций финального DATA отсортирован по xr_strcmp
    check("sections sorted", [n for n, _ in r.data],
          sorted([n for n, _ in r.data]))
    print("\nRESULT:", "ALL PASS" if failures == 0 else "%d FAILURES" % failures)
    return 1 if failures else 0

if __name__ == "__main__":
    sys.exit(main())
