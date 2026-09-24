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

DLTX_DELETE = "DLTX_DELETE"          # Xr_ini.cpp:465
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
    rx = "^" + "".join(".*" if c == "*" else re.escape(c) for c in mask.lower()) + "$"
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
    """CInifile::Sect (xr_ini.h:68-74)."""
    __slots__ = ("name", "data")

    def __init__(self, name=""):
        self.name = name
        self.data = []


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
                lst = self.override_modify_list_data.setdefault(tgt.name, [])
                lst.append(item)
                item.insertion_index = len(lst)
                return
        item.insertion_index = len(tgt.data)
        tgt.data.append(item)

    # ----------------------------------------------- StashCurrentSection

    def _stash_current_section(self, current_base, current_override, current_file_name):
        """CInifile::StashCurrentSection (Xr_ini.cpp:365-408)."""
        if current_base is not None:
            existing = self.base_data.get(current_base.name)
            if existing is not None:
                # Debug.fatal в движке: дубликат базовой секции без '!' — фатальная ошибка
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
                # слияние повторного override: каждый item вставляется в существующий
                for it in current_override.data:
                    self._insert_item(existing, it)
                self.override_to_filenames.setdefault(existing.name, []).append(current_file_name)
            else:
                self.override_data[current_override.name] = current_override
                self.override_to_filenames.setdefault(current_override.name, []).append(current_file_name)

    # ------------------------------------------------------- loadFile

    def _load_file(self, fn, inc_path, name, current_file_name, depth):
        """CInifile::loadFile (Xr_ini.cpp:330-363)."""
        if self.allow_include_func is not None and not self.allow_include_func(fn):
            return
        if not os.path.isfile(fn):
            raise FileNotFoundError("Can't find include file: %s" % name)
        current_file_name[0] = name
        self._ltx_load(fn, inc_path, False, current_file_name, depth)

    # ------------------------------------------------------- LTXLoad

    def _ltx_load(self, reader_path, path, b_is_root_file, current_file_name, depth):
        """
        CInifile::LTXLoad (Xr_ini.cpp:454-906) — однопроходный парсер.
        current_file_name — list из 1 элемента (аналог string_path&).
        """
        current_base = None
        current_override = None
        sections_marked_for_create = set()

        lines = self._read_lines(reader_path)
        i = 0
        n = len(lines)
        mod_phase_done = False

        while True:
            # ---- конец файла: для корневого файла запускается фаза mod_* ----
            if i >= n:
                if b_is_root_file and not mod_phase_done:
                    self._stash_current_section(current_base, current_override,
                                                current_file_name[0])
                    current_base = current_override = None
                    mod_phase_done = True
                    if not self.file_name:
                        break
                    self._load_mod_files(current_file_name, os.path.dirname(self.root_path))
                break

            line = _trim(lines[i])
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
                # SecName = substr(start, strchr(str,']')-str-start) (Xr_ini.cpp:750-751).
                # Для "[name:parents]" strchr находит ПЕРВЫЙ ']', поэтому движок
                # делает имя секции ВМЕСТЕ с хвостом ":parents" (особенность/квирк
                # DLTX-парсера); для ![ / @[ хвост ":..." в имя не попадает.
                close_bracket = line.find("]")
                if close_bracket == -1:
                    raise RuntimeError("Bad ini section found: %s" % line)   # Xr_ini.cpp:784
                inherit_pos = line.find(":", close_bracket)
                name_end = len(line) if (inherit_pos != -1 and not (is_override or is_safe)) \
                    else close_bracket
                sec_full = line[start:name_end]
                sec_name = sec_full.lower()

                b_is_override = False
                if is_override:
                    b_is_override = True
                elif is_safe:
                    b_is_override = True
                    if sec_name not in self.base_data:
                        sections_marked_for_create.add(sec_name)

                sect = Sect(sec_name)
                if b_is_override:
                    current_override = sect
                else:
                    current_base = sect

                # наследование: ]:parent,parent,... (Xr_ini.cpp:786-808)
                inherited = line[close:].find(":")
                if line[close:].startswith(":"):
                    parents_str = line[close + 1:]
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
                item.second = DLTX_DELETE if b_is_delete else (value if value else None)
                item.filename = os.path.splitext(current_file_name[0].lower())[0]  # 869-870
                item.depth = depth

                if item.first or item.second:
                    if current_base is not None:
                        self._insert_item(current_base, Item(item.first, item.second,
                                                             item.filename, depth))
                    if current_override is not None:
                        self._insert_item(current_override, Item(item.first, item.second,
                                                                 item.filename, depth))
                continue

        self._stash_current_section(current_base, current_override, current_file_name[0])

        # ---- пустые секции, помеченные @[, так и не созданные (Xr_ini.cpp:892-905) ----
        for sec_name in sections_marked_for_create:
            if sec_name not in self.base_data:
                s = Sect(sec_name)
                self.base_data[sec_name] = s
                self.override_to_filenames.setdefault(sec_name, []).append(current_file_name[0])
                self.section_to_filename[sec_name] = current_file_name[0]

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
        stem = os.path.splitext(self.file_name)[0].lower()
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
            d += dt

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
        sect.data.sort(key=lambda it: (_key(it.first or ""),
                                       it.depth,
                                       -it.insertion_index))
        result = []
        j = 0
        while j < len(sect.data):
            k = sect.data[j].first
            result.append(sect.data[j])
            j += 1
            while j < len(sect.data) and sect.data[j].first == k:
                j += 1
        sect.data = result

    # --------------------------------------------------- MergeSections

    def _merge_sections(self, base_items, override_items, deleted_items,
                        is_merging_base_and_mod):
        """
        CInifile::MergeSections (Xr_ini.cpp:908-1002). Оба входа должны быть
        отсортированы по ключу (как std-векторы после SortAndFilter... нет —
        здесь это merge двух отсортированных по первому полю последовательностей).
        Правила:
          * !key (DLTX_DELETE) в override:
              - base+mod  : ключ удаляется совсем (попадает в DeletedItems);
              - parent+base: ключ остаётся со значением из base (защита от
                удаления унаследованного значения самим родителем).
          * коллизия — побеждает override (значение).
        """
        result = []
        b = sorted(base_items, key=lambda it: _key(it.first or ""))
        o = sorted(override_items, key=lambda it: _key(it.first or ""))
        bi = oi = 0
        while bi < len(b) or oi < len(o):
            if bi == len(b):
                ov = o[oi]
                if ov.second == DLTX_DELETE:
                    if is_merging_base_and_mod:
                        deleted_items.add(ov.first)
                else:
                    result.append(ov)
                oi += 1
                continue
            if oi == len(o):
                result.append(b[bi])
                bi += 1
                continue
            cmp = _xr_strcmp(b[bi].first, o[oi].first)
            if cmp < 0:
                result.append(b[bi]); bi += 1
            elif cmp > 0:
                ov = o[oi]
                if ov.second == DLTX_DELETE:
                    if is_merging_base_and_mod:
                        deleted_items.add(ov.first)
                else:
                    result.append(ov)
                oi += 1
            else:
                ov = o[oi]
                if ov.second == DLTX_DELETE:
                    if is_merging_base_and_mod:
                        deleted_items.add(ov.first)
                    else:
                        result.append(b[bi])
                else:
                    result.append(ov)
                oi += 1
                bi += 1
        return result

    # -------------------------------------------------- EvaluateSection

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
            pname = parent[1:].lower() if parent.startswith("!") else parent.lower()
            if pname not in self.base_data:
                if pname in self.override_data:
                    self._warn("Section '%s' has parent '%s' that is defined as Override. "
                               "Creating parent for backwards compatibility." % (section_name, pname))
                    s = Sect(pname)
                    self.base_data[pname] = s
                else:
                    self._warn("Section '%s' inherits from non-existent section '%s'. "
                               "Creating fallback empty parent section." % (section_name, pname))
                    s = Sect(pname)
                    self.base_data[pname] = s
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

        # ---- операции > / < над CSV-списками (Xr_ini.cpp:1167-1290) ----
        mods = self.override_modify_list_data.get(section_name)
        if mods:
            mods_sorted = sorted(mods, key=lambda it: (_key((it.first or "")[1:]),
                                                       it.insertion_index))
            result = []
            cur = sorted(current_result, key=lambda it: _key(it.first or ""))
            di = mi = 0
            while di < len(cur) or mi < len(mods_sorted):
                if mi < len(mods_sorted) and mods_sorted[mi].second is None:
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
                        exists_in_output = True
                        di += 1
                    else:
                        if active_key not in deleted_items:
                            working = Item(active_key, "", "")
                            exists_in_output = True
                    while mi < len(mods_sorted) and mods_sorted[mi].first[1:] == active_key:
                        m = mods_sorted[mi]
                        if exists_in_output and m.second is not None:
                            op = m.first[0]
                            items_vec = self._split_list(working.second)
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
        resolved_cache[section_name] = current_result
        return current_result

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
        for name in list(self.base_data.keys()):
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

        for k in self.override_data:                           # 1384-1400
            for fname in self.override_to_filenames.get(k, []):
                self._warn("Attempted to override section '%s', which doesn't exist. "
                           "Ensure that a base section with the same name is loaded "
                           "first. Check %s, mod file %s" % (k, self.file_name, fname))

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
        name = name.lower()
        for k, v in self.data:
            if k == name:
                return v
        raise KeyError("Can't open section '%s'" % name)

    def line_exist(self, sec, key):
        if not self.section_exist(sec):
            return False
        for it in self.r_section(sec).data:
            if it.first == key:
                return True
        return False

    def r_string(self, sec, key):
        for it in self.r_section(sec).data:
            if it.first == key:
                return it.second
        raise KeyError("Cannot find line %s:%s" % (sec, key))

    def winner_of(self, sec, key):
        """Отладковый запрос: какой физический файл дал значение ключа."""
        return self.r_section(sec) and next(
            (it.filename for it in self.r_section(sec).data if it.first == key), None)

    def as_dict(self):
        out = OrderedDict()
        for name, sect in self.data:
            out[name] = OrderedDict((it.first, it.second) for it in sect.data)
        return out

    @staticmethod
    def _read_lines(path):
        with open(path, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8", errors="replace")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return text.split("\n")
