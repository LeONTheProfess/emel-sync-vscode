#!/usr/bin/env python3
"""emel_sync - зеркало классов EMEL в git-репозитории (БД -> файлы -> git).

  python emel_sync.py init <папка> --url http://хост/api --token ТОКЕН
  python emel_sync.py sync [папка] [--dry-run] [--full] [--workers N]
                           [--no-commit] [--force] [--allow-dirty]

Что делает sync:
  1. запрашивает список классов (id, parent_id, full_name, modified);
  2. сравнивает с .emel/index.json: новые, изменённые, удалённые, перемещённые;
  3. скачивает только изменённые классы (конструктор + методы);
  4. раскладывает файлы по папкам (класс с потомками = папка);
  5. делает git commit, если что-то изменилось.

Запросы: <url>?command=classes_list и <url>?command=class&id=<id>
(другой вид - через list_url / class_url в .emel/config.json).
Токен можно не хранить в конфиге, а передавать переменной EMEL_TOKEN.
Коды возврата: 0 - успех, 1 - часть классов не скачалась,
               2 - фатальная ошибка, 3 - другой sync уже запущен.
"""
import argparse
import concurrent.futures as cf
import hashlib
import http.client
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CFG_DIR, CFG_FILE, INDEX_FILE, LOCK_FILE = ".emel", "config.json", "index.json", "sync.lock"
MARK_CTOR = "/*@@ constructor @@*/"
MARK_METH = "/*@@ methods @@*/"
EOL = "\r\n"
# длины текста рядом с подозрительным лимитом 2047 символов
SUSPECT = range(2035, 2049)
RESERVED = ({"con", "prn", "aux", "nul"}
            | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)})


class Fatal(Exception):
    pass


class Locked(Fatal):
    pass


class ApiError(Exception):
    def __init__(self, code, url, body="", what=None):
        what = what or (f"HTTP {code}" if code else "нет ответа от сервера")
        super().__init__(f"{what}: {url} {body[:700]}".strip())
        self.code = code


def warn(msg):
    print("Внимание:", msg, file=sys.stderr)


# --------------------------------------------------------------------- API
def _pairs(pairs):
    """PropertyTree отдаёт массив без имени как объект с пустыми ключами:
    {"": {...}, "": {...}}. Обычный json.loads оставил бы только последний элемент."""
    if pairs and all(k == "" for k, _ in pairs):
        return [v for _, v in pairs]
    return dict(pairs)


class Api:
    def __init__(self, cfg):
        self.base = cfg["url"].rstrip("/")
        self.token = os.environ.get("EMEL_TOKEN") or cfg.get("token")
        if not self.token:
            raise Fatal("не задан токен (config.json или переменная EMEL_TOKEN)")
        # шаблоны URL; {id} подставляется в class_url. По умолчанию:
        #   <url>?command=classes_list   и   <url>?command=class&id=<id>
        self.dump_path = None      # куда сохранять ответ, который не удалось разобрать
        self.list_url = cfg.get("list_url") or f"{self.base}?command=classes_list"
        self.class_url = cfg.get("class_url") or f"{self.base}?command=class&id={{id}}"
        self.timeout = cfg.get("timeout", 60)

    def _get(self, url):
        # токен уходит "как есть": WMS сравнивает заголовок Authorization с token_classes
        req = urllib.request.Request(url, headers={"Authorization": self.token,
                                                   "Accept": "application/json"})
        last = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                if e.code < 500:
                    raise ApiError(e.code, url, body)
                last = f"HTTP {e.code} {body[:200]}"
            except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
                last = f"{type(e).__name__}: {e}"     # в т.ч. IncompleteRead при обрезанном ответе
            else:
                return self._parse(url, raw)
            time.sleep(1 + attempt * 2)
        raise ApiError(None, url, last or "")

    def _dump(self, raw):
        if self.dump_path is None:
            return "(не сохранён)"
        try:
            Path(self.dump_path).write_bytes(raw)
            return str(self.dump_path)
        except OSError:
            return "(не удалось сохранить)"

    def _parse(self, url, raw):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ApiError(None, url, f"{e}; ответ сохранён в {self._dump(raw)}",
                           what="ответ не в кодировке UTF-8")
        try:
            # strict=False: допускает сырые переводы строк и табуляцию внутри строк JSON
            return json.loads(text, object_pairs_hook=_pairs, strict=False)
        except ValueError as e:
            pos = getattr(e, "pos", 0)
            raise ApiError(None, url,
                           f"{e}; размер {len(raw)} байт; вокруг ошибки: "
                           f"{text[max(0, pos - 60):pos + 60]!r}; конец ответа: {text[-60:]!r}; "
                           f"полный ответ: {self._dump(raw)}",
                           what="ответ не является корректным JSON")

    def list_classes(self):
        data = self._get(self.list_url)
        if isinstance(data, dict):
            arrays = [v for v in data.values() if isinstance(v, list)]
            if len(arrays) != 1:
                raise Fatal("ответ со списком классов: ожидался массив "
                            "(или объект с ровно одним массивом)")
            data = arrays[0]
        if not isinstance(data, list):
            raise Fatal("ответ со списком классов: ожидался массив")
        return data

    def get_class(self, cid):
        url = self.class_url.replace("{id}", urllib.parse.quote(str(cid)))
        d = self._get(url)
        # без этой проверки ответ-ошибка превращался в "пустой класс" и коммитился
        if not isinstance(d, dict) or not ({"constructor", "methods", "modified"} & d.keys()):
            raise ApiError(None, url, repr(d), what="ответ не похож на класс (нет constructor/methods)")
        return d


