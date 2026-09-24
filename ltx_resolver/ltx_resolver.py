# -*- coding: utf-8 -*-
"""
Python-резолвер подсистемы загрузки LTX/DLTX движка XRAY.

Полностью повторяет наблюдаемое поведение CInifile из
    /workspace/src/xrCore/Xr_ini.cpp  (функции Load/LTXLoad/loadFile/
    StashCurrentSection/insert_item/MergeSections/EvaluateSection/
    MergeParentSet/SortAndFilterSection)
и /workspace/src/xrCore/xr_ini.h (Item.depth / Item.insertionIndex).

Каждое правило снабжено ссылкой "файл:строки" на код движка.

Основные правила (см. DLTX_RULES.md):
  * корневой файл грузится с depth = 0                     (Xr_ini.cpp:1322-1331)
  * mod_<root>_*.ltx автозагружаются ПОСЛЕ base-файла,     (Xr_ini.cpp:537-614)
        глубина -200, -400, -600 ... (d += dt, dt = -200)   (Xr_ini.cpp:570-571,611)
  #include увеличивает глубину на 1                        (Xr_ini.cpp:692-716)
  * победитель дубликата ключа: меньший depth, при равных  (Xr_ini.cpp:410-451)
        больший insertionIndex
"""

import os
import re
from collections import OrderedDict
from enum import Enum


class DLTXToken(Enum):
    """Служебные токены значений. В движке DLTX_DELETE — interned shared_str
    (Xr_ini.cpp:465), сравнение o_it->second == DLTX_DELETE идёт по указателю
    str_value* (xrstring.h:182), поэтому коллизии с реальными значениями
    физически невозможны. В Python моделируем это отдельным типом Enum,
    чтобы строка "DLTX_DELETE" в конфиге не могла быть спутана с токеном."""
    DELETE = "DELETE"                # !key = (Xr_ini.cpp:867)
    EMPTY = "EMPTY"                  # 'key =' без значения -> движковский NULL


DLTX_DELETE = DLTXToken.DELETE       # Xr_ini.cpp:465
DLTX_EMPTY  = DLTXToken.EMPTY        # NULL shared_str (Xr_ini.cpp:867)
MOD_DEPTH_STEP = -200                # Xr_ini.cpp:570-571, 611


# ---------------------------------------------------------------- utilities

def _xr_strcmp(a, b):
    """xr_strcmp == strcmp (byte-wise), см. _std_extensions.h:205-208."""
    a = "" if a is None else a
    b = "" if b is None else b
    return (a > b) - (a < b)


def _key(s):
    """Ключ сортировки, эквивалентный побайтовому strcmp для ASCII."""
    return s.encode("latin-1", "replace") if isinstance(s, str) else s


def _trim(s):
    """_Trim: удаление пробельных символов с двух сторон (_std_extensions.h)."""
    return s.strip()


def pattern_match(name, mask):
    """
    FS-маска с '*' (PatternMatch, LocatorAPI.cpp; используется в file_list,
    LocatorAPI.cpp:1030-1041). Регистронезависимо: имена файлов в FS_FileSet
    приводятся к lower-case (LocatorAPI_defs.h:63 'low-case name').
    """
    rx = "^" + "".join(".*" if c == "*" else "." if c == "?" else re.escape(c)
                       for c in mask.lower()) + "$"
    return re.match(rx, name.lower()) is not None


def _get_item_count(s):
    """_GetItemCount: число элементов списка через запятую (xrstring.h)."""
    if not s:
        return 0
    n = 1
    inside = False
    for ch in s:
        if ch == '"':
            inside = not inside
        elif ch == "," and not inside:
            n += 1
    return n


def _get_item(s, idx):
    """_GetItem(...,'\"'): i-й элемент списка, кавычки снимаются (xrstring.h)."""
    parts = []
    cur = []
    inside = False
    for ch in s:
        if ch == '"':
            inside = not inside
            continue
        if ch == "," and not inside:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    if idx >= len(parts):
        return ""
    return _trim(parts[idx])


def _parse_value(src):
    """
    _parse (Xr_ini.cpp:38-67): схлопывает пробелы вне кавычек;
    возвращает (строка, bInsideSTR - нечётное число кавычек).
    """
    out = []
    inside = False
    i = 0
    n = len(src)
    while i < n:
        ch = src[i]
        if ch.isspace():
            if inside:
                out.append(ch)
                i += 1
                continue
            while i < n and src[i].isspace():
                i += 1
            continue
        if ch == '"':
            inside = not inside
        out.append(ch)
        i += 1
    return "".join(out), inside


# ---------------------------------------------------------------- data model

class Item:
    """CInifile::Item (xr_ini.h:12-33): first/second/filename/depth/insertionIndex."""
    __slots__ = ("first", "second", "filename", "depth", "insertion_index")

    def __init__(self, first=None, second=None, filename="", depth=0, insertion_index=0):
        self.first = first
        self.second = second
        self.filename = filename
        self.depth = depth
        self.insertion_index = insertion_index


class Sect:
    """CInifile::Sect (xr_ini.h:68-74): { Name; xr_vector<ItemPtr> Data }.
    Итерация/len — по Data, как по вектору в движке."""
    __slots__ = ("name", "data")

    def __init__(self, name=""):
        self.name = name
        self.data = []

    def __iter__(self):
        return iter(self.data)

    def __len__(self):
        return len(self.data)


# ---------------------------------------------------------------- resolver