# ------------------------------------------------------------- дерево/пути
def norm_parent(v):
    """None/""/отрицательное число -> корневой класс."""
    if v is None or v == "":
        return None
    try:
        i = int(v)
    except (TypeError, ValueError):
        return None
    return None if i < 0 else str(i)


def norm_list(items):
    nodes = {}
    for it in items:
        if "id" not in it:
            warn(f"элемент списка без id пропущен: {str(it)[:80]}")
            continue
        cid = str(it["id"])
        full = str(it.get("full_name") or "")
        # если API не отдаёт name - берём последний сегмент полного имени
        name = str(it.get("name") or (re.split(r"[./\\]", full)[-1] if full else "") or cid)
        mod, chk = str(it.get("modified") or ""), it.get("checksum")
        # ревизия: дата изменения + контрольная сумма, если она есть в списке
        rev = mod if chk in (None, "") else f"{mod}|{chk}"
        nodes[cid] = {"id": cid, "name": name, "full_name": full or name,
                      "parent": norm_parent(it.get("parent_id")),
                      "modified": mod, "rev": rev}
    return nodes


def check_stamps(nodes):
    """Предупреждаем, если по списку нельзя надёжно определить, что класс изменился."""
    if len(nodes) < 2:
        return
    has_chk = any("|" in n["rev"] for n in nodes.values())
    vals = [n["modified"] for n in nodes.values()]
    zero = sum(1 for v in vals if v.startswith(("0100-", "0000-", "0001-")) or not v)
    if zero:
        warn(f"у {zero} из {len(vals)} классов нулевая/пустая дата изменения (например "
             f"{next(v for v in vals if v.startswith(('0100-', '0000-', '0001-')) or not v)!r})")
    if len(set(vals)) == 1 and not has_chk:
        warn("дата изменения одинакова у всех классов, checksum в списке нет: "
             "sync не сможет заметить правки. Отдавайте в списке настоящую дату "
             "или поле checksum.")


def safe_name(s):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip().rstrip(". ")
    if not s:
        s = "_"
    if s.split(".")[0].lower() in RESERVED:
        s = "_" + s
    return s


def layout(nodes):
    """id -> относительный путь файла. Класс с потомками = папка <Имя>/<Имя>.emel."""
    kids, orphans = {}, {}
    for n in nodes.values():
        p = n["parent"]
        if p is not None and p not in nodes:
            orphans[p] = orphans.get(p, 0) + 1
            p = None
        kids.setdefault(p, []).append(n["id"])
    if orphans:
        shown = ", ".join(f"{k} (x{v})" for k, v in list(orphans.items())[:5])
        warn(f"parent_id не найден среди классов, они считаются корневыми: {shown}")

    def key(cid):
        return (nodes[cid]["name"].lower(), cid)

    paths, used = {}, set()

    def walk(roots):
        stack = [(c, "") for c in sorted(roots, key=key, reverse=True)]
        while stack:
            cid, base = stack.pop()
            if cid in paths:
                continue
            n = nodes[cid]
            has_kids = bool(kids.get(cid))
            seg = safe_name(n["name"])
            while True:
                folder = f"{base}{seg}/" if has_kids else None
                file = f"{folder}{seg}.emel" if has_kids else f"{base}{seg}.emel"
                if file.lower() not in used:   # Windows: регистр в путях не различается
                    break
                seg = f"{seg}~{cid}"
            used.add(file.lower())
            paths[cid] = file
            if folder:
                for c in sorted(kids[cid], key=key, reverse=True):
                    stack.append((c, folder))

    walk(kids.get(None, []))
    left = [c for c in nodes if c not in paths]   # циклы в parent_id
    if left:
        warn(f"циклическая связь parent_id у {len(left)} классов, считаю их корневыми")
        walk(left)
    return paths