class LtxResolver:
    """Эквивалент одного экземпляра CInifile (корневой .ltx файла)."""

    def __init__(self, root_path, gamedata_root=None, allow_include_func=None,
                 use_cache=True, verbose=False):
        """
        root_path      — путь к корневому ltx (например .../configs/system.ltx)
        gamedata_root  — эмуляция $game_config$ (для future update_path); не обязат.
        allow_include_func — аналог fastdelegate-предиката (xr_ini.h:81, 341-343).
        use_cache           — аналог dltx_use_cache (Xr_ini.cpp:110).
        """
        self.root_path = os.path.normpath(root_path)
        self.file_name = os.path.basename(self.root_path)      # m_file_name ~ short name
        self.allow_include_func = allow_include_func
        self.use_cache = use_cache
        self.verbose = verbose

        # состояние одной загрузки (Xr_ini.h:176-183)
        self.override_to_filenames = {}
        self.section_to_filename = {}
        self.sections_to_delete = set()
        self.base_parent_data_map = {}
        self.base_data = OrderedDict()
        self.override_parent_data_map = {}
        self.override_data = OrderedDict()
        self.override_modify_list_data = {}

        self.data = []            # финальный Root DATA
        self.warnings = []
        self._cache = {}          # CachedData (Xr_ini.cpp:111)
        # Флаг предупреждений DLTX (отсутствующий родитель и т.п.) —
        # в движке управляется консольной переменной print_dltx_warnings
        # (Xr_ini.cpp:1066/1077). По умолчанию выключен, чтобы не падать
        # на фикстурах с несуществующими родителями.
        self.print_dltx_warnings = False

    # ------------------------------------------------------------- helpers

    def _log(self, msg):
        if self.verbose:
            print(msg)

    def _warn(self, msg):
        self.warnings.append(msg)
        self._log("~[DLTX] WARNING: " + msg)

    @staticmethod
    def _file_list(folder, mask):
        """
        FS.file_list(FS_FileSet&...) — упорядоченное множество по xr_strcmp
        от lower-case имени (LocatorAPI.cpp:997-1055, LocatorAPI_defs.h:73-78).
        Возвращает список имён файлов, подходящих под маску.
        """
        result = set()
        if not os.path.isdir(folder):
            return []
        for entry in os.listdir(folder):
            full = os.path.join(folder, entry)
            if not os.path.isfile(full):
                continue
            low = entry.lower()
            if pattern_match(low, mask.lower()):
                result.add(low)
        return sorted(result, key=_key)

    # --------------------------------------------------- insert_item rules

    def _insert_item(self, tgt, item):
        """
        CInifile::insert_item (Xr_ini.cpp:235-256):
          * префикс '>' (Insert) или '<' (Remove) у ключа — операция над
            CSV-списком, уходит в OverrideModifyListData (xr_ini.h:214-218);
          * иначе — push_back с присвоением insertionIndex = текущий размер.
        """
        key = item.first or ""
        if key:
            op = key[0]
            if op in (">", "<"):
                # Xr_ini.cpp:235-256 insert_item: запись идёт в Section.data
                # (push_back с insertionIndex) И дублируется в
                # OverrideModifyListData (xr_ini.h:214-218). Именно поэтому
                # SortAndFilterSection (410-451) выбирает победителя среди
                # всех >key/<key записей по depth, и только ОДНА выжившая
                # запись доходит до мод-фазы EvaluateSection (1167+).
                lst = self.override_modify_list_data.setdefault(tgt.name, [])
                lst.append(item)
                item.insertion_index = len(lst)
                tgt.data.append(item)
                return
        item.insertion_index = len(tgt.data)
        tgt.data.append(item)

    # ----------------------------------------------- StashCurrentSection

    def _stash_current_section(self, current_base, current_override,
                               current_file_name):
        """CInifile::StashCurrentSection (Xr_ini.cpp:365-408). Точный порт.

        Порядок в движке (важно!):
          1) CurrentBase != NULL: дубликат base -> Debug.fatal (374-378);
             иначе emplace + SectionToFilename (380-384).
          2) CurrentOverride != NULL (386-407):
             a) OverrideData уже есть -> MergeVector + InsertIntoMap(
                OverrideToFilename, sectionName, currentFileName) (390-397);
             b) иначе -> insert(override) и ЕСЛИ BaseData ещё нет —
                предупреждение 'Attempted to override ...' +
                InsertIntoMap(OverrideToFilename, ...) (398-405).
           Предупреждение привязано к ФАКТУ ОТСУТСТВИЯ BASE НА МОМЕНТ СЛЭША,
           а не к наличию ключа в OverrideToFilename."""
        if current_base is not None:
            existing = self.base_data.get(current_base.name)
            if existing is not None:
                # Debug.fatal в движке: дубликат базовой секции без '!' — фатально
                raise RuntimeError(
                    "[DLTX] Duplicate section '%s' wasn't marked as an override. "
                    "Override section by prefixing it with '!' (![%s]). "
                    "Check this file and its DLTX mods: %s, file with section: %s, "
                    "file with duplicate: %s" % (
                        current_base.name, current_base.name, self.file_name,
                        self.section_to_filename.get(current_base.name, "?"),
                        current_file_name))
            self.base_data[current_base.name] = current_base
            self.section_to_filename[current_base.name] = current_file_name

        if current_override is not None:
            existing = self.override_data.get(current_override.name)
            if existing is not None:
                for it in current_override.data:      # MergeVector (xr_vector.h:196)
                    self._insert_item(existing, it)
                self._insert_into_map(self.override_to_filenames,
                                      existing.name, current_file_name)
            else:
                self.override_data[current_override.name] = current_override
                if current_override.name not in self.base_data:   # BaseData.find==end
                    self._warn("Attempted to override section '%s', which doesn't "
                               "exist. Ensure that a base section with the same "
                               "name is loaded first. Check %s, mod file %s" % (
                                   current_override.name, self.file_name,
                                   current_file_name))
                    self._insert_into_map(self.override_to_filenames,
                                          current_override.name, current_file_name)

    @staticmethod
    def _insert_into_map(mp, key, fname):
        """InsertIntoMap<K,V,xr_string> (Xr_ini.cpp:~120-133): std::set-insert
        значения (уникальность), но у нас ordered list для детерминизма."""
        lst = mp.setdefault(key, [])
        if fname not in lst:
            lst.append(fname)

    # ------------------------------------------------------- loadFile

    def _load_file(self, fn, inc_path, name, current_file_name, depth):
        """CInifile::loadFile (Xr_ini.cpp:330-363)."""
        if self.allow_include_func is not None and not self.allow_include_func(fn):
            return
        if not os.path.isfile(fn):
            raise FileNotFoundError("Can't find include file: %s" % name)
        current_file_name[0] = name
        # bIsRootFile=False: движок передаёт false во всех рекурсиях loadFile
        # (Xr_ini.cpp:330-363) — вложенные проходы фазу mod_* НЕ запускают.
        self._ltx_load(fn, inc_path, False, current_file_name, depth)

    # ------------------------------------------------------- LTXLoad

    def _ltx_load(self, reader_path, path, b_is_root_file, current_file_name, depth):
        """
        CInifile::LTXLoad (Xr_ini.cpp:454-906) — однопроходный парсер.
        current_file_name — list из 1 элемента (аналог string_path&).
        """
        current_base = None
        current_override = None
        # xr_unordered_flat_set<shared_str> sectionsMarkedForCreate (Xr_ini.cpp:528):
        # уникальные имена в ПОРЯДКЕ первого добавления (flat-set: find+push_back).
        sections_marked_for_create = OrderedDict()

        # Движок (Xr_ini.cpp:527-543): bHasLoadedModFiles — ЛОКАЛЬНАЯ переменная
        # прохода LTXLoad, НЕ член класса. Фаза mod_* запускается при EOF любого
        # прохода с bIsRootFile==true (вложенные loadFile идут с
        # bIsRootFile=false и фазу не запускают). m_file_name.clear() в движке
        # выполняется ВНУТРИ _LoadModFiles (Xr_ini.cpp:612) после загрузки всех
        # модов; если корневое имя уже пусто — фаза пропускается (545-548).
        b_has_loaded_mod_files = False   # Xr_ini.cpp:~527
        lines = self._read_lines(reader_path)
        i = 0
        n = len(lines)

        while True:
            # ---- конец файла: для корневого файла запускается фаза mod_* ----
            if i >= n:
                if b_is_root_file and not b_has_loaded_mod_files:
                    b_has_loaded_mod_files = True       # Xr_ini.cpp:543
                    self._stash_current_section(current_base, current_override,
                                                current_file_name[0])
                    current_base = current_override = None
                    # Xr_ini.cpp:892-905 (в конце КАЖДОГО LTXLoad-прохода):
                    # секции, помеченные @[...], создаются в BaseData пустыми.
                    for sec_name in list(sections_marked_for_create.keys()):
                        if sec_name not in self.base_data:
                            s = Sect(sec_name)
                            self.base_data[sec_name] = s
                            fnames = self.override_to_filenames.setdefault(sec_name, [])
                            fname = current_file_name[0]
                            if fname not in fnames:          # std::set insert
                                fnames.append(fname)
                            self.section_to_filename[sec_name] = fname
                    # ВАЖНОЕ СООТВЕТСТВИЕ ДВИЖКУ (Xr_ini.cpp:336-341, 546-556):
                    # loadFile передаёт в LTXLoad свой АРГУМЕНТ path как 'folder'
                    # для поиска mod_*.ltx — НЕ директорию текущего файла.
                    # Поэтому рекурсивный инклуд с b_is_root_file=True
                    # (#include "dir\\\\file.ltx" из root, Xr_ini.cpp:716) ищет
                    # моды в КОРНЕВОЙ папке, а не в dir\\.
                    # m_file_name.empty() -> skip (Xr_ini.cpp:545-548);
                    # очистка имени — внутри _load_mod_files (612).
                    self._load_mod_files(current_file_name, path)
                    # После фазы цикл движка делает continue при F->eof()==true,
                    # условие while ложно -> переход к финальному слэшу (884+).
                    break
                else:
                    pass  # финальный слэш и @[...] — ниже, после цикла

            line = _trim(lines[i]) if i < n else ""
            if i >= n:
                break        # некорневой проход: выходим к финальному слэшу (884-905)
            i += 1

            # ---- комментарии: ';' и '//' (Xr_ini.cpp:618-651) ----
            comm = line.find(";")
            comm1 = line.find("/")
            if comm1 != -1 and comm1 + 1 < len(line) and line[comm1 + 1] == "/" \
                    and (comm == -1 or comm1 < comm):
                comm = comm1
            if comm != -1:
                # комментарий внутри кавычек не режется (Xr_ini.cpp:632-650)
                q1 = line.find('"')
                in_quot = False
                if q1 != -1 and q1 < comm:
                    q2 = line.find('"', q1 + 1)
                    if q2 != -1 and q2 > comm:
                        in_quot = True
                if not in_quot:
                    line = _trim(line[:comm])

            # ---- #include (Xr_ini.cpp:670-720) ----
            if line and line[0] == "#" and "#include" in line:
                inc_name = _get_item(line, 1)
                if inc_name:
                    folder_dir, base = os.path.split(os.path.normpath(inc_name.replace("\\", "/")))
                    fn = os.path.join(path, inc_name.replace("\\", "/"))
                    inc_path = os.path.join(os.path.dirname(fn), "")
                    if "*.ltx" in inc_name:
                        # wildcard-инклуд: обход FS_FileSet по xr_strcmp (681-704)
                        for nm in self._file_list(inc_path, os.path.basename(inc_name)):
                            self._load_file(os.path.join(inc_path, nm), inc_path,
                                            nm, current_file_name, depth + 1)
                    else:
                        self._load_file(fn, inc_path, inc_name, current_file_name, depth + 1)
                continue

            # ---- !![sec] — удаление секции (Xr_ini.cpp:721-740) ----
            if line and line.startswith("!!["):
                self._stash_current_section(current_base, current_override,
                                            current_file_name[0])
                current_base = current_override = None
                close = line.find("]")
                sec = line[3:close].lower()
                self._log("[DLTX] [%s] Encountered %s, mark section to delete" % (self.file_name, line))
                self.sections_to_delete.add(sec)
                continue

            # ---- [sec], ![sec], @[sec], возможно [:parent1,parent2] ----
            is_override = line.startswith("![")           # isOverrideSection (655-658)
            is_safe = line.startswith("@[")               # isSafeOverrideSection (660-663)
            if line and (line[0] == "[" or is_override or is_safe):
                self._stash_current_section(current_base, current_override,
                                            current_file_name[0])
                current_base = current_override = None

                start = 2 if (is_override or is_safe) else 1
                # SecName = substr(start, strchr(str,']')-str-start), lower (Xr_ini.cpp:750-756).
                # ВНИМАНИЕ (квирк движка): strchr находит ПЕРВЫЙ ']', поэтому для
                # base-секции "[name:parent]" хвост ":parent" остаётся ЧАСТЬЮ имени
                # секции (BaseData key = "child:sup"), а наследование отдельно
                # распознаётся через strstr(str, "]:") (Xr_ini.cpp:785-808).
                # Для override ("![", "@[") используется !isModSection(&str[2])
                # (xrstring.h:393-396) — первый ']' ПОСЛЕ позиции 2, т.е. закрывающая
                # скобка, и хвост ":..." в имя не попадает.
                close_bracket = line.find("]", 2) if (is_override or is_safe) \
                    else line.find("]")
                if close_bracket == -1:
                    raise RuntimeError("Bad ini section found: %s" % line)   # Xr_ini.cpp:784
                sec_full = line[start:close_bracket]
                sec_name = sec_full.lower()

                b_is_override = False
                if is_override:
                    b_is_override = True
                elif is_safe:
                    b_is_override = True
                    if sec_name not in self.base_data:
                        sections_marked_for_create[sec_name] = True

                sect = Sect(sec_name)
                if b_is_override:
                    current_override = sect
                else:
                    current_base = sect

                # наследование: strstr(str, "]:") — ТОЛЬКО непосредственно "]" + ":"
                # (Xr_ini.cpp:785-808). Хвост ":parents" при этом остаётся частью
                # имени секции (квирк substr/strchr, строка 751) — см. выше.
                inherit_pos = line.find("]:")
                if inherit_pos != -1:
                    parents_str = line[inherit_pos + 2:]
                    parents = [_trim(_get_item(parents_str, k)).lower()
                               for k in range(_get_item_count(parents_str))]
                    parents = [p for p in parents if p]
                    target_map = (self.override_parent_data_map if b_is_override
                                  else self.base_parent_data_map)
                    cur = target_map.setdefault(sec_name, [])
                    self._merge_parent_set(cur, parents, True)
                continue

            # ---- key = value ----
            if line and line[0] != ";":
                b_is_delete = line[0] == "!"              # удаление ключа (815)
                name_part = line[1:] if b_is_delete else line
                eq = name_part.find("=")
                if eq != -1:
                    name = _trim(name_part[:eq])
                    raw_value = name_part[eq + 1:]
                    value, inside = _parse_value(raw_value)
                    # многострочные значения с нечётной кавычкой (Xr_ini.cpp:827-851)
                    while inside and i < n:
                        raw_value += "\r\n" + lines[i]
                        i += 1
                        value, inside = _parse_value(raw_value)
                else:
                    name = _trim(name_part)
                    value = ""

                item = Item()
                item.first = name if name else None
                if item.first is None:
                    self._log("~[DLTX] WARNING: Malformed line %s in file %s" %
                              (line, current_file_name[0]))
                    continue
                # Xr_ini.cpp:867 I.second = bIsDelete ? DLTX_DELETE : (str2[0] ? str2 : NULL).
                # Точное соответствие движку: '!' (даже без '=') -> токен DELETE;
                # строка БЕЗ '=' вообще -> None (в движке это separate if-ветка, 819-857);
                # 'key =' (пустое после '=') -> "" — непустой указатель shared_str,
                # НЕ нормализуется к NULL.
                item.second = (DLTX_DELETE if b_is_delete
                               else (value if value else DLTX_EMPTY))
                item.filename = os.path.splitext(current_file_name[0].lower())[0]  # 869-870
                item.depth = depth

                # Xr_ini.cpp:873 if (*I.first || *I.second): в движке условие всегда
                # истинно при непустом имени ключа; для пустого имени пропускаем.
                # Xr_ini.cpp:873 if (*I.first || *I.second): непустой указатель first
                # истинен ВСЕГДА — условие тавтологично; запись происходит даже
                # при second==NULL (движок хранит Item со значением NULL).
                if True:
                    if current_base is not None:
                        self._insert_item(current_base, Item(item.first, item.second,
                                                             item.filename, depth))
                    if current_override is not None:
                        self._insert_item(current_override, Item(item.first, item.second,
                                                                 item.filename, depth))
                continue

        self._stash_current_section(current_base, current_override, current_file_name[0])

        # ---- пустые секции, помеченные @[, так и не созданные (Xr_ini.cpp:892-905) ----
        # Движок: CurrentBase = xr_new<Sect>(SecName); BaseData.emplace(...);
        # OverrideToFilename.insert(currentFileName); SectionToFilename = ...
        # ВАЖНО: CurrentOverride к этому моменту уже stash-нут (строка 885),
        # поэтому ключи @[секции оседают только в OverrideData — базовая
        # секция остаётся ПУСТОЙ; победитель определяется MergeSections.
        for sec_name in sections_marked_for_create:
            if sec_name not in self.base_data:
                s = Sect(sec_name)
                self.base_data[sec_name] = s
                fnames = self.override_to_filenames.setdefault(sec_name, [])
                fname = current_file_name[0]
                if fname not in fnames:                      # std::set insert
                    fnames.append(fname)
                self.section_to_filename[sec_name] = fname

    # --------------------------------------------------- mod_* autoload

    def _load_mod_files(self, current_file_name, folder):
        """
        Автозагрузка mod_system_* / mod_<root>_*.ltx (Xr_ini.cpp:546-614):
          маска "mod_<ИмяКорня>_*.ltx"; глубина первого мода -200, каждого
          следующего ещё на -200 (d += dt, dt=-200) => раньше в алфавитном
          порядке = важнее.
          Фильтр bIsModfileMeantForMe (577-598): мод пропускается, если он
          полностью матчится на "mod_<ambiguous>_.+.ltx" для любого другого
          файла "<root>_*.ltx" в той же папке (чтобы mod_logs_xxx.ltx не
          грузился вместе с system.ltx, когда есть logs.ltx).
        """
        # Xr_ini.cpp:549-561: цикл движка идёт по маске "<m_file_name>_*.ltx"
        # (шаблон = имя корня + '_' + "*"+расширение). stem в нижний регистр —
        # как strlwr перед PatternMatch (573-580; имена FS_FileSet хранятся
        # lower-case, LocatorAPI.cpp:1023).
        stem = os.path.splitext(current_file_name[0])[0].lower()   # m_file_name (546)
        ambiguous = {os.path.splitext(f)[0]
                     for f in self._file_list(folder, stem + "_*.ltx")}
        mod_files = self._file_list(folder, "mod_" + stem + "_*.ltx")

        d = MOD_DEPTH_STEP
        dt = MOD_DEPTH_STEP
        for mod_name in mod_files:
            skip = False
            for amb in ambiguous:
                if amb == stem:
                    continue
                pat = "mod_" + amb + "_*.ltx"
                # IsFullRegexMatch("mod_<amb>_.+.ltx") — хотя бы 1 символ после '_'
                head = "mod_" + amb + "_"
                if mod_name.startswith(head) and len(mod_name) > len(head) + 4 \
                        and pattern_match(mod_name, pat):
                    skip = True
                    break
            if skip:
                continue
            self._load_file(os.path.join(folder, mod_name), folder,
                            mod_name, current_file_name, d)
            d += dt   # Xr_ini.cpp:601-611: шаг -200; первый мод depth=-200

    # -------------------------------------------------- MergeParentSet

    @staticmethod
    def _merge_parent_set(parents_base, parents_override, include_removers):
        """CInifile::MergeParentSet (Xr_ini.cpp:300-328). Родители с '!' удаляются."""
        for cur in parents_override:
            is_removal = cur.startswith("!")
            stale = ("" if is_removal else "!") + (cur[1:] if is_removal else cur)
            parents_base[:] = [p for p in parents_base if p != stale]
            if include_removers or not is_removal:
                parents_base.append(cur)

    # -------------------------------------------- SortAndFilterSection

    def _sort_and_filter_section(self, sect):
        """
        CInifile::SortAndFilterSection (Xr_ini.cpp:410-451):
        сортировка (ключ ↑, depth ↑, insertionIndex ↓), затем оставляется
        ПЕРВЫЙ элемент каждой группы равных ключей:
          * минимальный depth побеждает;
          * при равном depth — последний вставленный (max insertionIndex).
        """
        if len(sect.data) < 2:
            return
        # Компаратор Xr_ini.cpp:410-451 (проверен по тексту): sort by
        # (ключ ↑ xr_strcmp, depth ↑, insertionIndex ↓), затем unique_copy
        # оставляет ПЕРВОГО в группе равных ключей => победитель:
        # минимальный depth; при равенстве глубин — ПОСЛЕДНЯЯ вставка.
        sect.data.sort(key=lambda it: (_key(it.first or ""),
                                       it.depth,
                                       -it.insertion_index))
        result = []
        j = 0
        while j < len(sect.data):
            k = sect.data[j].first
            result.append(sect.data[j])                 # keep FIRST per key (424-431)
            j += 1
            while j < len(sect.data) and sect.data[j].first == k:
                j += 1
        sect.data = result
        # Xr_ini.cpp:410-451 действует и на Section.data, и на параллельный
        # список OverrideModifyListData той же секции (записи >/< дублируются
        # в оба контейнера — insert_item 235-256). Применяем тот же
        # sort+keep-first к модификаторам списков.
        mods = self.override_modify_list_data.get(sect.name)
        if mods and len(mods) > 1:
            mods.sort(key=lambda it: (_key(it.first or ""),
                                      it.depth,
                                      -it.insertion_index))
            mres = []
            j = 0
            while j < len(mods):
                k = mods[j].first
                mres.append(mods[j])
                j += 1
                while j < len(mods) and mods[j].first == k:
                    j += 1
            self.override_modify_list_data[sect.name] = mres

    # --------------------------------------------------- MergeSections

    def _merge_sections(self, base_items, override_items, deleted_items,
                        is_merging_base_and_mod):
        """
        CInifile::MergeSections (Xr_ini.cpp:908-1002). Точный порт merge-цикла.
        Оба входа отсортированы по xr_strcmp ключей (917-921 — СЕГОДНЯ это
        побайтовый strcmp; парсер НЕ понижает регистр ключей).

        Семантика токена DLTX_DELETE в движке (xrstring.h:182 — сравнение
        указателей interned-строк; токен пишет только парсер, Xr_ini.cpp:867):

          * cmp < 0 (только base)               -> push_back(base)          (941-945)
          * cmp > 0 (только override):                                        (946-966)
              - DELETE: если base+mod -> DeletedItems.insert(key)  (951-957)
                        (ключ мог прийти из ДРУГОГО мода/родителя — удаляется);
              - иначе -> push_back(override).
          * cmp == 0 (ключ в обоих):                                           (967-993)
              - override==DELETE:
                    base+mod : push_back(base)  (975-980) — base-запись
                               СОХРАНЯЕТСЯ; реальное удаление !key произойдёт
                               позже: EvaluateSection вычитает из DeletedItems
                               все ключи, присутствующие в ResolvedBaseAndMods
                               (Xr_ini.cpp:1206-1211). Итог: !key из мода,
                               объявленный в ТОМ ЖЕ merge, что и base-ключ,
                               ключ НЕ удаляет (квирк движка).
                    parent+base: base остаётся (981-984).
              - иначе: push_back(override), base подавлен           (985-992).
        """
        result = []
        b = sorted(base_items, key=lambda it: it.first or "")
        o = sorted(override_items, key=lambda it: it.first or "")
        bi = oi = 0
        while bi < len(b) or oi < len(o):
            if bi == len(b):
                # Хвост override: ключа нет ни в одной позиции base-слияния.
                # DELETE здесь нечего удалять (в т.ч. !key, чья strcmp-позиция
                # правее всех base-ключей — см. REGRESSION.txt, кейс sup.b).
                ov = o[oi]
                if ov.second is DLTX_DELETE:
                    pass
                else:
                    result.append(ov)
                oi += 1
                continue
            if oi == len(o):
                result.append(b[bi])
                bi += 1
                continue
            cmp = _xr_strcmp(b[bi].first or "", o[oi].first or "")
            if cmp < 0:
                result.append(b[bi]); bi += 1
            elif cmp > 0:
                ov = o[oi]
                if ov.second is DLTX_DELETE:
                    if is_merging_base_and_mod:
                        deleted_items.add(ov.first)     # 951-957
                else:
                    result.append(ov)
                oi += 1
            else:
                ov = o[oi]
                if ov.second is DLTX_DELETE:
                    result.append(b[bi])                # 975-984: base сохраняется
                else:
                    result.append(ov)                   # 985-992: override побеждает
                oi += 1
                bi += 1
        return result


    def _evaluate_section(self, section_name, resolved_cache, recursion_stack):
        """
        CInifile::EvaluateSection (Xr_ini.cpp:1004-1295):
        результат = merge( merge(parents...), merge(base_section, overrides) )
        затем применяются > / < модификаторы списков.
        Циклическое наследование — фатальная ошибка (1016-1024).
        """
        if section_name in resolved_cache:
            return resolved_cache[section_name]
        if section_name in recursion_stack:
            raise RuntimeError("[DLTX] Section '%s' has cyclical dependencies. Cycle: %s"
                               % (section_name, " -> ".join(recursion_stack)))
        recursion_stack.append(section_name)

        base_parents = self.base_parent_data_map.get(section_name)
        override_parents = self.override_parent_data_map.get(section_name)

        if override_parents is not None and base_parents is None:
            base_parents = []
            self.base_parent_data_map[section_name] = base_parents
            self._merge_parent_set(base_parents, list(override_parents), False)
        elif base_parents is not None and override_parents is not None:
            self._merge_parent_set(base_parents, list(override_parents), False)

        resolved_parents = []
        deleted_items = set()
        for parent in (base_parents or []):
            # Xr_ini.cpp:1059-1089: имена родителей используются КАК ЕСТЬ
            # (без lower()); GetParentsSetFromString/_GetItem не понижают регистр
            # (xr_trims.cpp:74-81). Родитель "!Name" с заглавной N НЕ совпадёт с
            # удалителем "name" — воспроизводим это буквально.
            pname = parent[1:] if parent.startswith("!") else parent
            if pname not in self.base_data:
                if pname in self.override_data:
                    self._warn("Section '%s' has parent '%s' that is defined as Override. "
                               "Creating parent for backwards compatibility." % (section_name, pname))
                    s = Sect(pname)
                    self.base_data[pname] = s
                else:
                    if self.print_dltx_warnings:
                        self._warn("Section '%s' inherits from non-existent section '%s'. "
                               "Creating fallback empty parent section." % (section_name, pname))
                    s = Sect(pname)
                    self.base_data[pname] = s
            # движок: BaseData[parent] создаёт пустую секцию при отсутствии
            # (operator[]), а EvaluateSection(parent) вызывается ВСЕГДА (1088)
            parent_data = self._evaluate_section(pname, resolved_cache, recursion_stack)
            resolved_parents = self._merge_sections(resolved_parents, parent_data,
                                                    deleted_items, False)

        resolved_base_and_mods = list(self.base_data[section_name].data) \
            if section_name in self.base_data else []
        ov = self.override_data.get(section_name)
        if ov is not None:
            resolved_base_and_mods = self._merge_sections(resolved_base_and_mods,
                                                          ov.data, deleted_items, True)
            del self.override_data[section_name]

        current_result = self._merge_sections(resolved_parents, resolved_base_and_mods,
                                              deleted_items, False)
        # Движок (Xr_ini.cpp:1291-1294): результат кладётся в кэш как Section*
        # с полем Name; у нас Sect(name). Дальнейшие CSV-моды мутируют
        # current_result.data на месте — это безопасно, т.к. EvaluateSection
        # для разных секций независим (каждая пересобирается из своих родителей).
        sect_obj = Sect(section_name)
        sect_obj.data = current_result
        current_result = sect_obj

        # ---- операции > / < над CSV-списками (Xr_ini.cpp:1167-1290) ----
        mods = self.override_modify_list_data.get(section_name)
        if mods:
            # Xr_ini.cpp:1216-1224: sort by (*a.first)+1 (ключ всегда непустой —
            # начинается с >/<), затем insertionIndex ASC ("preserve file order",
            # в отличие от SortAndFilterSection).
            mods_sorted = sorted(mods, key=lambda it: (_key(it.first[1:]),
                                                       it.insertion_index))
            result = []
            cur = sorted(current_result, key=lambda it: _key(it.first or ""))
            di = mi = 0
            while di < len(cur) or mi < len(mods_sorted):
                # Xr_ini.cpp:1233-1237: модификация с ПУСТЫМ значением пропускается.
                # В движке условие mod_it->second == NULL; у нас NULL-подобные
                # значения не доходят сюда (парсер пишет "" или токен), поэтому
                # единственная корректная интерпретация — пропуск токена DELETE.
                # Движок (1233-1237): if (*mod_it == end || mod_it->second == NULL)
                # { ++mod_it; continue; } — пропускаются ТОЛЬКО записи с NULL-
                # значением ('key =' без значения -> EMPTY). Токен DELETE у
                # >/< мода НЕ пропускается: он даёт ''-add/remove (см. ниже),
                # как в движке (там DELETE != NULL).
                if mi < len(mods_sorted) and mods_sorted[mi].second is DLTX_EMPTY:
                    mi += 1
                    continue
                if mi < len(mods_sorted) and di < len(cur):
                    mod_key = mods_sorted[mi].first[1:]
                    c = _xr_strcmp(cur[di].first, mod_key)
                    active_key = cur[di].first if c <= 0 else mod_key
                elif mi < len(mods_sorted):
                    active_key = mods_sorted[mi].first[1:]
                else:
                    active_key = cur[di].first

                existing = None
                if di < len(cur) and cur[di].first == active_key:
                    existing = cur[di]

                if mi < len(mods_sorted) and mods_sorted[mi].first[1:] == active_key:
                    working = None
                    exists_in_output = False
                    if existing is not None:
                        working = Item(existing.first, existing.second,
                                       existing.filename, existing.depth)
                        # В движке условие cur_it->first || cur_it->second (1258)
                        # истинно всегда при непустом имени ключа => запись
                        # считается присутствующей даже с DELETE/NULL-значением.
                        exists_in_output = True
                        di += 1
                    else:
                        if active_key not in deleted_items:
                            working = Item(active_key, "", "")
                            exists_in_output = True
                    while mi < len(mods_sorted) and mods_sorted[mi].first[1:] == active_key:
                        m = mods_sorted[mi]
                        # Xr_ini.cpp:1271: mod_it->second != NULL
                        if exists_in_output and m.second is not None:
                            op = m.first[0]
                            items_vec = self._split_list(working.second
                                                          if isinstance(
                                                              working.second, str)
                                                          else "")
                            add_vec = self._split_list(m.second)
                            if op == ">":
                                items_vec.extend(add_vec)
                            elif op == "<":
                                items_vec = [x for x in items_vec if x not in add_vec]
                            working.second = self._join_list(items_vec)
                            working.filename = m.filename
                        mi += 1
                    if exists_in_output and working.second:
                        result.append(working)
                else:
                    if existing is not None:
                        result.append(existing)
                        di += 1
            current_result = result

        recursion_stack.pop()
        # Движок (Xr_ini.cpp:1293): RResultIt = ResultData
        #       .insert(make_pair(Name, Section)).first —
        # в кэш попадает ВСЕГДА актуальный Sect с полным именем секции.
        final_sect = Sect(section_name)
        final_sect.data = list(current_result) if not isinstance(current_result, Sect) \
            else current_result.data
        resolved_cache[section_name] = final_sect
        return final_sect

    @staticmethod
    def _split_list(s, delimiter=","):
        """split_list (Xr_ini.cpp:1101-1138): trim, пропуск пустых."""
        if not s:
            return []
        return [t.strip() for t in s.split(delimiter) if t.strip()]

    @staticmethod
    def _join_list(vec, delimiter=","):
        """join_list (Xr_ini.cpp:1141-1165): склейка БЕЗ пробела после разделителя."""
        return delimiter.join(vec)

    # ------------------------------------------------------------- Load

    def load(self):
        """CInifile::Load (Xr_ini.cpp:1297-1420) + кэш конструктора (189-206)."""
        cache_key = self.root_path.lower()
        if self.use_cache and self.file_name:
            if cache_key in self._cache:
                self._log("[DLTX] [%s] Found data in cache" % self.file_name)
                self.data = self._cache[cache_key]
                return self

        current_file_name = [self.file_name]                  # 1306-1317
        folder = os.path.dirname(self.root_path)
        self._ltx_load(self.root_path, folder, True, current_file_name, 0)   # 1322-1331

        for sect in self.base_data.values():                   # 1333-1337
            self._sort_and_filter_section(sect)
        for sect in self.override_data.values():
            self._sort_and_filter_section(sect)

        resolved = {}                                          # 1339-1351
        # Движок копирует ключи BaseData в RStringVec и обходит его (1344-1352).
        # BaseData — xr_unordered_flat_map (hash-порядок); детерминированный
        # аналог в Python — sorted по имени (секции независимо рекурсируют,
        # результат не зависит от порядка обхода).
        for name in sorted(list(self.base_data.keys()), key=_key):
            self._evaluate_section(name, resolved, [])

        for s in sorted(self.sections_to_delete, key=_key):    # 1354-1370
            self._log("[DLTX] [%s] Found section %s to delete" % (self.file_name, s))
            if s in resolved:
                del resolved[s]
                if s in self.override_data:
                    del self.override_data[s]

        # InsertIntoDATA (136-147): сортировка секций по имени (strcmp)
        self.data = sorted(((k, v) for k, v in resolved.items()),
                           key=lambda kv: _key(kv[0]))

        if self.use_cache and self.file_name:                  # 1375-1381
            self._cache[cache_key] = self.data

        # ВНИМАНИЕ: финального прохода с предупреждениями в движке НЕТ.
        # 'Attempted to override...' печатается ровно один раз за слэш секции
        # в StashCurrentSection (Xr_ini.cpp:400-404) — см. выше.
        # cleanup (1402-1419)
        self.override_to_filenames.clear()
        self.section_to_filename.clear()
        self.sections_to_delete.clear()
        self.base_parent_data_map.clear()
        self.base_data.clear()
        self.override_parent_data_map.clear()
        self.override_data.clear()
        self.override_modify_list_data.clear()
        return self

    # ---------------------------------------------------------- queries

    def section_exist(self, name):
        name = name.lower()
        lo, hi = 0, len(self.data)
        while lo < hi:
            mid = (lo + hi) // 2
            if _xr_strcmp(self.data[mid][0], name) < 0:
                lo = mid + 1
            else:
                hi = mid
        return lo < len(self.data) and self.data[lo][0] == name

    def r_section(self, name):
        """Бинарный поиск по отсортированному Root DATA (xr_strcmp-порядок).
        Аналог xr_unordered_map::find в движке, но у нас data отсортирован
        (Xr_ini.cpp:1390-1400 — секции складываются в упорядоченный контейнер).
        Шаблон идентичен section_exist."""
        name = name.lower()
        lo, hi = 0, len(self.data)
        while lo < hi:
            mid = (lo + hi) // 2
            if _xr_strcmp(self.data[mid][0], name) < 0:
                lo = mid + 1
            else:
                hi = mid
        if lo < len(self.data) and self.data[lo][0] == name:
            return self.data[lo][1]
        # Движок: CInifile::r_section возвращает NULL при отсутствии секции
        # (xr_ini.h), а не бросает исключение.
        return None

    def line_exist(self, sec, key):
        sect = self.r_section(sec)
        if sect is None:
            return False
        for it in sect.data:
            if it.first == key:
                return True
        return False

    def r_string(self, sec, key):
        sect = self.r_section(sec)
        if sect is not None:
            for it in sect.data:
                if it.first == key:
                    return it.second
        raise KeyError("Cannot find line %s:%s" % (sec, key))

    def winner_of(self, sec, key):
        """Отладковый запрос: полный «победитель» ключа.
        Возвращает кортеж (filename, depth, insertion_index) или None.
        Соответствует первой записи после SortAndFilterSection
        (Xr_ini.cpp:410-451: ключ↑, depth↑, insertionIndex↓),
        т.к. self.data уже отсортирован и дубликаты ключей схлопнуты."""
        section = self.r_section(sec)
        if section is None:
            return None
        for it in section.data:
            if it.first == key:
                return (it.filename, it.depth, it.insertion_index)
        return None

    def as_dict(self):
        out = OrderedDict()
        for name, sect in self.data:
            out[name] = OrderedDict((it.first, it.second) for it in sect.data)
        return out

    @staticmethod
    def _read_lines(path):
        """Чтение строк файла. Движок читает байты (IReader::r_string);
        игровые .ltx часто в Windows-1251, поэтому: сначала UTF-8 строго,
        при UnicodeDecodeError — cp1251 с заменой; BOM снимается."""
        with open(path, "rb") as f:
            raw = f.read()
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("windows-1251", errors="replace")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return text.split("\n")