def render(ctor, methods):
    return f"{MARK_CTOR}{EOL}{ctor}{EOL}{MARK_METH}{EOL}{methods}"


# ------------------------------------------------------------ файлы и git
def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def prune_empty_dirs(root: Path, rel: str):
    d = (root / rel).parent
    while d != root and d.exists() and not any(d.iterdir()):
        d.rmdir()
        d = d.parent


def git(root, *args, check=True):
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise Fatal(f"git {' '.join(args[:2])}: {r.stderr.strip()}")
    return r


def git_ident(cfg):
    return ["-c", f"user.name={cfg.get('git_user', 'emel-sync')}",
            "-c", f"user.email={cfg.get('git_email', 'emel-sync@localhost')}",
            "-c", "i18n.commitEncoding=utf-8"]


def dirty_emel(root):
    """Незакоммиченные изменения в *.emel (изменённые, удалённые, неотслеживаемые).
    Остальное (.vscode, __pycache__ и т.п.) sync не трогает и не блокирует."""
    out = git(root, "status", "--porcelain", "--untracked-files=all").stdout.splitlines()
    return [l for l in out if re.search(r'\.emel"?\s*$', l)]


def git_commit(root, cfg, subject, body):
    git(root, "add", "-A", "--", "*.emel")   # только файлы классов
    if git(root, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    git(root, *git_ident(cfg), "commit", "-q", "-m", subject, "-m", body)
    return True


class Lock:
    def __init__(self, root):
        self.path = root / CFG_DIR / LOCK_FILE

    def __enter__(self):
        self.path.parent.mkdir(exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                if time.time() - self.path.stat().st_mtime > 6 * 3600:
                    self.path.unlink()      # устаревший lock после падения
                    continue
                raise Locked(f"sync уже запущен (lock: {self.path})")
        raise Locked(f"не удалось взять lock: {self.path}")

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


def load_cfg(root):
    p = root / CFG_DIR / CFG_FILE
    if not p.exists():
        raise Fatal(f"нет {p} - сначала выполните init")
    cfg = json.loads(p.read_text("utf-8"))
    if "url" not in cfg:
        raise Fatal(f"в {p} не задан url")
    return cfg


def old_rev(o):
    return o.get("rev", o.get("modified"))     # старые индексы хранили modified


def load_index(root):
    p = root / CFG_DIR / INDEX_FILE
    return json.loads(p.read_text("utf-8")) if p.exists() else {"version": 1, "classes": {}}


# -------------------------------------------------------------------- init
def cmd_init(a):
    root = Path(a.dir).resolve()
    (root / CFG_DIR).mkdir(parents=True, exist_ok=True)
    cfg_path = root / CFG_DIR / CFG_FILE
    if cfg_path.exists():
        raise Fatal(f"{cfg_path} уже существует")
    token = a.token or os.environ.get("EMEL_TOKEN")
    if not token:
        raise Fatal("укажите --token или переменную EMEL_TOKEN")
    cfg = {"url": a.url, "token": token}
    if a.list_url:
        cfg["list_url"] = a.list_url
    if a.class_url:
        cfg["class_url"] = a.class_url
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    # -text: git не трогает переводы строк (платформа хранит CRLF)
    (root / ".gitattributes").write_text("*.emel -text\n", "utf-8")
    (root / ".gitignore").write_text(f"{CFG_DIR}/\n", "utf-8")   # в .emel лежит токен
    if not (root / ".git").exists():
        git(root, "init", "-q")
    git(root, "add", ".gitattributes", ".gitignore")
    git(root, *git_ident(cfg), "commit", "-q", "-m", "init: emel mirror")
    print(f"Готово: {root}\nДальше: python {Path(__file__).name} sync \"{root}\"")
    return 0


# -------------------------------------------------------------------- sync
def do_sync(root, cfg, a):
    api = Api(cfg)
    api.dump_path = root / CFG_DIR / "bad_response.txt"
    print(f"Зеркало: {root}")
    dirty = [] if a.allow_dirty else dirty_emel(root)
    if dirty:
        shown = "\n  ".join(dirty[:10]) + (f"\n  ... и ещё {len(dirty) - 10}" if len(dirty) > 10 else "")
        raise Fatal(f"в репозитории {root} есть незакоммиченные изменения в файлах классов; sync "
                    "перезаписывает файлы, поэтому закоммитьте их (git add -A && git commit) "
                    "или используйте отдельную копию для зеркала "
                    f"(--allow-dirty, чтобы проигнорировать):\n  {shown}")
    old = load_index(root)["classes"]
    try:
        nodes = norm_list(api.list_classes())
    except ApiError as e:
        raise Fatal(str(e))
    if not a.force and old and (not nodes or (len(old) >= 20 and len(nodes) < len(old) * 0.5)):
        raise Fatal(f"список классов подозрительно короткий: {len(nodes)} против {len(old)} "
                    f"в индексе (--force, если так и должно быть)")
    check_stamps(nodes)
    paths = layout(nodes)

    to_fetch, moves = [], []
    for cid, n in nodes.items():
        o, p = old.get(cid), paths[cid]
        if o is None or a.full or old_rev(o) != n["rev"]:
            to_fetch.append(cid)
        elif o["path"] != p:
            if (root / o["path"]).exists():
                moves.append(cid)
            else:
                to_fetch.append(cid)
        elif not (root / p).exists():
            to_fetch.append(cid)
    deleted = [c for c in old if c not in nodes]
    print(f"Классов в WMS: {len(nodes)}; к скачиванию: {len(to_fetch)}, "
          f"перемещений: {len(moves)}, удалений: {len(deleted)}")
    if a.dry_run:
        return 0

    # 1) скачиваем всё в память; при сбое класс остаётся в прежнем состоянии
    fetched, failed = {}, []
    if to_fetch:
        # сначала до трёх пробных запросов: если ни один не вернул класс,
        # нет смысла долбить сервер тысячами одинаково неудачных запросов
        for cid in to_fetch[:3]:
            try:
                fetched[cid] = api.get_class(cid)
                break
            except ApiError as e:
                failed.append((cid, str(e)))
        else:
            raise Fatal("не удалось получить ни один из первых классов, останавливаюсь. "
                        "Проверьте class_url в .emel/config.json и метод класса на сервере:\n  "
                        + "\n  ".join(err for _, err in failed))
        tried = set(fetched) | {c for c, _ in failed}
        rest = [c for c in to_fetch if c not in tried]
        with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
            futs = {ex.submit(api.get_class, cid): cid for cid in rest}
            for i, fu in enumerate(cf.as_completed(futs), 1):
                cid = futs[fu]
                try:
                    fetched[cid] = fu.result()
                except ApiError as e:
                    failed.append((cid, str(e)))
                if i % 200 == 0:
                    print(f"  скачано {i}/{len(to_fetch)}")

    empty = sum(1 for d in fetched.values() if not d.get("constructor") and not d.get("methods"))
    if not a.force and len(fetched) >= 20 and empty >= len(fetched) * 0.9:
        raise Fatal(f"{empty} из {len(fetched)} скачанных классов пустые (нет ни конструктора, "
                    f"ни методов): похоже, запрос класса возвращает не то. Ничего не записано и "
                    f"не закоммичено (--force, если так и должно быть).")

    # 2) готовим новое состояние
    new_idx, writes, remove = {}, {}, set()
    added, changed, moved = [], [], []
    suspects = []
    for cid, d in fetched.items():
        n, o = nodes[cid], old.get(cid)
        ctor, meth = d.get("constructor") or "", d.get("methods") or ""
        if len(ctor) in SUSPECT or len(meth) in SUSPECT:
            suspects.append(n["full_name"])
        data = render(ctor, meth).encode("utf-8")
        sha = hashlib.sha256(data).hexdigest()
        p = paths[cid]
        # rev берём из списка: если класс изменился между списком и скачиванием,
        # следующий запуск увидит новую ревизию и скачает его ещё раз
        new_idx[cid] = {"path": p, "rev": n["rev"], "sha256": sha,
                        "name": n["name"], "parent_id": n["parent"]}
        same = o and o.get("sha256") == sha and o["path"] == p and (root / p).exists()
        if same:
            continue
        writes[p] = data
        if o and o["path"] != p:
            remove.add(o["path"])
            moved.append(n["full_name"])
        if o is None:
            added.append(n["full_name"])
        elif o.get("sha256") != sha:
            changed.append(n["full_name"])
    for cid in moves:
        o, p = old[cid], paths[cid]
        writes[p] = (root / o["path"]).read_bytes()
        remove.add(o["path"])
        new_idx[cid] = dict(o, path=p, name=nodes[cid]["name"], parent_id=nodes[cid]["parent"])
        moved.append(nodes[cid]["full_name"])
    del_names = []
    for cid in deleted:
        remove.add(old[cid]["path"])
        del_names.append(old[cid].get("name", cid))
    for cid in nodes:                        # без изменений или не скачались - как было
        if cid not in new_idx and cid in old:
            new_idx[cid] = old[cid]

    # 3) применяем: сначала удаляем старые пути, потом пишем новые
    for rel in remove:
        (root / rel).unlink(missing_ok=True)
        prune_empty_dirs(root, rel)
    for rel, data in writes.items():
        atomic_write(root / rel, data)
    atomic_write(root / CFG_DIR / INDEX_FILE, json.dumps(
        {"version": 1, "last_sync": time.strftime("%Y-%m-%dT%H:%M:%S"), "classes": new_idx},
        ensure_ascii=False, indent=1).encode("utf-8"))

    # 4) git
    parts = []
    if added:
        parts.append(f"+{len(added)} новых")
    if changed:
        parts.append(f"~{len(changed)} изменено")
    if moved:
        parts.append(f"{len(moved)} перемещено/переименовано")
    if del_names:
        parts.append(f"-{len(del_names)} удалено")
    if parts and not a.no_commit:
        body = "\n".join(f"{title}: " + ", ".join(names[:40]) + (" ..." if len(names) > 40 else "")
                         for title, names in (("Новые", added), ("Изменены", changed),
                                              ("Перемещены", moved), ("Удалены", del_names))
                         if names)
        done = git_commit(root, cfg, "sync: " + ", ".join(parts), body)
        print("Коммит создан:" if done else "Изменений для коммита нет:", ", ".join(parts))
    elif not parts:
        print("Изменений нет.")

    if suspects:
        warn(f"{len(suspects)} классов с текстом длиной 2035-2048 символов: возможно, текст "
             f"обрезан лимитом строки в WMS (например: {', '.join(suspects[:3])}). "
             f"Если таких много - проверьте метод get_class на длинных классах.")
    for cid, err in failed:
        warn(f"не скачан класс {nodes[cid]['full_name']} (id {cid}): {err}")
    return 1 if failed else 0


def cmd_sync(a):
    root = Path(a.dir).resolve()
    cfg = load_cfg(root)
    if not (root / ".git").exists():
        raise Fatal("нет .git - сначала выполните init")
    with Lock(root):
        return do_sync(root, cfg, a)


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        if isinstance(s, io.TextIOWrapper):   # у pythonw stdout может быть None
            s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Зеркало классов EMEL в git")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("init", help="создать папку зеркала, конфиг и git-репозиторий")
    pi.add_argument("dir")
    pi.add_argument("--url", required=True,
                    help="URL API без command, например http://192.168.0.12:3215/api")
    pi.add_argument("--token")
    pi.add_argument("--list-url", help="если список классов запрашивается не как <url>?command=classes_list")
    pi.add_argument("--class-url", help="шаблон запроса класса с {id}, "
                    "по умолчанию <url>?command=class&id={id}")
    pi.set_defaults(func=cmd_init)
    ps = sub.add_parser("sync", help="подтянуть изменения и закоммитить")
    ps.add_argument("dir", nargs="?", default=".")
    ps.add_argument("--dry-run", action="store_true", help="только показать план")
    ps.add_argument("--full", action="store_true", help="перескачать все классы")
    ps.add_argument("--workers", type=int, default=2, help="параллельных запросов (по умолчанию 2)")
    ps.add_argument("--no-commit", action="store_true")
    ps.add_argument("--force", action="store_true", help="не проверять, что список не сократился вдвое")
    ps.add_argument("--allow-dirty", action="store_true")
    ps.set_defaults(func=cmd_sync)
    a = ap.parse_args(argv)
    try:
        return a.func(a)
    except Locked as e:
        print("Ошибка:", e, file=sys.stderr)
        return 3
    except Fatal as e:
        print("Ошибка:", e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())